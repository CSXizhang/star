using System.Diagnostics;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Xunit;

namespace StardewAI.Companion.Mod.Transport.Tests;

public class WebSocketTransportServerTests
{
    private sealed class TestTransportHandler : ITransportHandler
    {
        public int AcceptCount { get; private set; }
        public int CancelCount { get; private set; }
        public int PauseCount { get; private set; }
        public int ResumeCount { get; private set; }
        public int DisconnectCount { get; private set; }

        public int LastAcceptThreadId { get; private set; }
        public int LastDisconnectThreadId { get; private set; }

        public List<SkillExecutePayload> AcceptedPayloads { get; } = new();
        public List<SkillCancelPayload> CancelPayloads { get; } = new();
        public string? DisconnectReason { get; private set; }

        public bool AcceptShouldSucceed { get; set; } = true;
        public SkillResultPayload? CustomRejectResult { get; set; }
        public string? ActiveCommand { get; set; }
        public bool WasActiveCommandCancelled { get; set; }

        public bool TryAcceptSkillExecute(SkillExecutePayload payload, EnvelopeDto envelope, out SkillResultPayload? rejectResult)
        {
            AcceptCount++;
            LastAcceptThreadId = Environment.CurrentManagedThreadId;
            AcceptedPayloads.Add(payload);

            if (AcceptShouldSucceed)
            {
                ActiveCommand = payload.CommandId;
                rejectResult = null;
                return true;
            }

            rejectResult = CustomRejectResult ?? new SkillResultPayload(
                CommandId: payload.CommandId,
                TaskId: payload.TaskId,
                TerminalState: "rejected",
                CompletedCount: 0,
                SkippedCount: 0,
                FailedCount: 0,
                FinalWorldRevision: envelope.WorldRevision,
                Effects: new List<Dictionary<string, object>>()
            );
            return false;
        }

        public void OnCancelSkill(SkillCancelPayload payload, EnvelopeDto envelope)
        {
            CancelCount++;
            CancelPayloads.Add(payload);
            if (ActiveCommand == payload.CommandId)
            {
                WasActiveCommandCancelled = true;
                ActiveCommand = null;
            }
        }

        public void OnPauseSkill(SkillPausePayload payload, EnvelopeDto envelope)
        {
            PauseCount++;
        }

        public void OnResumeSkill(SkillResumePayload payload, EnvelopeDto envelope)
        {
            ResumeCount++;
        }

        public WorldSnapshotPayload CaptureCurrentSnapshot(long worldRevision)
        {
            return new WorldSnapshotPayload(
                CapturedRevision: worldRevision,
                Companion: new CompanionSnapshot(
                    LocationId: "Farm",
                    TileX: 64,
                    TileY: 15,
                    FacingDirection: 2,
                    Stamina: 270.0f,
                    MaxStamina: 270,
                    WaterCanLevel: 40,
                    MaxWaterCanLevel: 40,
                    HasWateringCan: true,
                    Activity: "idle"
                ),
                World: new WorldStateSnapshot(
                    CurrentLocation: "Farm",
                    TimeOfDay: 600,
                    Season: "spring",
                    DayOfMonth: 1,
                    IsRaining: false
                )
            );
        }

        public void OnTransportDisconnected(string reason)
        {
            DisconnectCount++;
            LastDisconnectThreadId = Environment.CurrentManagedThreadId;
            DisconnectReason = reason;
            if (ActiveCommand != null)
            {
                WasActiveCommandCancelled = true;
                ActiveCommand = null;
            }
        }
    }

    private static async Task<ClientWebSocket> ConnectClientAsync(int port, string token, bool useHeader = true)
    {
        var client = new ClientWebSocket();
        if (useHeader)
        {
            client.Options.SetRequestHeader("Authorization", $"Bearer {token}");
        }

        string uri = useHeader
            ? $"ws://127.0.0.1:{port}/"
            : $"ws://127.0.0.1:{port}/?token={token}";

        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await client.ConnectAsync(new Uri(uri), cts.Token);
        return client;
    }

    private static async Task SendRawJsonAsync(ClientWebSocket ws, string json)
    {
        var bytes = Encoding.UTF8.GetBytes(json);
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, cts.Token);
    }

    private static async Task SendAndDrainAsync(ClientWebSocket ws, WebSocketTransportServer server, string json, int delayMs = 60)
    {
        await SendRawJsonAsync(ws, json);
        await Task.Delay(delayMs);
        server.Update();
    }

    private static async Task<EnvelopeDto> ReceiveEnvelopeAsync(ClientWebSocket ws)
    {
        var buffer = new byte[4096];
        using var ms = new MemoryStream();
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));

        while (true)
        {
            var res = await ws.ReceiveAsync(new ArraySegment<byte>(buffer), cts.Token);
            ms.Write(buffer, 0, res.Count);
            if (res.EndOfMessage)
            {
                break;
            }
        }

        var json = Encoding.UTF8.GetString(ms.GetBuffer(), 0, (int)ms.Length);
        var envelope = JsonSerializer.Deserialize<EnvelopeDto>(json, WebSocketTransportServer.JsonOptions);
        Assert.NotNull(envelope);
        return envelope;
    }

    private static string CreateHelloJson(string token)
    {
        return JsonSerializer.Serialize(new
        {
            protocolVersion = "0.1",
            messageType = "runtime.hello",
            messageId = "msg-hello-1",
            senderInstanceId = "runtime-test",
            sequenceNumber = 0,
            worldRevision = 0,
            sentAt = DateTimeOffset.UtcNow.ToString("O"),
            payload = new
            {
                supportedProtocolVersions = new[] { "0.1" },
                runtimeVersion = "0.1.0",
                sessionToken = token,
                features = new Dictionary<string, bool>()
            }
        });
    }

    private static string CreateExecuteJson(
        string commandId,
        string taskId,
        string idempotencyKey,
        List<TileCoord>? tiles = null,
        DateTimeOffset? expiresAt = null)
    {
        return JsonSerializer.Serialize(new
        {
            protocolVersion = "0.1",
            messageType = "skill.execute",
            messageId = $"msg-exec-{commandId}",
            saveId = "save-123",
            gameSessionId = "session-456",
            senderInstanceId = "runtime-test",
            sequenceNumber = 1,
            worldRevision = 1,
            sentAt = DateTimeOffset.UtcNow.ToString("O"),
            expiresAt = (expiresAt ?? DateTimeOffset.UtcNow.AddMinutes(5)).ToString("O"),
            idempotencyKey = idempotencyKey,
            payload = new
            {
                commandId = commandId,
                taskId = taskId,
                skillId = "water-zone",
                skillVersion = "0.1",
                expectedWorldRevision = 1,
                parameters = new
                {
                    locationId = "Farm",
                    tiles = tiles ?? new List<TileCoord> { new(64, 15), new(64, 16) }
                },
                budgets = new
                {
                    maxGameMinutes = 60,
                    maxStamina = 50.0f,
                    maxWater = 20
                },
                cancelPolicy = "safe-point",
                policyDecisionId = "policy-allow"
            }
        });
    }

    [Fact]
    public async Task UnauthorizedConnection_RejectedWith401()
    {
        var handler = new TestTransportHandler();
        using var server = new WebSocketTransportServer(0, "valid-secret", handler);
        server.Start();

        var client = new ClientWebSocket();
        client.Options.SetRequestHeader("Authorization", "Bearer wrong-secret");

        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(3));
        await Assert.ThrowsAnyAsync<WebSocketException>(async () =>
        {
            await client.ConnectAsync(new Uri($"ws://127.0.0.1:{server.Port}/"), cts.Token);
        });
    }

    [Fact]
    public async Task HandshakeAndSnapshot_FullFlow_PreservesSaveAndSessionIds()
    {
        var handler = new TestTransportHandler();
        using var server = new WebSocketTransportServer(0, "test-token", handler, saveId: "save-123", gameSessionId: "session-456");
        server.Start();

        using var client = await ConnectClientAsync(server.Port, "test-token");

        // Send hello
        await SendRawJsonAsync(client, CreateHelloJson("test-token"));

        // Receive mod.welcome
        var welcome = await ReceiveEnvelopeAsync(client);
        Assert.Equal("mod.welcome", welcome.MessageType);
        Assert.Equal("save-123", welcome.SaveId);
        Assert.Equal("session-456", welcome.GameSessionId);

        // Server update triggers snapshot
        server.Update();

        var snapshot = await ReceiveEnvelopeAsync(client);
        Assert.Equal("world.snapshot", snapshot.MessageType);
        Assert.Equal("save-123", snapshot.SaveId);
        Assert.Equal("session-456", snapshot.GameSessionId);
        Assert.Equal(TransportState.Ready, server.State);
    }

    [Fact]
    public async Task MaliciousMissingPayload_FailsClosedWithoutCrashing()
    {
        var handler = new TestTransportHandler();
        using var server = new WebSocketTransportServer(0, "test-token", handler, saveId: "save-123", gameSessionId: "session-456");
        server.Start();

        using var client = await ConnectClientAsync(server.Port, "test-token");
        await SendRawJsonAsync(client, CreateHelloJson("test-token"));
        await ReceiveEnvelopeAsync(client); // welcome
        server.Update();
        await ReceiveEnvelopeAsync(client); // snapshot

        // 1. Send malformed JSON
        await SendRawJsonAsync(client, "{ not valid json !!! }");
        var err1 = await ReceiveEnvelopeAsync(client);
        Assert.Equal("protocol.error", err1.MessageType);
        Assert.Equal("MALFORMED_JSON", err1.Payload["code"]?.ToString());

        // 2. Send skill.execute with missing idempotencyKey (must NOT be synthesized)
        var missingIdemJson = JsonSerializer.Serialize(new
        {
            protocolVersion = "0.1",
            messageType = "skill.execute",
            messageId = "msg-missing-idem",
            saveId = "save-123",
            gameSessionId = "session-456",
            senderInstanceId = "runtime-test",
            sequenceNumber = 2,
            worldRevision = 1,
            sentAt = DateTimeOffset.UtcNow.ToString("O"),
            payload = new
            {
                commandId = "cmd-1",
                taskId = "task-1"
            }
        });
        await SendAndDrainAsync(client, server, missingIdemJson);

        var reject1 = await ReceiveEnvelopeAsync(client);
        Assert.Equal("skill.result", reject1.MessageType);
        Assert.Equal("rejected", reject1.Payload["terminalState"]?.ToString());
        Assert.Equal("MISSING_IDEMPOTENCY_KEY", reject1.Payload["error"]?["code"]?.ToString());

        // 3. Send skill.execute with 101 tiles (exceeds 100 max)
        var manyTiles = Enumerable.Range(0, 101).Select(i => new TileCoord(i, i)).ToList();
        await SendAndDrainAsync(client, server, CreateExecuteJson("cmd-2", "task-2", "idem-2", tiles: manyTiles));

        var reject2 = await ReceiveEnvelopeAsync(client);
        Assert.Equal("skill.result", reject2.MessageType);
        Assert.Equal("rejected", reject2.Payload["terminalState"]?.ToString());
        Assert.Equal("BOUNDS_VIOLATION", reject2.Payload["error"]?["code"]?.ToString());
    }

    [Fact]
    public async Task DuplicateAndReplay_PreservesOriginalResultWithoutReexecution()
    {
        var handler = new TestTransportHandler();
        using var server = new WebSocketTransportServer(0, "test-token", handler, saveId: "save-123", gameSessionId: "session-456");
        server.Start();

        using var client = await ConnectClientAsync(server.Port, "test-token");
        await SendRawJsonAsync(client, CreateHelloJson("test-token"));
        await ReceiveEnvelopeAsync(client); // welcome
        server.Update();
        await ReceiveEnvelopeAsync(client); // snapshot

        // 1. Submit execute
        await SendAndDrainAsync(client, server, CreateExecuteJson("cmd-water-1", "task-1", "idem-water-1"));

        Assert.Equal(1, handler.AcceptCount);
        Assert.Equal("cmd-water-1", server.ActiveCommandId);

        // 2. Complete execution
        var resultPayload = new SkillResultPayload(
            CommandId: "cmd-water-1",
            TaskId: "task-1",
            TerminalState: "succeeded",
            CompletedCount: 2,
            SkippedCount: 0,
            FailedCount: 0,
            FinalWorldRevision: 2,
            Effects: new List<Dictionary<string, object>>(),
            Resources: new SkillResultResources(4.0f, 2, 10)
        );
        await server.SendSkillResultAsync(resultPayload, correlationId: "msg-exec-cmd-water-1", idempotencyKey: "idem-water-1");

        var firstResult = await ReceiveEnvelopeAsync(client);
        Assert.Equal("skill.result", firstResult.MessageType);
        Assert.Equal("succeeded", firstResult.Payload["terminalState"]?.ToString());
        Assert.Null(server.ActiveCommandId);

        // 3. Resend EXACT DUPLICATE execute
        await SendAndDrainAsync(client, server, CreateExecuteJson("cmd-water-1", "task-1", "idem-water-1"));

        // Handler must NOT have been called again!
        Assert.Equal(1, handler.AcceptCount);

        // Replayed result received
        var replayedResult = await ReceiveEnvelopeAsync(client);
        Assert.Equal("skill.result", replayedResult.MessageType);
        Assert.Equal("succeeded", replayedResult.Payload["terminalState"]?.ToString());
        Assert.Equal("cmd-water-1", replayedResult.Payload["commandId"]?.ToString());
    }

    [Fact]
    public async Task ConflictRejection_DoesNotMutateOriginalRecordOrActiveExecution()
    {
        var handler = new TestTransportHandler();
        using var server = new WebSocketTransportServer(0, "test-token", handler, saveId: "save-123", gameSessionId: "session-456");
        server.Start();

        using var client = await ConnectClientAsync(server.Port, "test-token");
        await SendRawJsonAsync(client, CreateHelloJson("test-token"));
        await ReceiveEnvelopeAsync(client); // welcome
        server.Update();
        await ReceiveEnvelopeAsync(client); // snapshot

        // 1. Start execution A with key 'shared-key'
        await SendAndDrainAsync(client, server, CreateExecuteJson("cmd-A", "task-A", "shared-key", tiles: new List<TileCoord> { new(1, 1) }));

        Assert.Equal(1, handler.AcceptCount);
        Assert.Equal("cmd-A", server.ActiveCommandId);

        // 2. Submit conflicting execution B with SAME 'shared-key' but DIFFERENT commandId and tiles!
        await SendAndDrainAsync(client, server, CreateExecuteJson("cmd-B", "task-B", "shared-key", tiles: new List<TileCoord> { new(9, 9) }));

        // Must reject B with IDEMPOTENCY_CONFLICT
        var conflictReject = await ReceiveEnvelopeAsync(client);
        Assert.Equal("skill.result", conflictReject.MessageType);
        Assert.Equal("rejected", conflictReject.Payload["terminalState"]?.ToString());
        Assert.Equal("IDEMPOTENCY_CONFLICT", conflictReject.Payload["error"]?["code"]?.ToString());

        // CRITICAL INVARIANCE: Command A must STILL be active and untouched!
        Assert.Equal("cmd-A", server.ActiveCommandId);
        Assert.Equal(1, handler.AcceptCount);

        // 3. Complete Command A
        var resultA = new SkillResultPayload(
            CommandId: "cmd-A",
            TaskId: "task-A",
            TerminalState: "succeeded",
            CompletedCount: 1,
            SkippedCount: 0,
            FailedCount: 0,
            FinalWorldRevision: 3,
            Effects: new List<Dictionary<string, object>>()
        );
        await server.SendSkillResultAsync(resultA, correlationId: "msg-A", idempotencyKey: "shared-key");

        var resultEnvelope = await ReceiveEnvelopeAsync(client);
        Assert.Equal("cmd-A", resultEnvelope.Payload["commandId"]?.ToString());
        Assert.Equal("succeeded", resultEnvelope.Payload["terminalState"]?.ToString());
    }

    [Fact]
    public async Task CancelBeforeExecute_PurgesQueuedCommandAndEmitsCancelledResult()
    {
        var handler = new TestTransportHandler();
        using var server = new WebSocketTransportServer(0, "test-token", handler, saveId: "save-123", gameSessionId: "session-456");
        server.Start();

        using var client = await ConnectClientAsync(server.Port, "test-token");
        await SendRawJsonAsync(client, CreateHelloJson("test-token"));
        await ReceiveEnvelopeAsync(client); // welcome
        server.Update();
        await ReceiveEnvelopeAsync(client); // snapshot

        // 1. Send execute for cmd-cancel-target
        await SendRawJsonAsync(client, CreateExecuteJson("cmd-cancel-target", "task-ct", "idem-ct"));

        // 2. IMMEDIATELY send cancel before server.Update() is called!
        var cancelJson = JsonSerializer.Serialize(new
        {
            protocolVersion = "0.1",
            messageType = "skill.cancel",
            messageId = "msg-cancel-1",
            correlationId = "msg-exec-cmd-cancel-target",
            saveId = "save-123",
            gameSessionId = "session-456",
            senderInstanceId = "runtime-test",
            sequenceNumber = 2,
            worldRevision = 1,
            sentAt = DateTimeOffset.UtcNow.ToString("O"),
            payload = new
            {
                commandId = "cmd-cancel-target",
                reason = "User stopped before start",
                requestedByPlayer = true,
                cancelPolicy = "safe-point"
            }
        });
        await SendRawJsonAsync(client, cancelJson);

        // Give network thread a moment to enqueue both
        await Task.Delay(100);

        // 3. Now drain server on the main thread
        server.Update();

        // Handler must NEVER have received execute!
        Assert.Equal(0, handler.AcceptCount);

        // Client must receive cancelled terminal result
        var cancelResult = await ReceiveEnvelopeAsync(client);
        Assert.Equal("skill.result", cancelResult.MessageType);
        Assert.Equal("cancelled", cancelResult.Payload["terminalState"]?.ToString());
        Assert.Equal("cmd-cancel-target", cancelResult.Payload["commandId"]?.ToString());
    }

    [Fact]
    public async Task Disconnect_QueuedToMainThread_ExecutesOnUpdateThread()
    {
        var handler = new TestTransportHandler();
        using var server = new WebSocketTransportServer(0, "test-token", handler, saveId: "save-123", gameSessionId: "session-456");
        server.Start();

        var client = await ConnectClientAsync(server.Port, "test-token");
        await SendRawJsonAsync(client, CreateHelloJson("test-token"));
        await ReceiveEnvelopeAsync(client); // welcome
        server.Update();
        await ReceiveEnvelopeAsync(client); // snapshot

        // Start an active task
        await SendAndDrainAsync(client, server, CreateExecuteJson("cmd-disconnect-task", "task-d", "idem-d"));
        Assert.Equal("cmd-disconnect-task", server.ActiveCommandId);

        // Abruptly close client from client side
        client.Abort();
        client.Dispose();

        // Give network loop time to notice socket closure
        await Task.Delay(100);

        // Before Update() is called, OnTransportDisconnected must NOT have run on the network thread!
        Assert.Equal(0, handler.DisconnectCount);

        // Now invoke Update() on this specific test thread
        int mainThreadId = Environment.CurrentManagedThreadId;
        server.Update();

        // Handler callback MUST have executed on main thread during Update()!
        Assert.Equal(1, handler.DisconnectCount);
        Assert.Equal(mainThreadId, handler.LastDisconnectThreadId);
        Assert.Null(server.ActiveCommandId);
    }

    [Fact]
    public async Task Backpressure_NormalQueueCapacityEnforced_ShedsNormalMessages()
    {
        var handler = new TestTransportHandler();
        using var server = new WebSocketTransportServer(0, "test-token", handler);
        server.Start();

        using var client = await ConnectClientAsync(server.Port, "test-token");
        await SendRawJsonAsync(client, CreateHelloJson("test-token"));
        await ReceiveEnvelopeAsync(client); // welcome
        server.Update();
        await ReceiveEnvelopeAsync(client); // snapshot

        // Rapidly flood 70 pause messages without calling server.Update()
        for (int i = 0; i < 70; i++)
        {
            var pauseJson = JsonSerializer.Serialize(new
            {
                protocolVersion = "0.1",
                messageType = "skill.pause",
                messageId = $"msg-pause-{i}",
                senderInstanceId = "runtime-test",
                sequenceNumber = i + 2,
                worldRevision = 1,
                sentAt = DateTimeOffset.UtcNow.ToString("O"),
                payload = new
                {
                    commandId = "cmd-1",
                    reason = $"Flood pause {i}"
                }
            });
            await SendRawJsonAsync(client, pauseJson);
        }

        // Allow network thread to process
        await Task.Delay(100);

        // Must receive at least one QUEUE_FULL protocol.error
        var errorEnv = await ReceiveEnvelopeAsync(client);
        Assert.Equal("protocol.error", errorEnv.MessageType);
        Assert.Equal("QUEUE_FULL", errorEnv.Payload["code"]?.ToString());
    }

    [Fact]
    public async Task MaliciousExpiredCommand_RejectedBeforeExecution()
    {
        var handler = new TestTransportHandler();
        using var server = new WebSocketTransportServer(0, "test-token", handler, saveId: "save-123", gameSessionId: "session-456");
        server.Start();

        using var client = await ConnectClientAsync(server.Port, "test-token");
        await SendRawJsonAsync(client, CreateHelloJson("test-token"));
        await ReceiveEnvelopeAsync(client); // welcome
        server.Update();
        await ReceiveEnvelopeAsync(client); // snapshot

        // Command expired 1 hour ago
        var pastTime = DateTimeOffset.UtcNow.AddHours(-1);
        await SendAndDrainAsync(client, server, CreateExecuteJson("cmd-expired", "task-exp", "idem-exp", expiresAt: pastTime));

        // Handler must NEVER be called
        Assert.Equal(0, handler.AcceptCount);

        var reject = await ReceiveEnvelopeAsync(client);
        Assert.Equal("skill.result", reject.MessageType);
        Assert.Equal("rejected", reject.Payload["terminalState"]?.ToString());
        Assert.Equal("EXPIRED", reject.Payload["error"]?["code"]?.ToString());
    }

    [Fact]
    public async Task InvalidSaveId_RejectedWithSaveMismatch()
    {
        var handler = new TestTransportHandler();
        using var server = new WebSocketTransportServer(0, "test-token", handler, saveId: "active-save-A", gameSessionId: "session-456");
        server.Start();

        using var client = await ConnectClientAsync(server.Port, "test-token");
        await SendRawJsonAsync(client, CreateHelloJson("test-token"));
        await ReceiveEnvelopeAsync(client); // welcome
        server.Update();
        await ReceiveEnvelopeAsync(client); // snapshot

        // Command targets wrong save
        var wrongSaveJson = JsonSerializer.Serialize(new
        {
            protocolVersion = "0.1",
            messageType = "skill.execute",
            messageId = "msg-wrong-save",
            saveId = "different-save-B",
            gameSessionId = "session-456",
            senderInstanceId = "runtime-test",
            sequenceNumber = 2,
            worldRevision = 1,
            sentAt = DateTimeOffset.UtcNow.ToString("O"),
            expiresAt = DateTimeOffset.UtcNow.AddMinutes(5).ToString("O"),
            idempotencyKey = "idem-wrong-save",
            payload = new
            {
                commandId = "cmd-ws",
                taskId = "task-ws",
                skillId = "water-zone",
                skillVersion = "0.1",
                expectedWorldRevision = 1,
                parameters = new { locationId = "Farm", tiles = new[] { new { x = 1, y = 1 } } },
                budgets = new { maxGameMinutes = 60, maxStamina = 50.0f, maxWater = 20 },
                cancelPolicy = "safe-point",
                policyDecisionId = "policy-allow"
            }
        });
        await SendAndDrainAsync(client, server, wrongSaveJson);

        Assert.Equal(0, handler.AcceptCount);

        var reject = await ReceiveEnvelopeAsync(client);
        Assert.Equal("skill.result", reject.MessageType);
        Assert.Equal("rejected", reject.Payload["terminalState"]?.ToString());
        Assert.Equal("SAVE_MISMATCH", reject.Payload["error"]?["code"]?.ToString());
    }

    [Fact]
    public async Task ReconnectBeforeUpdate_DeliversOldDisconnectAndCancelsActiveTask()
    {
        var handler = new TestTransportHandler();
        using var server = new WebSocketTransportServer(0, "test-token", handler, saveId: "save-123", gameSessionId: "session-456");
        server.Start();

        // 1. Client 1 connects, handshakes, and starts executing cmd-1
        var client1 = await ConnectClientAsync(server.Port, "test-token");
        await SendRawJsonAsync(client1, CreateHelloJson("test-token"));
        await ReceiveEnvelopeAsync(client1); // welcome
        server.Update();
        await ReceiveEnvelopeAsync(client1); // snapshot

        await SendAndDrainAsync(client1, server, CreateExecuteJson("cmd-1", "task-1", "idem-1"));
        Assert.Equal(1, handler.AcceptCount);
        Assert.Equal("cmd-1", handler.ActiveCommand);
        Assert.Equal("cmd-1", server.ActiveCommandId);

        // 2. Client 1 disconnects unexpectedly
        await client1.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "Client 1 done", CancellationToken.None);
        client1.Dispose();

        // Wait briefly for network thread to detect disconnect and schedule it
        await Task.Delay(50);

        // 3. CRUCIAL: Client 2 connects BEFORE server.Update() has run!
        var client2 = await ConnectClientAsync(server.Port, "test-token");
        await SendRawJsonAsync(client2, CreateHelloJson("test-token"));
        await Task.Delay(50);

        // 4. NOW server.Update() runs!
        // It MUST deliver client 1's disconnect first, cancelling cmd-1, before client 2 can begin!
        server.Update();

        Assert.Equal(1, handler.DisconnectCount);
        Assert.True(handler.WasActiveCommandCancelled, "Active command from client 1 must be cancelled on disconnect");
        Assert.Null(server.ActiveCommandId);

        // Client 2's handshake welcome was sent, snapshot now ready
        var welcome2 = await ReceiveEnvelopeAsync(client2);
        Assert.Equal("mod.welcome", welcome2.MessageType);

        server.Update();
        var snap2 = await ReceiveEnvelopeAsync(client2);
        Assert.Equal("world.snapshot", snap2.MessageType);

        // Client 2 can now execute cmd-2 cleanly
        await SendAndDrainAsync(client2, server, CreateExecuteJson("cmd-2", "task-2", "idem-2"));
        Assert.Equal(2, handler.AcceptCount);
        Assert.Equal("cmd-2", handler.ActiveCommand);
        Assert.Equal("cmd-2", server.ActiveCommandId);

        await client2.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "Client 2 done", CancellationToken.None);
        client2.Dispose();
    }

    [Fact]
    public async Task SaturatedCriticalChannel_TriggersExplicitDisconnect_AndPreservesCachedReplay()
    {
        var handler = new TestTransportHandler();
        // Construct with maxCriticalOutboundCapacity: 2 to deterministically test saturation
        using var server = new WebSocketTransportServer(
            0,
            "test-token",
            handler,
            saveId: "save-123",
            gameSessionId: "session-456",
            logger: (TransportLogger?)null,
            maxNormalOutboundCapacity: 128,
            maxCriticalOutboundCapacity: 2
        );
        server.Start();

        var client = await ConnectClientAsync(server.Port, "test-token");
        await SendRawJsonAsync(client, CreateHelloJson("test-token"));
        await ReceiveEnvelopeAsync(client); // welcome
        server.Update();
        await ReceiveEnvelopeAsync(client); // snapshot

        // Execute cmd-sat
        await SendAndDrainAsync(client, server, CreateExecuteJson("cmd-sat", "task-sat", "idem-sat"));
        Assert.Equal(1, handler.AcceptCount);

        // Create a result payload that was completed
        var successResult = new SkillResultPayload(
            CommandId: "cmd-sat",
            TaskId: "task-sat",
            TerminalState: "succeeded",
            CompletedCount: 5,
            SkippedCount: 0,
            FailedCount: 0,
            FinalWorldRevision: 2,
            Effects: new List<Dictionary<string, object>>(),
            Resources: new SkillResultResources(10f, 5, 15)
        );

        // Send rapid burst of results to saturate the critical outbound queue (capacity is 2)
        // A tight synchronous loop prevents background SendLoopAsync from draining before queue is filled
        for (int i = 0; i < 20; i++)
        {
            _ = server.SendSkillResultAsync(successResult, $"msg-exec-{i}", "idem-sat");
        }

        // Wait briefly for explicit disconnect triggered by critical overflow
        await Task.Delay(100);

        // Assertion 1: Server transitioned to Offline
        Assert.Equal(TransportState.Offline, server.State);

        // Assertion 2: IdempotencyManager preserved the completed result!
        var check = server.Idempotency.Check("idem-sat", "cmd-sat", IdempotencyManager.ComputeSemanticFingerprint(new SkillExecutePayload(
            CommandId: "cmd-sat",
            TaskId: "task-sat",
            SkillId: "water-zone",
            SkillVersion: "0.1",
            ExpectedWorldRevision: 1,
            Parameters: new WaterZoneParameters("Farm", new List<TileCoord> { new(64, 15), new(64, 16) }),
            Budgets: new ExecutionBudgets(60, 50.0f, 20),
            CancelPolicy: "safe-point",
            PolicyDecisionId: "policy-allow"
        )), out var cached);
        Assert.Equal(IdempotencyCheck.CompletedReplay, check);
        Assert.NotNull(cached);
        Assert.Equal("succeeded", cached.TerminalState);
        Assert.Equal(5, cached.CompletedCount);

        client.Dispose();
    }

    [Fact]
    public async Task OutboundSequenceNumbers_AssignedInActualSendOrder()
    {
        var handler = new TestTransportHandler();
        using var server = new WebSocketTransportServer(0, "test-token", handler, saveId: "save-123", gameSessionId: "session-456");
        server.Start();

        var client = await ConnectClientAsync(server.Port, "test-token");
        await SendRawJsonAsync(client, CreateHelloJson("test-token"));

        var welcome = await ReceiveEnvelopeAsync(client);
        Assert.Equal(0, welcome.SequenceNumber);

        server.Update();
        var snapshot = await ReceiveEnvelopeAsync(client);
        Assert.Equal(1, snapshot.SequenceNumber);

        var result = new SkillResultPayload(
            CommandId: "cmd-seq",
            TaskId: "task-seq",
            TerminalState: "succeeded",
            CompletedCount: 1,
            SkippedCount: 0,
            FailedCount: 0,
            FinalWorldRevision: 2,
            Effects: new List<Dictionary<string, object>>()
        );
        await server.SendSkillResultAsync(result, "corr-1", "idem-seq");

        var resultEnv = await ReceiveEnvelopeAsync(client);
        Assert.Equal(2, resultEnv.SequenceNumber);

        client.Dispose();
    }

    [Fact]
    public async Task Test_RawTcpHandshake_MatchesPythonClient()
    {
        var handler = new TestTransportHandler();
        var logs = new List<string>();
        using var server = new WebSocketTransportServer(0, "test-token", handler, logger: (msg, lvl) => logs.Add($"{lvl}: {msg}"));
        server.Start();

        using var tcp = new TcpClient();
        await tcp.ConnectAsync("127.0.0.1", server.Port);
        using var stream = tcp.GetStream();

        string key = Convert.ToBase64String(Guid.NewGuid().ToByteArray());
        string req =
            $"GET / HTTP/1.1\r\n" +
            $"Host: 127.0.0.1:{server.Port}\r\n" +
            $"Upgrade: websocket\r\n" +
            $"Connection: Upgrade\r\n" +
            $"Sec-WebSocket-Key: {key}\r\n" +
            $"Sec-WebSocket-Version: 13\r\n" +
            $"Authorization: Bearer test-token\r\n\r\n";

        byte[] reqBytes = Encoding.Latin1.GetBytes(req);
        await stream.WriteAsync(reqBytes);
        await stream.FlushAsync();

        byte[] respBytes = new byte[1024];
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        int read = await stream.ReadAsync(respBytes, cts.Token);
        string resp = Encoding.Latin1.GetString(respBytes, 0, read);

        Assert.StartsWith("HTTP/1.1 101", resp);
    }

    [Fact]
    public async Task Test_RawTcpHandshake_WrongToken_Returns401()
    {
        var handler = new TestTransportHandler();
        using var server = new WebSocketTransportServer(0, "test-token", handler);
        server.Start();

        using var tcp = new TcpClient();
        await tcp.ConnectAsync("127.0.0.1", server.Port);
        using var stream = tcp.GetStream();

        string key = Convert.ToBase64String(Guid.NewGuid().ToByteArray());
        string req =
            $"GET / HTTP/1.1\r\n" +
            $"Host: 127.0.0.1:{server.Port}\r\n" +
            $"Upgrade: websocket\r\n" +
            $"Connection: Upgrade\r\n" +
            $"Sec-WebSocket-Key: {key}\r\n" +
            $"Sec-WebSocket-Version: 13\r\n" +
            $"Authorization: Bearer wrong-token\r\n\r\n";

        byte[] reqBytes = Encoding.Latin1.GetBytes(req);
        await stream.WriteAsync(reqBytes);
        await stream.FlushAsync();

        byte[] respBytes = new byte[1024];
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        int read = await stream.ReadAsync(respBytes, cts.Token);
        string resp = Encoding.Latin1.GetString(respBytes, 0, read);

        Assert.StartsWith("HTTP/1.1 401", resp);
    }

    [Fact]
    public async Task Test_RawTcpHandshake_Conflict_Returns409()
    {
        var handler = new TestTransportHandler();
        using var server = new WebSocketTransportServer(0, "test-token", handler);
        server.Start();

        // Connect first client
        var client1 = await ConnectClientAsync(server.Port, "test-token");

        // Attempt second client via raw TCP
        using var tcp = new TcpClient();
        await tcp.ConnectAsync("127.0.0.1", server.Port);
        using var stream = tcp.GetStream();

        string key = Convert.ToBase64String(Guid.NewGuid().ToByteArray());
        string req =
            $"GET / HTTP/1.1\r\n" +
            $"Host: 127.0.0.1:{server.Port}\r\n" +
            $"Upgrade: websocket\r\n" +
            $"Connection: Upgrade\r\n" +
            $"Sec-WebSocket-Key: {key}\r\n" +
            $"Sec-WebSocket-Version: 13\r\n" +
            $"Authorization: Bearer test-token\r\n\r\n";

        byte[] reqBytes = Encoding.Latin1.GetBytes(req);
        await stream.WriteAsync(reqBytes);
        await stream.FlushAsync();

        byte[] respBytes = new byte[1024];
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        int read = await stream.ReadAsync(respBytes, cts.Token);
        string resp = Encoding.Latin1.GetString(respBytes, 0, read);

        Assert.StartsWith("HTTP/1.1 409", resp);

        client1.Dispose();
    }

    [Fact]
    public async Task Test_ActualPythonClient_ConnectsToCSharpServer()
    {
        var handler = new TestTransportHandler();
        var logs = new List<string>();
        string token = "test-token-" + Guid.NewGuid().ToString("N");
        using var server = new WebSocketTransportServer(
            0,
            token,
            handler,
            saveId: "save-123",
            gameSessionId: "session-456",
            logger: (msg, lvl) => logs.Add($"{lvl}: {msg}")
        );
        server.Start();

        string scriptPath = Path.Combine(Path.GetTempPath(), $"test_connect_{Guid.NewGuid():N}.py");
        string repoRoot = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", ".."));
        string pyCode = $@"
import asyncio, sys
from pathlib import Path
repo = Path(r'{repoRoot}')
sys.path.insert(0, str(repo / 'runtime' / 'src'))
from stardew_ai_runtime.client import TransportClient

async def main():
    c = TransportClient(host='127.0.0.1', port={server.Port}, session_token='{token}')
    await c.connect(timeout=5.0)
    await c.handshake(timeout=5.0)
    await c.close()
    print('SUCCESS')

asyncio.run(main())
";
        File.WriteAllText(scriptPath, pyCode);

        try
        {
            var psi = new ProcessStartInfo("uv", $"run --project \"{Path.Combine(repoRoot, "runtime")}\" python \"{scriptPath}\"")
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false
            };
            using var proc = Process.Start(psi)!;
            string stdout = await proc.StandardOutput.ReadToEndAsync();
            string stderr = await proc.StandardError.ReadToEndAsync();
            await proc.WaitForExitAsync();

            Assert.True(proc.ExitCode == 0, $"Python exited with code {proc.ExitCode}.\nStdout: {stdout}\nStderr: {stderr}\nServer logs: {string.Join("\n", logs)}");
            Assert.Contains("SUCCESS", stdout);
        }
        finally
        {
            if (File.Exists(scriptPath)) File.Delete(scriptPath);
        }
    }

    [Fact]
    public async Task Test_RawTcpHandshake_NonWebSocket_Returns400()
    {
        var handler = new TestTransportHandler();
        var logs = new List<string>();
        using var server = new WebSocketTransportServer(0, "test-token", handler, logger: (msg, lvl) => logs.Add($"{lvl}: {msg}"));
        server.Start();

        using var tcp = new TcpClient();
        await tcp.ConnectAsync("127.0.0.1", server.Port);
        using var stream = tcp.GetStream();

        string req =
            $"GET / HTTP/1.1\r\n" +
            $"Host: 127.0.0.1:{server.Port}\r\n" +
            $"Authorization: Bearer test-token\r\n\r\n";

        byte[] reqBytes = Encoding.Latin1.GetBytes(req);
        await stream.WriteAsync(reqBytes);
        await stream.FlushAsync();

        byte[] respBytes = new byte[1024];
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        int read = await stream.ReadAsync(respBytes, cts.Token);
        string resp = Encoding.Latin1.GetString(respBytes, 0, read);

        Assert.StartsWith("HTTP/1.1 400", resp);
        Assert.Contains(logs, l => l.Contains("400 BadRequest"));
    }

    [Fact]
    public async Task Test_ActualPythonClient_WrongToken_RejectionLogged()
    {
        var handler = new TestTransportHandler();
        var logs = new List<string>();
        using var server = new WebSocketTransportServer(
            0,
            "correct-token",
            handler,
            logger: (msg, lvl) => logs.Add($"{lvl}: {msg}")
        );
        server.Start();

        string scriptPath = Path.Combine(Path.GetTempPath(), $"test_wrong_token_{Guid.NewGuid():N}.py");
        string repoRoot = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", ".."));
        string pyCode = $@"
import asyncio, sys
from pathlib import Path
repo = Path(r'{repoRoot}')
sys.path.insert(0, str(repo / 'runtime' / 'src'))
from stardew_ai_runtime.client import TransportClient, TransportClientError

async def main():
    c = TransportClient(host='127.0.0.1', port={server.Port}, session_token='wrong-token')
    try:
        await c.connect(timeout=5.0)
        print('UNEXPECTED_CONNECT')
        sys.exit(1)
    except TransportClientError as ex:
        print(f'CAUGHT_EXPECTED: {{ex}}')
        sys.exit(0)
    finally:
        await c.close()

asyncio.run(main())
";
        File.WriteAllText(scriptPath, pyCode);

        try
        {
            var psi = new ProcessStartInfo("uv", $"run --project \"{Path.Combine(repoRoot, "runtime")}\" python \"{scriptPath}\"")
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false
            };
            using var proc = Process.Start(psi)!;
            string stdout = await proc.StandardOutput.ReadToEndAsync();
            string stderr = await proc.StandardError.ReadToEndAsync();
            await proc.WaitForExitAsync();

            Assert.True(proc.ExitCode == 0, $"Python exited with code {proc.ExitCode}.\nStdout: {stdout}\nStderr: {stderr}\nServer logs: {string.Join("\n", logs)}");
            Assert.Contains("CAUGHT_EXPECTED", stdout);
            Assert.Contains(logs, l => l.Contains("401 Unauthorized") && l.Contains("HasAuthHeader=True"));
        }
        finally
        {
            if (File.Exists(scriptPath)) File.Delete(scriptPath);
        }
    }

    [Fact]
    public async Task Test_ActualPythonClient_Conflict_RejectionLogged()
    {
        var handler = new TestTransportHandler();
        var logs = new List<string>();
        string token = "token-" + Guid.NewGuid().ToString("N");
        using var server = new WebSocketTransportServer(
            0,
            token,
            handler,
            logger: (msg, lvl) => logs.Add($"{lvl}: {msg}")
        );
        server.Start();

        // Connect first client and keep open
        var client1 = await ConnectClientAsync(server.Port, token);

        string scriptPath = Path.Combine(Path.GetTempPath(), $"test_conflict_{Guid.NewGuid():N}.py");
        string repoRoot = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", ".."));
        string pyCode = $@"
import asyncio, sys
from pathlib import Path
repo = Path(r'{repoRoot}')
sys.path.insert(0, str(repo / 'runtime' / 'src'))
from stardew_ai_runtime.client import TransportClient, TransportClientError

async def main():
    c = TransportClient(host='127.0.0.1', port={server.Port}, session_token='{token}')
    try:
        await c.connect(timeout=5.0)
        print('UNEXPECTED_CONNECT')
        sys.exit(1)
    except TransportClientError as ex:
        print(f'CAUGHT_CONFLICT: {{ex}}')
        sys.exit(0)
    finally:
        await c.close()

asyncio.run(main())
";
        File.WriteAllText(scriptPath, pyCode);

        try
        {
            var psi = new ProcessStartInfo("uv", $"run --project \"{Path.Combine(repoRoot, "runtime")}\" python \"{scriptPath}\"")
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false
            };
            using var proc = Process.Start(psi)!;
            string stdout = await proc.StandardOutput.ReadToEndAsync();
            string stderr = await proc.StandardError.ReadToEndAsync();
            await proc.WaitForExitAsync();

            Assert.True(proc.ExitCode == 0, $"Python exited with code {proc.ExitCode}.\nStdout: {stdout}\nStderr: {stderr}\nServer logs: {string.Join("\n", logs)}");
            Assert.Contains("CAUGHT_CONFLICT", stdout);
            Assert.Contains(logs, l => l.Contains("409 Conflict"));
        }
        finally
        {
            if (File.Exists(scriptPath)) File.Delete(scriptPath);
            client1.Dispose();
        }
    }

    [Fact]
    public async Task Test_ChatChannel_SubmitAndReply_Roundtrip()
    {
        var handler = new TestTransportHandler();
        var logs = new List<string>();
        using var server = new WebSocketTransportServer(
            0,
            "test-token",
            handler,
            logger: (msg, lvl) => logs.Add($"{lvl}: {msg}")
        );
        server.Start();

        ChatReplyPayload? receivedReply = null;
        server.OnChatReplyReceived += reply => { receivedReply = reply; };

        // Connect ClientWebSocket to /chat
        using var clientWs = new ClientWebSocket();
        clientWs.Options.SetRequestHeader("Authorization", "Bearer test-token");
        using var connectCts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await clientWs.ConnectAsync(new Uri($"ws://127.0.0.1:{server.Port}/chat"), connectCts.Token);

        Assert.Equal(WebSocketState.Open, clientWs.State);
        await Task.Delay(100);
        Assert.True(server.IsChatConnected);

        // Server sends chat submit
        var submitPayload = new ChatSubmitPayload("req-chat-01", "帮我把箱子里的种子种下", "text", "Farm_Save01");
        bool sent = await server.SendChatSubmitAsync(submitPayload);
        Assert.True(sent);

        // Client receives chat.submit
        var buffer = new byte[4096];
        var recvResult = await clientWs.ReceiveAsync(new ArraySegment<byte>(buffer), connectCts.Token);
        Assert.True(recvResult.EndOfMessage);
        string recvText = Encoding.UTF8.GetString(buffer, 0, recvResult.Count);
        using var doc = JsonDocument.Parse(recvText);
        Assert.Equal("chat.submit", doc.RootElement.GetProperty("messageType").GetString());
        var p = doc.RootElement.GetProperty("payload");
        Assert.Equal("req-chat-01", p.GetProperty("requestId").GetString());
        Assert.Equal("帮我把箱子里的种子种下", p.GetProperty("text").GetString());

        // Client replies with chat.reply
        var replyJson = JsonSerializer.Serialize(new
        {
            messageType = "chat.reply",
            payload = new
            {
                requestId = "req-chat-01",
                status = "completed",
                replyText = "胡萝卜种子已全部播种完成！",
                tokensUsed = 12500,
                promptTokens = 11000,
                outputTokens = 1500,
                conversationId = "cid-abc-123"
            }
        });
        var replyBytes = Encoding.UTF8.GetBytes(replyJson);
        await clientWs.SendAsync(new ArraySegment<byte>(replyBytes), WebSocketMessageType.Text, true, connectCts.Token);

        // Give server loop time to enqueue and drain on Update()
        await Task.Delay(100);
        server.Update();

        Assert.NotNull(receivedReply);
        Assert.Equal("req-chat-01", receivedReply!.RequestId);
        Assert.Equal("completed", receivedReply.Status);
        Assert.Equal("胡萝卜种子已全部播种完成！", receivedReply.ReplyText);
        Assert.Equal(12500, receivedReply.TokensUsed);
        Assert.Equal("cid-abc-123", receivedReply.ConversationId);

        // Close client and verify state
        await clientWs.CloseAsync(WebSocketCloseStatus.NormalClosure, "done", connectCts.Token);
        await Task.Delay(100);
        Assert.False(server.IsChatConnected);
    }
}




