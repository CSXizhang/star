using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>A source handoff remains closed until its own runtime acknowledgement arrives.</summary>
public sealed class CompanionConnectionState
{
    public string Source { get; private set; } = "builtin";
    private string _savedSource = "builtin";
    public bool NeedsPersistence => Source != _savedSource;
    public string Status { get; private set; } = "waiting";
    public string? ClientName { get; private set; }
    public string? ConversationName { get; private set; }
    public string? PendingRequestId { get; private set; }
    public string? PendingSource { get; private set; }
    public bool IsSwitching => PendingRequestId != null;

    public void Initialize(string source)
    {
        Source = source == "external" ? "external" : "builtin";
        _savedSource = Source;
        ResetConnection();
    }

    public void ResetConnection()
    {
        Status = "waiting";
        ClientName = ConversationName = PendingRequestId = PendingSource = null;
    }

    public void NoteSaved(string source) => _savedSource = source;

    public void Begin(string requestId, string source)
    {
        if (source is not ("builtin" or "external")) throw new ArgumentException("Unknown source.", nameof(source));
        PendingRequestId = requestId;
        PendingSource = source;
        Status = "switching";
    }

    public bool Apply(ConnectionStatePayload state)
    {
        if (state.Source is not ("builtin" or "external") || state.Status is not ("waiting" or "connected" or "disconnected" or "error")) return false;
        if (IsSwitching && (state.RequestId != PendingRequestId || state.Source != PendingSource)) return false;
        if (!IsSwitching && state.Source != Source) return false;
        if (state.Status == "error") { Status = "error"; return false; }
        bool confirmed = IsSwitching;
        Source = state.Source;
        Status = state.Status;
        ClientName = state.ClientName;
        ConversationName = state.ConversationName;
        PendingRequestId = PendingSource = null;
        return confirmed;
    }

    public string Description => IsSwitching ? Status == "error" ? "连接切换未完成，请重试。" : "正在断开旧伙伴并切换连接…"
        : Source == "external" ? Status == "connected"
            ? $"已有伙伴已连接：{ClientName ?? "原客户端"} / {ConversationName ?? "原会话"}"
            : "等待已有 AI 伙伴从原会话连接。"
        : "在游戏内启动伙伴";
}
