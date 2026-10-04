using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>A player-opened batch of speech; arrivals wait for the next interaction.</summary>
public sealed class CompanionF8Flow
{
    private readonly Queue<string> _unread = new();
    public string? ReplyToDecisionId { get; private set; }

    public void Begin(LifeMenuUiState state)
    {
        _unread.Clear();
        ReplyToDecisionId = null;
        foreach (var entry in state.Conversation.Where(e => !e.IsPlayer && e.Unread && e.DecisionStatus != "dismissed"))
            _unread.Enqueue(entry.Id);
    }

    public CompanionConversationEntry? TakeNext(LifeMenuUiState state)
    {
        while (_unread.Count > 0)
        {
            string id = _unread.Dequeue();
            var entry = state.Conversation.FirstOrDefault(e => e.Id == id && e.Unread && e.DecisionStatus != "dismissed");
            if (entry != null) return entry;
        }
        return null;
    }

    public void Presented(LifeMenuUiState state, string id)
    {
        var entry = state.Conversation.FirstOrDefault(e => e.Id == id);
        if (entry == null) return;
        state.MarkConversationRead(id);
        ReplyToDecisionId = entry.DecisionStatus == "pending" ? entry.DecisionId : null;
    }

    public string? PendingReplyTarget(LifeMenuUiState state, string? draftDecisionId = null, bool includePresented = true)
    {
        string? id = draftDecisionId ?? (includePresented ? ReplyToDecisionId : null);
        return state.PendingDecisions.Any(e => e.DecisionId == id) ? id : null;
    }
}
