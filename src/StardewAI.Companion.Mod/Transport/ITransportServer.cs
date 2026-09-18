namespace StardewAI.Companion.Mod.Transport;

public enum TransportState
{
    Disconnected,
    Listening,
    Handshaking,
    Syncing,
    Ready,
    Offline
}

/// <summary>
/// Interface for the WebSocket Transport Server.
/// </summary>
public interface ITransportServer : IDisposable
{
    int Port { get; }
    string SessionToken { get; }
    TransportState State { get; }
    bool IsClientConnected { get; }
    string? ActiveCommandId { get; }

    void Start();
    void Stop();

    /// <summary>
    /// Must be called every tick on SMAPI's GameLoop.UpdateTicked on the main thread.
    /// Drains incoming messages from the bounded queue and dispatches them to ITransportHandler.
    /// </summary>
    void Update();

    /// <summary>
    /// Sends a world snapshot to the connected companion client.
    /// </summary>
    Task SendSnapshotAsync(WorldSnapshotPayload snapshot, long worldRevision);

    /// <summary>
    /// Sends a terminal skill result to the connected companion client.
    /// </summary>
    Task SendSkillResultAsync(SkillResultPayload result, string correlationId, string? idempotencyKey = null);

    /// <summary>
    /// Sends a protocol error to the connected companion client.
    /// </summary>
    Task SendProtocolErrorAsync(string code, string message, string? correlationId = null);

    /// <summary>
    /// Whether an agent chat bridge client is connected to /chat.
    /// </summary>
    bool IsChatConnected { get; }

    /// <summary>
    /// Sends a user chat command to the connected chat bridge agent.
    /// </summary>
    Task<bool> SendChatSubmitAsync(ChatSubmitPayload payload);

    /// <summary>
    /// Sends a cancellation request to the connected chat bridge agent.
    /// </summary>
    Task<bool> SendChatCancelAsync(ChatCancelPayload payload);

    /// <summary>
    /// Fired on the main game thread when a chat reply or status update arrives from the chat bridge.
    /// </summary>
    event Action<ChatReplyPayload>? OnChatReplyReceived;
}

