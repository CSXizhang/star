using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>Accept controls only from the current conversation, before its final reply.</summary>
public sealed class LifeControlAcknowledgements
{
    private string? _requestId;
    private int? _latestEpoch;
    private readonly HashSet<string> _accepted = new();
    public void Begin(string requestId) { _requestId = requestId; _latestEpoch = null; _accepted.Clear(); }
    public void Supersede() { _requestId = null; _latestEpoch = null; _accepted.Clear(); }
    public bool TryAccept(AutonomyStatePayload state, string? pendingChatId, bool hasPendingControl)
    {
        if (hasPendingControl || _requestId == null || state.RequestId != _requestId ||
            pendingChatId != _requestId || state.Status != "confirmed" || state.Mode is not ("free" or "command") ||
            state.ControlAction is not ("pause" or "resume" or "cancel" or "set_mode" or "set_preferences")) return false;
        if (state.ControlAction == "pause" && !state.Paused || state.ControlAction == "resume" && state.Paused) return false;
        if (_latestEpoch.HasValue && state.DecisionEpoch < _latestEpoch) return false;
        if (!_accepted.Add($"{state.ControlAction}:{state.DecisionEpoch}")) return false;
        if (state.DecisionEpoch.HasValue) _latestEpoch = state.DecisionEpoch;
        return true;
    }
}
