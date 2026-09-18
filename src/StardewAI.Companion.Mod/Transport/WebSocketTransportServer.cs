using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace StardewAI.Companion.Mod.Transport;

public delegate void TransportLogger(string message, string level = "Info");

public sealed class WebSocketTransportServer : ITransportServer
{
    private const int MaxMessageSizeBytes = 65536; // 64 KB
    private const int MaxJsonDepth = 32;

    private readonly int _maxNormalOutboundCapacity;
    private readonly int _maxCriticalOutboundCapacity;

    private sealed record OutboundItem(EnvelopeDto Envelope, bool IsCritical, long Generation);

    private readonly HttpListener _listener;
    private readonly BoundedMessageQueue _queue;
    private readonly IdempotencyManager _idempotencyManager;
    private readonly ITransportHandler _handler;
    private readonly TransportLogger? _logger;
    private readonly object _stateLock = new();

    // Outbound async queuing (dedicated sender task) tagged with socket generation
    private readonly Queue<OutboundItem> _outboundCriticalQueue = new();
    private readonly Queue<OutboundItem> _outboundNormalQueue = new();
    private readonly SemaphoreSlim _outboundSignal = new(0);
    private readonly object _outboundLock = new();

    private WebSocket? _activeSocket;
    private long _socketGeneration;
    private CancellationTokenSource? _serverCts;
    private CancellationTokenSource? _socketCts;
    private Task? _listenerLoopTask;
    private Task? _senderTask;

    private string? _saveId;
    private string? _gameSessionId;
    private readonly string _senderInstanceId;
    private long _outgoingSequenceNumber;
    private bool _disposed;
    private bool _initialSnapshotPending;

    // Disconnect events pending for main thread dispatch across generations
    private readonly Queue<(long Generation, string Reason)> _pendingDisconnects = new();
    private readonly object _disconnectLock = new();

    public int Port { get; }
    public string SessionToken { get; }
    public TransportState State { get; private set; } = TransportState.Disconnected;
    public bool IsClientConnected => _activeSocket is { State: WebSocketState.Open };
    public string? ActiveCommandId { get; private set; }
    public IdempotencyManager Idempotency => _idempotencyManager;

    private WebSocket? _chatSocket;
    private long _chatSocketGeneration;
    private CancellationTokenSource? _chatSocketCts;
    private readonly Queue<ChatReplyPayload> _pendingChatReplies = new();
    private readonly object _chatLock = new();
    private readonly SemaphoreSlim _chatSendLock = new(1, 1);
    private WorldSnapshotPayload? _lastSnapshot;
    private long _lastSnapshotRevision;

    public bool IsChatConnected => _chatSocket is { State: WebSocketState.Open };
    public event Action<ChatReplyPayload>? OnChatReplyReceived;
    public event Action<AutonomyStatePayload>? OnAutonomyStateReceived;
    public event Action<string?, string?, string>? OnChatChannelProblem;


    public static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        MaxDepth = MaxJsonDepth,
        WriteIndented = false
    };

    public WebSocketTransportServer(
        int port,
        string? sessionToken,
        ITransportHandler handler,
        string? saveId = null,
        string? gameSessionId = null,
        TransportLogger? logger = null,
        int maxNormalOutboundCapacity = 128,
        int maxCriticalOutboundCapacity = 64)
    {
        _handler = handler ?? throw new ArgumentNullException(nameof(handler));
        _logger = logger;
        _saveId = saveId;
        _gameSessionId = gameSessionId;
        _senderInstanceId = $"mod-{Guid.NewGuid():N}"[..12];
        _maxNormalOutboundCapacity = maxNormalOutboundCapacity > 0 ? maxNormalOutboundCapacity : 128;
        _maxCriticalOutboundCapacity = maxCriticalOutboundCapacity > 0 ? maxCriticalOutboundCapacity : 64;

        Port = port > 0 ? port : GetAvailablePort();
        SessionToken = string.IsNullOrWhiteSpace(sessionToken)
            ? GenerateSecureToken()
            : sessionToken;

        _queue = new BoundedMessageQueue(maxNormalCapacity: 64, maxPriorityCapacity: 16);
        _idempotencyManager = new IdempotencyManager(maxCapacity: 256);

        _listener = new HttpListener();
        _listener.Prefixes.Add($"http://127.0.0.1:{Port}/");
    }

    public WebSocketTransportServer(
        int port,
        string? sessionToken,
        ITransportHandler handler,
        string? saveId,
        string? gameSessionId,
        Action<string>? simpleLogger)
        : this(port, sessionToken, handler, saveId, gameSessionId,
            simpleLogger != null ? new TransportLogger((msg, _) => simpleLogger(msg)) : (TransportLogger?)null)
    {
    }

    public WebSocketTransportServer(
        int port,
        string? sessionToken,
        ITransportHandler handler,
        string? saveId,
        string? gameSessionId,
        Action<string, StardewModdingAPI.LogLevel>? smapiLogger)
        : this(port, sessionToken, handler, saveId, gameSessionId,
            smapiLogger != null ? new TransportLogger((msg, level) => smapiLogger(msg, ParseSmapiLevel(level))) : (TransportLogger?)null)
    {
    }

    private static StardewModdingAPI.LogLevel ParseSmapiLevel(string level) => level switch
    {
        "Warn" or "Warning" => StardewModdingAPI.LogLevel.Warn,
        "Error" => StardewModdingAPI.LogLevel.Error,
        "Trace" => StardewModdingAPI.LogLevel.Trace,
        _ => StardewModdingAPI.LogLevel.Info
    };

    public void SetGameSession(string saveId, string gameSessionId)
    {
        lock (_stateLock)
        {
            _saveId = saveId;
            _gameSessionId = gameSessionId;
        }
    }

    public void Start()
    {
        lock (_stateLock)
        {
            if (State != TransportState.Disconnected && State != TransportState.Offline)
            {
                return;
            }

            _serverCts = new CancellationTokenSource();
            _listener.Start();
            State = TransportState.Listening;
            _listenerLoopTask = Task.Run(() => AcceptLoopAsync(_serverCts.Token));
            _logger?.Invoke($"WebSocket transport server listening on 127.0.0.1:{Port}");
        }
    }

    public void Stop()
    {
        lock (_stateLock)
        {
            if (State == TransportState.Disconnected)
            {
                return;
            }

            _serverCts?.Cancel();
            try
            {
                if (_listener.IsListening)
                {
                    _listener.Stop();
                }
            }
            catch (Exception ex)
            {
                _logger?.Invoke($"Error stopping HttpListener: {ex.Message}", "Warn");
            }

            CloseActiveSocketNonBlocking("Server stopping");
            _queue.Clear();
            ClearOutboundQueues();
            ActiveCommandId = null;
            State = TransportState.Disconnected;
        }
    }

    private async Task AcceptLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested && _listener.IsListening)
        {
            try
            {
                var context = await _listener.GetContextAsync().ConfigureAwait(false);
                if (ct.IsCancellationRequested)
                {
                    context.Response.Abort();
                    break;
                }

                _logger?.Invoke($"Incoming connection from {context.Request.RemoteEndPoint} (Method={context.Request.HttpMethod}, Path={context.Request.Url?.AbsolutePath}, IsWebSocket={context.Request.IsWebSocketRequest})", "Trace");

                // Security: Validate loopback origin only
                if (!IPAddress.IsLoopback(context.Request.RemoteEndPoint.Address))
                {
                    _logger?.Invoke($"Rejected non-loopback connection from {context.Request.RemoteEndPoint} (403 Forbidden)", "Warn");
                    context.Response.StatusCode = (int)HttpStatusCode.Forbidden;
                    context.Response.ContentLength64 = 0;
                    context.Response.Close();
                    continue;
                }

                // Security: Session token authentication (prefer header over query)
                if (!ValidateSessionToken(context.Request))
                {
                    bool hasAuth = !string.IsNullOrEmpty(context.Request.Headers["Authorization"]);
                    bool hasQuery = !string.IsNullOrEmpty(context.Request.QueryString["token"]);
                    _logger?.Invoke($"Rejected unauthorized connection from {context.Request.RemoteEndPoint} (401 Unauthorized, HasAuthHeader={hasAuth}, HasQueryToken={hasQuery})", "Warn");
                    context.Response.StatusCode = (int)HttpStatusCode.Unauthorized;
                    context.Response.ContentLength64 = 0;
                    context.Response.Close();
                    continue;
                }

                // Validate WebSocket upgrade request
                if (!context.Request.IsWebSocketRequest)
                {
                    string upgrade = context.Request.Headers["Upgrade"] ?? "<none>";
                    string conn = context.Request.Headers["Connection"] ?? "<none>";
                    _logger?.Invoke($"Rejected non-WebSocket request from {context.Request.RemoteEndPoint} (400 BadRequest, Upgrade='{upgrade}', Connection='{conn}')", "Warn");
                    context.Response.StatusCode = (int)HttpStatusCode.BadRequest;
                    context.Response.ContentLength64 = 0;
                    context.Response.Close();
                    continue;
                }

                // Route /chat to dedicated chat channel
                if (string.Equals(context.Request.Url?.AbsolutePath.TrimEnd('/'), "/chat", StringComparison.OrdinalIgnoreCase))
                {
                    await HandleChatConnectionAsync(context, ct).ConfigureAwait(false);
                    continue;
                }

                // Enforce single connected client for skill execution
                lock (_stateLock)
                {
                    if (_activeSocket != null && _activeSocket.State == WebSocketState.Open)
                    {
                        _logger?.Invoke($"Rejected concurrent connection from {context.Request.RemoteEndPoint} (409 Conflict, active socket exists in generation {_socketGeneration})", "Warn");
                        context.Response.StatusCode = (int)HttpStatusCode.Conflict;
                        context.Response.ContentLength64 = 0;
                        context.Response.Close();
                        continue;
                    }
                }

                _logger?.Invoke($"Accepting WebSocket connection from {context.Request.RemoteEndPoint}...", "Info");
                var wsContext = await context.AcceptWebSocketAsync(subProtocol: null).ConfigureAwait(false);
                long gen = Interlocked.Increment(ref _socketGeneration);

                lock (_stateLock)
                {
                    // Clean up previous socket CTS
                    _socketCts?.Cancel();
                    _socketCts?.Dispose();
                    _socketCts = new CancellationTokenSource();

                    _activeSocket = wsContext.WebSocket;
                    State = TransportState.Handshaking;
                    // NOTE: DO NOT clear _pendingDisconnects! Any pending disconnect from a previous
                    // generation must be preserved and delivered on the main thread in Update()
                    // before new executable commands are accepted.
                }

                // Reset outgoing sequence number for the new socket generation
                Interlocked.Exchange(ref _outgoingSequenceNumber, 0);

                // Purge stale messages from previous generations
                _queue.PurgeOlderThanGeneration(gen);
                ClearOutboundQueuesOlderThan(gen);

                _logger?.Invoke($"WebSocket client connected (generation {gen}) from {context.Request.RemoteEndPoint}. Awaiting handshake.", "Info");

                // Start dedicated background sender and receiver loops
                var combinedCts = CancellationTokenSource.CreateLinkedTokenSource(ct, _socketCts.Token);
                _senderTask = Task.Run(() => SendLoopAsync(wsContext.WebSocket, gen, combinedCts.Token), combinedCts.Token);
                _ = Task.Run(() => ReceiveLoopAsync(wsContext.WebSocket, gen, combinedCts.Token), combinedCts.Token);
            }
            catch (HttpListenerException) when (ct.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                if (!ct.IsCancellationRequested)
                {
                    _logger?.Invoke($"AcceptLoop exception: {ex.Message}", "Warn");
                }
            }
        }
    }

    private bool ValidateSessionToken(HttpListenerRequest request)
    {
        // 1. Prefer Authorization header: Bearer <token>
        var authHeader = request.Headers["Authorization"];
        if (!string.IsNullOrEmpty(authHeader) && authHeader.StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase))
        {
            var bearerToken = authHeader[7..].Trim();
            if (CryptographicOperations.FixedTimeEquals(
                    Encoding.UTF8.GetBytes(bearerToken),
                    Encoding.UTF8.GetBytes(SessionToken)))
            {
                return true;
            }
        }

        // 2. Fall back to query string: ?token=<token>
        var queryToken = request.QueryString["token"];
        if (!string.IsNullOrEmpty(queryToken) &&
            CryptographicOperations.FixedTimeEquals(
                Encoding.UTF8.GetBytes(queryToken),
                Encoding.UTF8.GetBytes(SessionToken)))
        {
            return true;
        }

        return false;
    }

    private async Task ReceiveLoopAsync(WebSocket socket, long gen, CancellationToken ct)
    {
        var buffer = new byte[4096];
        using var ms = new MemoryStream();
        _logger?.Invoke($"ReceiveLoop started for generation {gen}.", "Trace");

        try
        {
            while (!ct.IsCancellationRequested && socket.State == WebSocketState.Open)
            {
                ms.SetLength(0);
                WebSocketReceiveResult result;

                do
                {
                    result = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), ct).ConfigureAwait(false);
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        _logger?.Invoke($"Client initiated WebSocket close in generation {gen} (CloseStatus={result.CloseStatus}, Description='{result.CloseStatusDescription}').", "Info");
                        ScheduleDisconnect(gen, "Client requested close");
                        return;
                    }

                    if (ms.Length + result.Count > MaxMessageSizeBytes)
                    {
                        _logger?.Invoke($"Message exceeded maximum size limit ({MaxMessageSizeBytes} bytes) in generation {gen}. Closing socket.", "Warn");
                        ScheduleDisconnect(gen, "Message too big");
                        return;
                    }

                    ms.Write(buffer, 0, result.Count);
                }
                while (!result.EndOfMessage);

                if (result.MessageType == WebSocketMessageType.Text)
                {
                    var jsonString = Encoding.UTF8.GetString(ms.GetBuffer(), 0, (int)ms.Length);
                    _logger?.Invoke($"Received text frame ({ms.Length} bytes) in generation {gen}.", "Trace");
                    ProcessIncomingRawMessage(jsonString, gen);
                }
            }
        }
        catch (OperationCanceledException)
        {
            _logger?.Invoke($"ReceiveLoop cancelled for generation {gen}.", "Trace");
        }
        catch (WebSocketException ex)
        {
            _logger?.Invoke($"WebSocket receive error in generation {gen}: {ex.WebSocketErrorCode} - {ex.Message}", "Warn");
            ScheduleDisconnect(gen, "WebSocket exception");
        }
        catch (Exception ex)
        {
            _logger?.Invoke($"ReceiveLoop unexpected error in generation {gen}: {ex.Message}", "Error");
            ScheduleDisconnect(gen, "Unexpected error");
        }
        finally
        {
            _logger?.Invoke($"ReceiveLoop exited for generation {gen}.", "Trace");
        }
    }

    private void ProcessIncomingRawMessage(string json, long gen)
    {
        EnvelopeDto? envelope;
        try
        {
            envelope = JsonSerializer.Deserialize<EnvelopeDto>(json, JsonOptions);
        }
        catch (Exception ex)
        {
            _logger?.Invoke($"Failed to deserialize message: {ex.Message}", "Warn");
            _ = SendProtocolErrorAsync("MALFORMED_JSON", "Envelope JSON could not be parsed.");
            return;
        }

        if (envelope == null)
        {
            _ = SendProtocolErrorAsync("EMPTY_MESSAGE", "Envelope cannot be null.");
            return;
        }

        if (envelope.ProtocolVersion != "0.1")
        {
            _ = SendProtocolErrorAsync("UNSUPPORTED_PROTOCOL", "Protocol version must be 0.1.", envelope.MessageId);
            return;
        }

        if (envelope.SequenceNumber < 0)
        {
            _ = SendProtocolErrorAsync("INVALID_SEQUENCE", "Sequence number must be non-negative.", envelope.MessageId);
            return;
        }

        if (string.IsNullOrWhiteSpace(envelope.MessageType))
        {
            _ = SendProtocolErrorAsync("MISSING_MESSAGE_TYPE", "MessageType cannot be empty.", envelope.MessageId);
            return;
        }

        if (string.IsNullOrWhiteSpace(envelope.MessageId))
        {
            _ = SendProtocolErrorAsync("MISSING_MESSAGE_ID", "MessageId cannot be empty.", envelope.MessageId);
            return;
        }

        // Handle Handshake runtime.hello immediately on network thread
        if (string.Equals(envelope.MessageType, "runtime.hello", StringComparison.OrdinalIgnoreCase))
        {
            HandleRuntimeHello(envelope);
            return;
        }

        // Enqueue to main-thread bounded queue
        // Enqueue to main-thread bounded queue tagged with receiving socket generation
        if (!_queue.TryEnqueue(envelope, gen, out bool purgedQueuedCommand))
        {
            if (purgedQueuedCommand)
            {
                _logger?.Invoke($"Command {envelope.MessageId} cancelled before execution; purged.", "Info");
            }
            else
            {
                _logger?.Invoke("Queue full. Shedding message.", "Warn");
                _ = SendProtocolErrorAsync("QUEUE_FULL", "Main thread queue capacity exceeded.", envelope.MessageId);
            }
        }
    }

    private void HandleRuntimeHello(EnvelopeDto helloEnvelope)
    {
        lock (_stateLock)
        {
            _logger?.Invoke($"Received runtime.hello in generation {_socketGeneration} (sender={helloEnvelope.SenderInstanceId}, protocolVersion={helloEnvelope.ProtocolVersion}).", "Info");

            var welcomePayload = new ModWelcomePayload(
                SelectedProtocolVersion: "0.1",
                ModVersion: "0.1.0",
                GameVersion: "1.6.15",
                SmapiVersion: "4.2.1",
                LifecycleState: "syncing",
                Skills: new[] { "water-zone", "harvest-zone", "deposit-chest", "organize-chest", "hoe-tiles", "plant-seeds", "ship-items", "withdraw-chest" }
            );

            var welcomeJson = JsonSerializer.SerializeToNode(welcomePayload, JsonOptions)!.AsObject();
            var welcomeEnvelope = CreateEnvelope("mod.welcome", welcomeJson, helloEnvelope.MessageId);

            EnqueueOutbound(welcomeEnvelope, isCritical: true, _socketGeneration);

            State = TransportState.Syncing;
            _initialSnapshotPending = true;
            _logger?.Invoke($"Handshake accepted in generation {_socketGeneration}. Sent mod.welcome. Initial snapshot pending.", "Info");
        }
    }

    /// <summary>
    /// Dedicated background loop that handles non-blocking asynchronous outbound frame transmission.
    /// Eliminates synchronous GetAwaiter().GetResult() blocking of the game thread.
    /// </summary>
    private async Task SendLoopAsync(WebSocket socket, long gen, CancellationToken ct)
    {
        _logger?.Invoke($"SendLoop started for generation {gen}.", "Trace");
        try
        {
            while (!ct.IsCancellationRequested && socket.State == WebSocketState.Open)
            {
                await _outboundSignal.WaitAsync(ct).ConfigureAwait(false);

                OutboundItem? item = null;
                lock (_outboundLock)
                {
                    if (_outboundCriticalQueue.Count > 0)
                    {
                        item = _outboundCriticalQueue.Dequeue();
                    }
                    else if (_outboundNormalQueue.Count > 0)
                    {
                        item = _outboundNormalQueue.Dequeue();
                    }
                }

                if (item == null)
                {
                    continue;
                }

                // Discard outbound item if it belongs to a stale socket generation
                if (item.Generation != gen)
                {
                    _logger?.Invoke($"Discarding outbound envelope {item.Envelope.MessageType} for stale generation {item.Generation} (current: {gen})", "Info");
                    continue;
                }

                // Monotonic outgoing sequence number assigned in ACTUAL send order right before wire transmission
                long seq = Interlocked.Increment(ref _outgoingSequenceNumber) - 1;
                var envelopeToSend = item.Envelope with { SequenceNumber = seq };

                var json = JsonSerializer.Serialize(envelopeToSend, JsonOptions);
                var bytes = Encoding.UTF8.GetBytes(json);

                if (bytes.Length > MaxMessageSizeBytes)
                {
                    _logger?.Invoke("Outgoing envelope exceeds max frame size. Dropping.", "Warn");
                    continue;
                }

                using var sendTimeoutCts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
                using var linkedCts = CancellationTokenSource.CreateLinkedTokenSource(ct, sendTimeoutCts.Token);

                await socket.SendAsync(
                    new ArraySegment<byte>(bytes),
                    WebSocketMessageType.Text,
                    endOfMessage: true,
                    linkedCts.Token
                ).ConfigureAwait(false);
                _logger?.Invoke($"Sent envelope {envelopeToSend.MessageType} (seq={seq}) to wire in generation {gen}.", "Trace");
            }
        }
        catch (OperationCanceledException)
        {
            _logger?.Invoke($"SendLoop cancelled for generation {gen}.", "Trace");
        }
        catch (Exception ex)
        {
            _logger?.Invoke($"SendLoop error in generation {gen}: {ex.Message}", "Warn");
            ScheduleDisconnect(gen, "Send failure");
        }
        finally
        {
            _logger?.Invoke($"SendLoop exited for generation {gen}.", "Trace");
        }
    }

    private void EnqueueOutbound(EnvelopeDto envelope, bool isCritical, long gen)
    {
        lock (_outboundLock)
        {
            if (isCritical)
            {
                if (_outboundCriticalQueue.Count < _maxCriticalOutboundCapacity)
                {
                    _outboundCriticalQueue.Enqueue(new OutboundItem(envelope, IsCritical: true, Generation: gen));
                    _outboundSignal.Release();
                }
                else
                {
                    _logger?.Invoke("Outbound critical queue saturated; triggering explicit disconnect with cached replay outcome retained.", "Error");
                    // Critical overflow: trigger explicit disconnect and backpressure failure instead of dropping silently.
                    // Cached outcome in IdempotencyManager is preserved so client can recover outcome on reconnect.
                    ScheduleDisconnect(gen, "BACKPRESSURE_CRITICAL_OVERFLOW: Critical outbound queue saturated");
                }
            }
            else
            {
                if (_outboundNormalQueue.Count < _maxNormalOutboundCapacity)
                {
                    _outboundNormalQueue.Enqueue(new OutboundItem(envelope, IsCritical: false, Generation: gen));
                    _outboundSignal.Release();
                }
                else
                {
                    _logger?.Invoke("Outbound normal queue full; shedding message.", "Warn");
                }
            }
        }
    }

    private void ClearOutboundQueuesOlderThan(long activeGeneration)
    {
        lock (_outboundLock)
        {
            var criticalItems = _outboundCriticalQueue.Where(i => i.Generation == activeGeneration).ToList();
            _outboundCriticalQueue.Clear();
            foreach (var item in criticalItems)
            {
                _outboundCriticalQueue.Enqueue(item);
            }

            var normalItems = _outboundNormalQueue.Where(i => i.Generation == activeGeneration).ToList();
            _outboundNormalQueue.Clear();
            foreach (var item in normalItems)
            {
                _outboundNormalQueue.Enqueue(item);
            }
        }
    }

    private void ClearOutboundQueues()
    {
        lock (_outboundLock)
        {
            _outboundCriticalQueue.Clear();
            _outboundNormalQueue.Clear();
        }
    }

    /// <summary>
    /// Thread-safe generation-scoped disconnect schedule.
    /// Does NOT call handler callbacks on the network thread; queues disconnect for Update().
    /// </summary>
    private void ScheduleDisconnect(long gen, string reason)
    {
        lock (_stateLock)
        {
            if (gen != _socketGeneration || State == TransportState.Disconnected)
            {
                return;
            }

            State = TransportState.Offline;
            lock (_disconnectLock)
            {
                _pendingDisconnects.Enqueue((gen, reason));
            }
            CloseActiveSocketNonBlocking(reason);
        }

        _logger?.Invoke($"WebSocket disconnect scheduled for main thread (gen {gen}): {reason}", "Info");
    }

    private void CloseActiveSocketNonBlocking(string reason)
    {
        var socket = _activeSocket;
        _activeSocket = null;
        _socketCts?.Cancel();
        _logger?.Invoke($"CloseActiveSocket non-blocking initiated: {reason}", "Trace");

        if (socket != null)
        {
            _ = Task.Run(async () =>
            {
                try
                {
                    if (socket.State == WebSocketState.Open)
                    {
                        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(2));
                        await socket.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, reason, cts.Token).ConfigureAwait(false);
                    }
                }
                catch
                {
                    // Suppress network tear-down exceptions
                }
                finally
                {
                    socket.Dispose();
                }
            });
        }
    }

    private const int MaxEnvelopesPerTick = 32;
    private static readonly TimeSpan MaxTickProcessingTime = TimeSpan.FromMilliseconds(4);

    /// <summary>
    /// Called every tick on SMAPI's GameLoop.UpdateTicked on the main game thread.
    /// Drains the bounded message queue and executes callbacks against ITransportHandler.
    /// </summary>
    public void Update()
    {
        // 1. Process and deliver pending disconnect callbacks on the main game thread.
        // This MUST be delivered before processing any new incoming commands or snapshots
        // to ensure active tasks from the previous generation are cancelled first.
        List<(long Gen, string Reason)> disconnectsToDeliver = new();
        lock (_disconnectLock)
        {
            while (_pendingDisconnects.Count > 0)
            {
                disconnectsToDeliver.Add(_pendingDisconnects.Dequeue());
            }
        }

        foreach (var (discGen, reason) in disconnectsToDeliver)
        {
            _logger?.Invoke($"Delivering disconnect callback on main thread for generation {discGen}: {reason}", "Info");
            ActiveCommandId = null;
            try
            {
                _handler.OnTransportDisconnected(reason);
            }
            catch (Exception ex)
            {
                _logger?.Invoke($"Error in OnTransportDisconnected: {ex.Message}", "Error");
            }
        }

        // 2. Process initial snapshot once handshake is accepted for the current connection
        if (_initialSnapshotPending && (State == TransportState.Syncing || State == TransportState.Ready))
        {
            _initialSnapshotPending = false;
            try
            {
                var snapshot = _handler.CaptureCurrentSnapshot(worldRevision: 1);
                _ = SendSnapshotAsync(snapshot, worldRevision: 1);
                lock (_stateLock)
                {
                    State = TransportState.Ready;
                }
                _logger?.Invoke("Initial snapshot sent. Transport is now Ready.");
            }
            catch (Exception ex)
            {
                _logger?.Invoke($"Error capturing initial snapshot: {ex.Message}", "Error");
            }
        }

        // 3. Drain pending messages from the main-thread bounded queue with per-tick count and time budget
        var sw = Stopwatch.StartNew();
        int processedCount = 0;

        while (processedCount < MaxEnvelopesPerTick && sw.Elapsed < MaxTickProcessingTime && _queue.TryDequeue(out var envelope, out long msgGen))
        {
            if (envelope == null)
            {
                continue;
            }

            // Stale socket cross-talk prevention: discard messages from older socket generations
            if (msgGen != _socketGeneration)
            {
                _logger?.Invoke($"Dropping stale incoming envelope {envelope.MessageId} from generation {msgGen} (current: {_socketGeneration})", "Info");
                continue;
            }

            ProcessMainThreadEnvelope(envelope);
            processedCount++;
        }

        // 4. Drain pending chat replies from agent bridge and deliver callbacks on main thread
        List<ChatReplyPayload> chatRepliesToDeliver = new();
        lock (_chatLock)
        {
            while (_pendingChatReplies.Count > 0)
            {
                chatRepliesToDeliver.Add(_pendingChatReplies.Dequeue());
            }
        }

        foreach (var chatReply in chatRepliesToDeliver)
        {
            try
            {
                OnChatReplyReceived?.Invoke(chatReply);
            }
            catch (Exception ex)
            {
                _logger?.Invoke($"Error delivering OnChatReplyReceived callback: {ex.Message}", "Error");
            }
        }
    }

    private void ProcessMainThreadEnvelope(EnvelopeDto envelope)
    {
        // Fail-closed expiration check
        if (envelope.ExpiresAt.HasValue && envelope.ExpiresAt.Value < DateTimeOffset.UtcNow)
        {
            _logger?.Invoke($"Command {envelope.MessageId} expired. Rejecting.", "Warn");
            SendTerminalRejection(envelope, "EXPIRED", "Command expired before execution.", recordIdempotency: false);
            return;
        }

        // Save ID and Session ID validation (fail-closed if bound to a save)
        if (!string.IsNullOrEmpty(_saveId) && !string.IsNullOrEmpty(envelope.SaveId) &&
            !string.Equals(_saveId, envelope.SaveId, StringComparison.Ordinal))
        {
            SendTerminalRejection(envelope, "SAVE_MISMATCH", "Target save ID does not match active game session.", recordIdempotency: false);
            return;
        }

        switch (envelope.MessageType.ToLowerInvariant())
        {
            case "skill.execute":
                HandleSkillExecute(envelope);
                break;

            case "skill.cancel":
                HandleSkillCancel(envelope);
                break;

            case "skill.pause":
                HandleSkillPause(envelope);
                break;

            case "skill.resume":
                HandleSkillResume(envelope);
                break;

            default:
                _logger?.Invoke($"Unrecognized message type: {envelope.MessageType}", "Warn");
                _ = SendProtocolErrorAsync("UNKNOWN_TYPE", $"Unrecognized message type {envelope.MessageType}", envelope.MessageId);
                break;
        }
    }

    private void HandleSkillExecute(EnvelopeDto envelope)
    {
        // Ready-before-execute invariant
        if (State != TransportState.Ready)
        {
            SendTerminalRejection(envelope, "NOT_READY", "Transport session is not in Ready state.", recordIdempotency: false);
            return;
        }

        // Reject missing idempotencyKey (schema strictly requires it, never synthesize!)
        if (string.IsNullOrWhiteSpace(envelope.IdempotencyKey))
        {
            SendTerminalRejection(envelope, "MISSING_IDEMPOTENCY_KEY", "idempotencyKey is required for skill.execute.", recordIdempotency: false);
            return;
        }

        // Check if command was tombstoned by an earlier cancel-before-execute
        string idempotencyKey = envelope.IdempotencyKey;

        SkillExecutePayload? payload;
        try
        {
            payload = envelope.Payload.Deserialize<SkillExecutePayload>(JsonOptions);
        }
        catch (Exception ex)
        {
            SendTerminalRejection(envelope, "INVALID_PAYLOAD", $"Could not parse skill.execute payload: {ex.Message}", recordIdempotency: false);
            return;
        }

        if (payload == null)
        {
            SendTerminalRejection(envelope, "EMPTY_PAYLOAD", "skill.execute payload cannot be null.", recordIdempotency: false);
            return;
        }

        if (string.IsNullOrWhiteSpace(payload.CommandId) || string.IsNullOrWhiteSpace(payload.TaskId))
        {
            SendTerminalRejection(envelope, "INVALID_IDENTIFIERS", "commandId and taskId must be non-empty.", recordIdempotency: false);
            return;
        }

        // Cancel-before-execute tombstone check
        if (_queue.IsCommandTombstoned(payload.CommandId))
        {
            _logger?.Invoke($"Command {payload.CommandId} was cancelled before execution started.", "Info");
            var cancelledResult = new SkillResultPayload(
                CommandId: payload.CommandId,
                TaskId: payload.TaskId,
                TerminalState: "cancelled",
                CompletedCount: 0,
                SkippedCount: 0,
                FailedCount: 0,
                FinalWorldRevision: envelope.WorldRevision,
                Effects: new List<Dictionary<string, object>>(),
                Resources: null,
                Error: new SkillResultError("CANCELLED_BEFORE_EXECUTE", "Command was cancelled before execution started.", null, false)
            );
            _ = SendSkillResultAsync(cancelledResult, envelope.MessageId, idempotencyKey);
            return;
        }

        // Validate parameter and budget bounds for the requested skill
        if (!ValidateSkillBounds(payload, out var boundsError))
        {
            SendTerminalRejection(envelope, "BOUNDS_VIOLATION", boundsError!, recordIdempotency: false);
            return;
        }

        // Canonical full semantic fingerprinting (covers all parameters, budgets, policies, and sorted tiles)
        string semanticFingerprint = IdempotencyManager.ComputeSemanticFingerprint(payload);

        var check = _idempotencyManager.Check(idempotencyKey, payload.CommandId, semanticFingerprint, out var cachedResult);
        switch (check)
        {
            case IdempotencyCheck.CapacityExceeded:
                SendTerminalRejection(envelope, "IDEMPOTENCY_CAPACITY_EXCEEDED", "Idempotency cache capacity reached for this session.", recordIdempotency: false);
                return;

            case IdempotencyCheck.Conflict:
                // CRITICAL RULE: Conflict rejection must NEVER overwrite the legitimate in-flight or completed record!
                _logger?.Invoke($"Idempotency conflict for key {idempotencyKey}. Rejecting conflicting request.", "Warn");
                SendTerminalRejection(envelope, "IDEMPOTENCY_CONFLICT", "Idempotency key reused with mismatched command semantics.", recordIdempotency: false);
                return;

            case IdempotencyCheck.CompletedReplay:
                _logger?.Invoke($"Replaying cached result for idempotency key {idempotencyKey}", "Info");
                if (cachedResult != null)
                {
                    // Replay preserves original result identity with fresh sequence number
                    _ = SendSkillResultAsync(cachedResult, envelope.MessageId, idempotencyKey);
                }
                return;

            case IdempotencyCheck.InFlightDuplicate:
                _logger?.Invoke($"Duplicate in-flight command for key {idempotencyKey}. Acknowledging.", "Info");
                return;

            case IdempotencyCheck.New:
            default:
                break;
        }

        // One active task invariant: reject if another task is currently active
        if (ActiveCommandId != null)
        {
            SendTerminalRejection(envelope, "BUSY", $"Another command is currently active ({ActiveCommandId}).", recordIdempotency: false);
            return;
        }

        // Register in-flight
        if (!_idempotencyManager.TryMarkInFlight(idempotencyKey, payload.CommandId, semanticFingerprint))
        {
            SendTerminalRejection(envelope, "IDEMPOTENCY_CAPACITY_EXCEEDED", "Idempotency capacity full.", recordIdempotency: false);
            return;
        }

        ActiveCommandId = payload.CommandId;

        // Dispatch to mechanics actor / handler on main thread
        if (!_handler.TryAcceptSkillExecute(payload, envelope, out var rejectResult))
        {
            ActiveCommandId = null;
            if (rejectResult != null)
            {
                _ = SendSkillResultAsync(rejectResult, envelope.MessageId, idempotencyKey);
            }
        }
    }

    private static bool ValidateSkillBounds(SkillExecutePayload payload, out string? error)
    {
        if (payload.Parameters == null)
        {
            error = "Parameters object is required.";
            return false;
        }

        if (string.IsNullOrWhiteSpace(payload.Parameters.LocationId))
        {
            error = "LocationId is required.";
            return false;
        }

        string skillId = payload.SkillId ?? "";
        bool isWater = string.Equals(skillId, "water-zone", StringComparison.OrdinalIgnoreCase);
        bool isHarvest = string.Equals(skillId, "harvest-zone", StringComparison.OrdinalIgnoreCase);
        bool isDeposit = string.Equals(skillId, "deposit-chest", StringComparison.OrdinalIgnoreCase);
        bool isOrganize = string.Equals(skillId, "organize-chest", StringComparison.OrdinalIgnoreCase);
        bool isWithdraw = string.Equals(skillId, "withdraw-chest", StringComparison.OrdinalIgnoreCase);
        bool isHoe = string.Equals(skillId, "hoe-tiles", StringComparison.OrdinalIgnoreCase);
        bool isPlant = string.Equals(skillId, "plant-seeds", StringComparison.OrdinalIgnoreCase);
        bool isShip = string.Equals(skillId, "ship-items", StringComparison.OrdinalIgnoreCase);

        if (isWater || isHarvest || isHoe || isPlant)
        {
            if (payload.Parameters.Tiles == null || payload.Parameters.Tiles.Count == 0)
            {
                error = "Tiles list cannot be empty.";
                return false;
            }

            if (payload.Parameters.Tiles.Count > 100)
            {
                error = "Tiles list exceeds maximum allowed batch size of 100 tiles.";
                return false;
            }

            if (isPlant && string.IsNullOrWhiteSpace(payload.Parameters.SeedItemId))
            {
                error = "SeedItemId is required for plant-seeds.";
                return false;
            }
        }
        else if (isDeposit || isOrganize || isWithdraw)
        {
            if (payload.Parameters.ChestTile is null)
            {
                error = "ChestTile is required for chest skills.";
                return false;
            }

            if (isWithdraw)
            {
                if ((payload.Parameters.Items is null || payload.Parameters.Items.Count == 0) &&
                    string.IsNullOrWhiteSpace(payload.Parameters.SeedItemId))
                {
                    error = "Items list (or seedItemId) is required for withdraw-chest.";
                    return false;
                }

                if (payload.Parameters.Items is not null)
                {
                    if (payload.Parameters.Items.Count > 36)
                    {
                        error = "Items list exceeds maximum allowed size of 36.";
                        return false;
                    }

                    if (payload.Parameters.Items.Any(i => string.IsNullOrWhiteSpace(i.ItemId) || i.Count < 1))
                    {
                        error = "Items entries must specify non-empty itemId and count >= 1.";
                        return false;
                    }
                }
            }
            else if (isDeposit && payload.Parameters.ItemIds is not null)
            {
                if (payload.Parameters.ItemIds.Count > 36)
                {
                    error = "ItemIds list exceeds maximum allowed size of 36.";
                    return false;
                }

                if (payload.Parameters.ItemIds.Any(string.IsNullOrWhiteSpace))
                {
                    error = "ItemIds must be non-empty strings.";
                    return false;
                }
            }
        }
        else if (isShip)
        {
            if (!string.Equals(payload.Parameters.LocationId, "Farm", StringComparison.OrdinalIgnoreCase))
            {
                error = "Shipping is only supported on the 'Farm' location.";
                return false;
            }

            if (payload.Parameters.Items == null || payload.Parameters.Items.Count == 0)
            {
                error = "Items list cannot be empty.";
                return false;
            }

            if (payload.Parameters.Items.Count > 36)
            {
                error = "Items list exceeds maximum allowed batch size of 36.";
                return false;
            }

            if (payload.Parameters.Items.Any(i => string.IsNullOrWhiteSpace(i.ItemId) || i.Count < 1))
            {
                error = "Items entries must specify non-empty itemId and count >= 1.";
                return false;
            }
        }
        // Unknown skills pass shape validation here; the handler rejects them as UNSUPPORTED_SKILL.

        bool isNativeTiled = string.Equals(skillId, "refill-watering-can", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(skillId, "apply-fertilizer", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(skillId, "clear-debris", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(skillId, "pickup-items", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(skillId, "collect-machine", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(skillId, "pet-animal", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(skillId, "collect-animal-produce", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(skillId, "toggle-animal-door", StringComparison.OrdinalIgnoreCase);

        if (isNativeTiled)
        {
            if (payload.Parameters.Tiles == null || payload.Parameters.Tiles.Count == 0)
            {
                error = $"Tiles list cannot be empty for {skillId}.";
                return false;
            }

            if (payload.Parameters.Tiles.Count > 100)
            {
                error = "Tiles list exceeds maximum allowed batch size of 100 tiles.";
                return false;
            }

            if (string.Equals(skillId, "apply-fertilizer", StringComparison.OrdinalIgnoreCase) &&
                string.IsNullOrWhiteSpace(payload.Parameters.FertilizerItemId))
            {
                error = "FertilizerItemId is required for apply-fertilizer.";
                return false;
            }

            if ((string.Equals(skillId, "pet-animal", StringComparison.OrdinalIgnoreCase) ||
                 string.Equals(skillId, "collect-animal-produce", StringComparison.OrdinalIgnoreCase)) &&
                string.IsNullOrWhiteSpace(payload.Parameters.AnimalName))
            {
                error = $"AnimalName is required for {skillId}.";
                return false;
            }
        }
        else if (string.Equals(skillId, "insert-machine", StringComparison.OrdinalIgnoreCase))
        {
            if (payload.Parameters.Tile == null)
            {
                error = "Tile is required for insert-machine.";
                return false;
            }
            if (string.IsNullOrWhiteSpace(payload.Parameters.ItemId))
            {
                error = "ItemId is required for insert-machine.";
                return false;
            }
            if (payload.Parameters.ItemCount is < 1 or > 36)
            {
                error = "ItemCount for insert-machine must be between 1 and 36.";
                return false;
            }
        }
        else if (string.Equals(skillId, "feed-animals", StringComparison.OrdinalIgnoreCase))
        {
            if (string.IsNullOrWhiteSpace(payload.Parameters.BuildingName))
            {
                error = "BuildingName (animal building interior) is required for feed-animals.";
                return false;
            }
        }

        if (payload.Budgets == null)
        {
            error = "Budgets object is required.";
            return false;
        }

        if (payload.Budgets.MaxGameMinutes is < 1 or > 1440)
        {
            error = "MaxGameMinutes must be between 1 and 1440.";
            return false;
        }

        if (payload.Budgets.MaxStamina < 0f)
        {
            error = "MaxStamina must be non-negative.";
            return false;
        }

        if (payload.Budgets.MaxWater < 0)
        {
            error = "MaxWater must be non-negative.";
            return false;
        }

        error = null;
        return true;
    }

    private void HandleSkillCancel(EnvelopeDto envelope)
    {
        SkillCancelPayload? payload;
        try
        {
            payload = envelope.Payload.Deserialize<SkillCancelPayload>(JsonOptions);
        }
        catch (Exception ex)
        {
            _logger?.Invoke($"Failed to parse skill.cancel: {ex.Message}", "Warn");
            return;
        }

        if (payload != null)
        {
            if (string.Equals(ActiveCommandId, payload.CommandId, StringComparison.Ordinal))
            {
                _handler.OnCancelSkill(payload, envelope);
            }
            else
            {
                // Command was purged before execution or never started; emit terminal cancelled result
                var cancelledResult = new SkillResultPayload(
                    CommandId: payload.CommandId,
                    TaskId: "task-cancelled",
                    TerminalState: "cancelled",
                    CompletedCount: 0,
                    SkippedCount: 0,
                    FailedCount: 0,
                    FinalWorldRevision: envelope.WorldRevision,
                    Effects: new List<Dictionary<string, object>>(),
                    Resources: null,
                    Error: new SkillResultError("CANCELLED_BEFORE_EXECUTE", "Command was cancelled before execution started.", null, false)
                );
                _ = SendSkillResultAsync(cancelledResult, envelope.MessageId, idempotencyKey: null);
                _handler.OnCancelSkill(payload, envelope);
            }
        }
    }

    private void HandleSkillPause(EnvelopeDto envelope)
    {
        SkillPausePayload? payload;
        try
        {
            payload = envelope.Payload.Deserialize<SkillPausePayload>(JsonOptions);
        }
        catch (Exception ex)
        {
            _logger?.Invoke($"Failed to parse skill.pause: {ex.Message}", "Warn");
            return;
        }

        if (payload != null)
        {
            _handler.OnPauseSkill(payload, envelope);
        }
    }

    private void HandleSkillResume(EnvelopeDto envelope)
    {
        SkillResumePayload? payload;
        try
        {
            payload = envelope.Payload.Deserialize<SkillResumePayload>(JsonOptions);
        }
        catch (Exception ex)
        {
            _logger?.Invoke($"Failed to parse skill.resume: {ex.Message}", "Warn");
            return;
        }

        if (payload != null)
        {
            _handler.OnResumeSkill(payload, envelope);
        }
    }

    private void SendTerminalRejection(EnvelopeDto request, string code, string message, bool recordIdempotency)
    {
        string commandId = "unknown";
        string taskId = "unknown";
        string? skillId = null;
        if (request.Payload.TryGetPropertyValue("commandId", out var cNode) && cNode != null)
        {
            commandId = cNode.ToString();
        }
        if (request.Payload.TryGetPropertyValue("taskId", out var tNode) && tNode != null)
        {
            taskId = tNode.ToString();
        }
        if (request.Payload.TryGetPropertyValue("skillId", out var sNode) && sNode != null)
        {
            skillId = sNode.ToString();
        }

        var rejectResult = new SkillResultPayload(
            CommandId: commandId,
            TaskId: taskId,
            TerminalState: "rejected",
            CompletedCount: 0,
            SkippedCount: 0,
            FailedCount: 0,
            FinalWorldRevision: request.WorldRevision,
            Effects: new List<Dictionary<string, object>>(),
            Resources: null,
            Error: new SkillResultError(code, message, null, false),
            SkillId: skillId
        );

        string? key = recordIdempotency ? request.IdempotencyKey : null;
        _ = SendSkillResultAsync(rejectResult, request.MessageId, key);
    }

    public Task SendSnapshotAsync(WorldSnapshotPayload snapshot, long worldRevision)
    {
        ArgumentNullException.ThrowIfNull(snapshot);
        lock (_chatLock)
        {
            _lastSnapshot = snapshot;
            _lastSnapshotRevision = worldRevision;
        }
        var payloadNode = JsonSerializer.SerializeToNode(snapshot, JsonOptions)!.AsObject();
        var envelope = CreateEnvelope("world.snapshot", payloadNode, correlationId: null, worldRevision: worldRevision);
        EnqueueOutbound(envelope, isCritical: true, _socketGeneration);
        _ = SendChatSnapshotAsync(snapshot, worldRevision);
        return Task.CompletedTask;
    }

    private async Task<bool> SendChatSnapshotAsync(WorldSnapshotPayload snapshot, long worldRevision)
    {
        WebSocket? socket;
        lock (_chatLock) { socket = _chatSocket; }
        if (socket == null || socket.State != WebSocketState.Open) return false;
        var envelope = CreateEnvelope("world.snapshot", JsonSerializer.SerializeToNode(snapshot, JsonOptions)!.AsObject(), worldRevision: worldRevision);
        var bytes = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(envelope, JsonOptions));
        using var timeoutCts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await _chatSendLock.WaitAsync(timeoutCts.Token).ConfigureAwait(false);
        try
        {
            if (socket.State != WebSocketState.Open) return false;
            await socket.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, timeoutCts.Token).ConfigureAwait(false);
            return true;
        }
        finally { _chatSendLock.Release(); }
    }

    public Task SendSkillResultAsync(SkillResultPayload result, string correlationId, string? idempotencyKey = null)
    {
        ArgumentNullException.ThrowIfNull(result);

        if (idempotencyKey != null)
        {
            _idempotencyManager.MarkCompleted(idempotencyKey, result);
        }

        if (string.Equals(ActiveCommandId, result.CommandId, StringComparison.Ordinal))
        {
            ActiveCommandId = null;
        }

        var payloadNode = JsonSerializer.SerializeToNode(result, JsonOptions)!.AsObject();
        var envelope = CreateEnvelope(
            messageType: "skill.result",
            payload: payloadNode,
            correlationId: correlationId,
            worldRevision: result.FinalWorldRevision,
            idempotencyKey: idempotencyKey
        );

        EnqueueOutbound(envelope, isCritical: true, _socketGeneration);
        return Task.CompletedTask;
    }

    public Task SendProtocolErrorAsync(string code, string message, string? correlationId = null)
    {
        var errPayload = new ProtocolErrorPayload(code, message);
        var payloadNode = JsonSerializer.SerializeToNode(errPayload, JsonOptions)!.AsObject();
        var envelope = CreateEnvelope("protocol.error", payloadNode, correlationId);
        EnqueueOutbound(envelope, isCritical: true, _socketGeneration);
        return Task.CompletedTask;
    }

    private EnvelopeDto CreateEnvelope(
        string messageType,
        JsonObject payload,
        string? correlationId = null,
        long worldRevision = 1,
        string? idempotencyKey = null)
    {
        return new EnvelopeDto(
            ProtocolVersion: "0.1",
            MessageType: messageType,
            MessageId: $"msg-{Guid.NewGuid():N}"[..16],
            SenderInstanceId: _senderInstanceId,
            SequenceNumber: 0,
            WorldRevision: worldRevision,
            SentAt: DateTimeOffset.UtcNow,
            Payload: payload,
            CorrelationId: correlationId,
            SaveId: _saveId,
            GameSessionId: _gameSessionId,
            IdempotencyKey: idempotencyKey
        );
    }

    private static int GetAvailablePort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        try
        {
            listener.Start();
            return ((IPEndPoint)listener.LocalEndpoint).Port;
        }
        finally
        {
            listener.Stop();
        }
    }

    private static string GenerateSecureToken()
    {
        byte[] bytes = new byte[32];
        RandomNumberGenerator.Fill(bytes);
        return Convert.ToHexString(bytes).ToLowerInvariant();
    }

    private async Task HandleChatConnectionAsync(HttpListenerContext context, CancellationToken ct)
    {
        _logger?.Invoke($"Accepting Chat WebSocket connection from {context.Request.RemoteEndPoint}...", "Info");
        var wsContext = await context.AcceptWebSocketAsync(subProtocol: null).ConfigureAwait(false);
        long gen = Interlocked.Increment(ref _chatSocketGeneration);

        lock (_chatLock)
        {
            _chatSocketCts?.Cancel();
            _chatSocketCts?.Dispose();
            _chatSocketCts = new CancellationTokenSource();
            _chatSocket = wsContext.WebSocket;
        }

        _logger?.Invoke($"Chat bridge connected (generation {gen}) from {context.Request.RemoteEndPoint}.", "Info");
        WorldSnapshotPayload? snapshot;
        long snapshotRevision;
        lock (_chatLock)
        {
            snapshot = _lastSnapshot;
            snapshotRevision = _lastSnapshotRevision;
        }
        if (snapshot != null)
        {
            _ = SendChatSnapshotAsync(snapshot, snapshotRevision);
        }
        var combinedCts = CancellationTokenSource.CreateLinkedTokenSource(ct, _chatSocketCts.Token);
        _ = Task.Run(() => ReceiveChatLoopAsync(wsContext.WebSocket, gen, combinedCts.Token), combinedCts.Token);
    }

    private async Task ReceiveChatLoopAsync(WebSocket socket, long gen, CancellationToken ct)
    {
        var buffer = new byte[4096];
        using var ms = new MemoryStream();
        _logger?.Invoke($"ReceiveChatLoop started for generation {gen}.", "Trace");

        try
        {
            while (!ct.IsCancellationRequested && socket.State == WebSocketState.Open)
            {
                ms.SetLength(0);
                WebSocketReceiveResult result;
                do
                {
                    result = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), ct).ConfigureAwait(false);
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        _logger?.Invoke($"Chat client initiated close in generation {gen}.", "Info");
                        try
                        {
                            if (socket.State == WebSocketState.CloseReceived)
                            {
                                await socket.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "Closing", CancellationToken.None).ConfigureAwait(false);
                            }
                        }
                        catch { }
                        return;
                    }
                    ms.Write(buffer, 0, result.Count);
                }
                while (!result.EndOfMessage);

                if (result.MessageType == WebSocketMessageType.Text)
                {
                    var jsonString = Encoding.UTF8.GetString(ms.GetBuffer(), 0, (int)ms.Length);
                    try
                    {
                        using var doc = JsonDocument.Parse(jsonString);
                        var root = doc.RootElement;
                        ChatReplyPayload? reply = null;
                        if (root.TryGetProperty("messageType", out var msgTypeProp) &&
                            string.Equals(msgTypeProp.GetString(), "autonomy.state", StringComparison.OrdinalIgnoreCase) &&
                            root.TryGetProperty("payload", out var autonomyElem))
                        {
                            var state = autonomyElem.Deserialize<AutonomyStatePayload>(JsonOptions);
                            if (state != null)
                            {
                                OnAutonomyStateReceived?.Invoke(state);
                            }
                        }
                        else if (root.TryGetProperty("messageType", out msgTypeProp) &&
                            string.Equals(msgTypeProp.GetString(), "chat.reply", StringComparison.OrdinalIgnoreCase) &&
                            root.TryGetProperty("payload", out var payloadElem))
                        {
                            reply = payloadElem.Deserialize<ChatReplyPayload>(JsonOptions);
                        }
                        else
                        {
                            reply = JsonSerializer.Deserialize<ChatReplyPayload>(jsonString, JsonOptions);
                        }

                        if (reply != null)
                        {
                            lock (_chatLock)
                            {
                                _pendingChatReplies.Enqueue(reply);
                            }
                        }
                    }
                    catch (Exception ex)
                    {
                        _logger?.Invoke($"Error parsing chat reply: {ex.Message}", "Warn");
                        string? requestId = null, saveId = null;
                        try
                        {
                            using var failed = JsonDocument.Parse(jsonString);
                            var payload = failed.RootElement.TryGetProperty("payload", out var part) ? part : failed.RootElement;
                            if (payload.TryGetProperty("requestId", out var id) && id.ValueKind == JsonValueKind.String) requestId = id.GetString();
                            if (payload.TryGetProperty("saveId", out var save) && save.ValueKind == JsonValueKind.String) saveId = save.GetString();
                        }
                        catch (JsonException) { }
                        OnChatChannelProblem?.Invoke(requestId, saveId, $"回复解析失败：{ex.Message}。请更新配套版本后重试；动作结果未确认。");
                    }
                }
            }
        }
        catch (Exception ex)
        {
            _logger?.Invoke($"ReceiveChatLoop exited: {ex.Message}", "Trace");
        }
        finally
        {
            lock (_chatLock)
            {
                if (_chatSocket == socket)
                {
                    _chatSocket = null;
                    OnChatChannelProblem?.Invoke(null, null, "伙伴连接已断开，请恢复服务后重试；动作结果未确认。");
                }
            }
        }
    }

    public async Task<bool> SendChatSubmitAsync(ChatSubmitPayload payload)
    {
        WebSocket? socket;
        lock (_chatLock)
        {
            socket = _chatSocket;
        }

        if (socket == null || socket.State != WebSocketState.Open)
        {
            return false;
        }

        var envelope = new EnvelopeDto(
            ProtocolVersion: "0.1",
            MessageType: "chat.submit",
            MessageId: $"chat-{Guid.NewGuid():N}"[..12],
            SenderInstanceId: _senderInstanceId,
            SequenceNumber: 0,
            WorldRevision: 0,
            SentAt: DateTimeOffset.UtcNow,
            Payload: JsonSerializer.SerializeToNode(payload, JsonOptions)!.AsObject(),
            SaveId: _saveId,
            GameSessionId: _gameSessionId
        );

        var json = JsonSerializer.Serialize(envelope, JsonOptions);
        var bytes = Encoding.UTF8.GetBytes(json);
        using var timeoutCts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await _chatSendLock.WaitAsync(timeoutCts.Token).ConfigureAwait(false);
        try
        {
            if (socket.State != WebSocketState.Open) return false;
            await socket.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, timeoutCts.Token).ConfigureAwait(false);
            return true;
        }
        finally { _chatSendLock.Release(); }
    }

    public async Task<bool> SendAutonomyControlAsync(AutonomyControlPayload payload)
    {
        WebSocket? socket;
        lock (_chatLock) socket = _chatSocket;
        if (socket == null || socket.State != WebSocketState.Open) return false;
        var envelope = new EnvelopeDto(
            "0.1", "autonomy.control", $"autonomy-{Guid.NewGuid():N}"[..12],
            _senderInstanceId, 0, 0, DateTimeOffset.UtcNow,
            JsonSerializer.SerializeToNode(payload, JsonOptions)!.AsObject(),
            SaveId: payload.SaveId, GameSessionId: _gameSessionId);
        var bytes = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(envelope, JsonOptions));
        using var timeoutCts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await _chatSendLock.WaitAsync(timeoutCts.Token).ConfigureAwait(false);
        try
        {
            if (socket.State != WebSocketState.Open) return false;
            await socket.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, timeoutCts.Token).ConfigureAwait(false);
            return true;
        }
        finally { _chatSendLock.Release(); }
    }

    public async Task<bool> SendChatCancelAsync(ChatCancelPayload payload)
    {
        WebSocket? socket;
        lock (_chatLock)
        {
            socket = _chatSocket;
        }

        if (socket == null || socket.State != WebSocketState.Open)
        {
            return false;
        }

        var envelope = new EnvelopeDto(
            ProtocolVersion: "0.1",
            MessageType: "chat.cancel",
            MessageId: $"cancel-{Guid.NewGuid():N}"[..12],
            SenderInstanceId: _senderInstanceId,
            SequenceNumber: 0,
            WorldRevision: 0,
            SentAt: DateTimeOffset.UtcNow,
            Payload: JsonSerializer.SerializeToNode(payload, JsonOptions)!.AsObject(),
            SaveId: _saveId,
            GameSessionId: _gameSessionId
        );

        var json = JsonSerializer.Serialize(envelope, JsonOptions);
        var bytes = Encoding.UTF8.GetBytes(json);
        using var timeoutCts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await _chatSendLock.WaitAsync(timeoutCts.Token).ConfigureAwait(false);
        try
        {
            if (socket.State != WebSocketState.Open) return false;
            await socket.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, timeoutCts.Token).ConfigureAwait(false);
            return true;
        }
        finally { _chatSendLock.Release(); }
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        Stop();
        _serverCts?.Dispose();
        _socketCts?.Dispose();
        _outboundSignal.Dispose();
        _listener.Close();
    }
}
