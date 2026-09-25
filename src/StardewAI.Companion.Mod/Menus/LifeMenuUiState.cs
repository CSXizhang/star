using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>
/// Pure-logic UI state machine for <see cref="CompanionLifeMenu"/>.
/// No StardewValley game types are referenced; all state is in CLR primitives.
/// Designed to be directly testable with xUnit.
/// </summary>
public sealed class LifeMenuUiState
{
    // -----------------------------------------------------------------------
    // Life chat state
    // -----------------------------------------------------------------------

    /// <summary>The pending life-chat request ID (<c>null</c> when idle).</summary>
    public string? PendingChatRequestId { get; private set; }

    /// <summary>The mode used for the pending chat request.</summary>
    public string? PendingChatMode { get; private set; }

    /// <summary>Whether a life-chat request is in flight.</summary>
    public bool IsChatPending => PendingChatRequestId is not null;

    /// <summary>Current status of the chat exchange.</summary>
    public LifeChatStatus ChatStatus { get; private set; } = LifeChatStatus.Idle;

    /// <summary>
    /// The companion's reply text when <see cref="ChatStatus"/> is
    /// <see cref="LifeChatStatus.Completed"/> or <see cref="LifeChatStatus.Failed"/>.
    /// </summary>
    public string? ReplyText { get; private set; }

    /// <summary>Error description when <see cref="ChatStatus"/> is <see cref="LifeChatStatus.Failed"/>.</summary>
    public string? ErrorText { get; private set; }

    /// <summary>Queue position when <see cref="ChatStatus"/> is <see cref="LifeChatStatus.Queued"/>.</summary>
    public int? QueuePosition { get; private set; }

    // -----------------------------------------------------------------------
    // Profile state (mirrored from life.profile.state)
    // -----------------------------------------------------------------------

    /// <summary>Whether the companion profile has been set up.</summary>
    public bool IsOnboarded { get; private set; }

    /// <summary>Whether the player explicitly skipped setup.</summary>
    public bool IsSkipped { get; private set; }

    /// <summary>The companion's chosen name.</summary>
    public string CompanionName { get; private set; } = "阿星";

    /// <summary>
    /// Play-style: <c>"earn"</c>, <c>"workhorse"</c>, <c>"community"</c>, or <c>"decor"</c>.
    /// </summary>
    public string PlayStyle { get; private set; } = "earn";

    /// <summary>
    /// Personality: <c>"gentle"</c>, <c>"lively"</c>, <c>"calm"</c>, or <c>"tsundere"</c>.
    /// </summary>
    public string Personality { get; private set; } = "gentle";

    /// <summary>
    /// Care frequency: <c>"quiet"</c>, <c>"moderate"</c>, or <c>"chatty"</c>.
    /// </summary>
    public string CareFrequency { get; private set; } = "moderate";

    /// <summary>Current profile revision used for optimistic-locking on edits.</summary>
    public int ProfileRevision { get; private set; }

    /// <summary>
    /// Whether any <c>life.profile.state</c> has been received for the current save.
    /// Until then the setup form only shows defaults and saving must stay disabled.
    /// </summary>
    public bool HasProfileState { get; private set; }

    // -----------------------------------------------------------------------
    // Work state (read-only projection from life.profile.state.work)
    // -----------------------------------------------------------------------

    /// <summary>Autonomy mode: <c>"free"</c> or <c>"command"</c>.</summary>
    public string WorkMode { get; private set; } = "command";

    /// <summary>Whether the companion is paused.</summary>
    public bool WorkPaused { get; private set; }

    /// <summary>Daily spend limit from the bridge (null = unknown).</summary>
    public int? DailySpendLimit { get; private set; }

    /// <summary>Confirmed, read-only work plan shown in the plan panel.</summary>
    public string? WorkGoal { get; private set; }

    public IReadOnlyList<string> ActiveGoalSummaries { get; private set; } = Array.Empty<string>();

    public IReadOnlyList<string> RecentTodoSummaries { get; private set; } = Array.Empty<string>();

    public IReadOnlyList<string> WaitingConditions { get; private set; } = Array.Empty<string>();

    public string? PlanWaitReason { get; private set; }

    /// <summary>Updates the work projection from a confirmed profile-state reply.</summary>
    public void ApplyWorkProjection(
        string? goal,
        IEnumerable<string> activeGoals,
        IEnumerable<string> recentTodos,
        IEnumerable<string> waitingConditions,
        string? planWaitReason)
    {
        WorkGoal = goal;
        ActiveGoalSummaries = activeGoals.Take(5).ToArray();
        RecentTodoSummaries = recentTodos.Take(5).ToArray();
        WaitingConditions = waitingConditions.Take(5).ToArray();
        PlanWaitReason = planWaitReason;
    }

    // -----------------------------------------------------------------------
    // Memory state
    // -----------------------------------------------------------------------

    /// <summary>Current memory revision used for optimistic-locking on edits.</summary>
    public int MemoryRevision { get; private set; }

    /// <summary>Snapshot of memory entries from <c>life.memory.state</c>.</summary>
    public IReadOnlyList<MemoryEntrySnapshot> MemoryEntries { get; private set; } =
        Array.Empty<MemoryEntrySnapshot>();

    // -----------------------------------------------------------------------
    // Care hint pending list
    // -----------------------------------------------------------------------

    private readonly List<PendingCareHint> _unreadCareHints = new();
    private readonly List<PendingCareHint> _recentCareHints = new();

    /// <summary>Care hints from <see cref="CareHintController"/> not yet read by the player.</summary>
    public IReadOnlyList<PendingCareHint> UnreadCareHints => _unreadCareHints;

    /// <summary>Recent care text remains readable after the unread badge is cleared.</summary>
    public IReadOnlyList<PendingCareHint> RecentCareHints => _recentCareHints;

    // -----------------------------------------------------------------------
    // Pending operations (optimistic-lock tracking)
    // -----------------------------------------------------------------------

    /// <summary>Pending profile-set request ID (<c>null</c> when none in flight).</summary>
    public string? PendingProfileSetRequestId { get; private set; }

    /// <summary>Expected profile revision for the pending profile-set operation.</summary>
    public int PendingProfileSetExpectedRevision { get; private set; }

    /// <summary>Pending memory-edit request ID (<c>null</c> when none in flight).</summary>
    public string? PendingMemoryEditRequestId { get; private set; }

    /// <summary>Player-facing result of the latest confirmed memory edit.</summary>
    public string? MemoryEditFeedback { get; private set; }

    // -----------------------------------------------------------------------
    // Life-chat methods
    // -----------------------------------------------------------------------

    /// <summary>Starts a life-chat exchange.</summary>
    /// <returns><see langword="false"/> when a request is already in flight.</returns>
    public bool BeginChat(string requestId, string mode)
    {
        if (IsChatPending) return false;
        PendingChatRequestId = requestId;
        PendingChatMode = mode;
        ChatStatus = LifeChatStatus.Sending;
        ReplyText = null;
        ErrorText = null;
        QueuePosition = null;
        return true;
    }

    /// <summary>
    /// Applies a <c>life.chat.reply</c> payload to this state.
    /// </summary>
    /// <returns><see langword="false"/> when the reply does not match the pending request.</returns>
    public bool ApplyChatReply(
        string requestId,
        string status,
        string? replyText,
        int? queuePosition,
        string? error,
        int profileRevision,
        int memoryRevision)
    {
        if (!string.Equals(PendingChatRequestId, requestId, StringComparison.Ordinal))
            return false;

        ProfileRevision = profileRevision;
        MemoryRevision = memoryRevision;
        QueuePosition = queuePosition;

        ChatStatus = status switch
        {
            "processing" => LifeChatStatus.Processing,
            "queued" => LifeChatStatus.Queued,
            "completed" => LifeChatStatus.Completed,
            "failed" => LifeChatStatus.Failed,
            _ => LifeChatStatus.Processing
        };

        if (ChatStatus is LifeChatStatus.Completed or LifeChatStatus.Failed)
        {
            ReplyText = replyText;
            ErrorText = error;
            PendingChatRequestId = null;
            PendingChatMode = null;
        }
        else
        {
            ReplyText = replyText;
        }

        return true;
    }

    // -----------------------------------------------------------------------
    // Profile-state methods
    // -----------------------------------------------------------------------

    /// <summary>Applies a <c>life.profile.state</c> payload.</summary>
    public void ApplyProfileState(
        bool onboarded,
        bool skipped,
        string companionName,
        string playStyle,
        string personality,
        string careFrequency,
        int profileRevision,
        string workMode,
        bool workPaused,
        int? dailySpendLimit,
        int memoryRevision,
        string? requestId = null)
    {
        IsOnboarded = onboarded;
        IsSkipped = skipped;
        CompanionName = companionName;
        PlayStyle = playStyle;
        Personality = personality;
        CareFrequency = careFrequency;
        ProfileRevision = profileRevision;
        WorkMode = workMode;
        WorkPaused = workPaused;
        DailySpendLimit = dailySpendLimit;
        MemoryRevision = memoryRevision;

        // An unrelated profile.get refresh must not claim this edit was saved.
        if (requestId != null && requestId == PendingProfileSetRequestId)
            PendingProfileSetRequestId = null;
    }

    /// <summary>
    /// Marks that a <c>life.profile.state</c> reply has arrived for the current save,
    /// even when it carries <c>profile: null</c> (not onboarded yet).
    /// </summary>
    public void MarkProfileStateReceived()
    {
        HasProfileState = true;
    }

    /// <summary>Records that a <c>life.profile.set</c> request is in flight.</summary>
    public void BeginProfileSet(string requestId, int expectedRevision)
    {
        PendingProfileSetRequestId = requestId;
        PendingProfileSetExpectedRevision = expectedRevision;
    }

    // -----------------------------------------------------------------------
    // Memory-state methods
    // -----------------------------------------------------------------------

    /// <summary>Applies a <c>life.memory.state</c> payload.</summary>
    public void ApplyMemoryState(
        int memoryRevision,
        IEnumerable<MemoryEntrySnapshot> entries,
        string? requestId = null,
        string status = "confirmed",
        string? reason = null)
    {
        MemoryRevision = memoryRevision;
        MemoryEntries = entries.ToList();
        if (requestId != null && requestId == PendingMemoryEditRequestId)
        {
            MemoryEditFeedback = status == "confirmed"
                ? "记忆已保存。"
                : reason == "STALE_REVISION"
                    ? "记忆已变化，请查看最新列表后重试。"
                    : "记忆未保存，请重试。";
            PendingMemoryEditRequestId = null;
        }
    }

    /// <summary>Records that a <c>life.memory.edit</c> request is in flight.</summary>
    public void BeginMemoryEdit(string requestId)
    {
        PendingMemoryEditRequestId = requestId;
        MemoryEditFeedback = null;
    }

    // -----------------------------------------------------------------------
    // Care-hint methods
    // -----------------------------------------------------------------------

    /// <summary>Adds an unread care hint to the pending list (capped at 5).</summary>
    public void AddUnreadCareHint(PendingCareHint hint)
    {
        if (_recentCareHints.Any(h => h.EventKey == hint.EventKey))
            return;
        while (_recentCareHints.Count >= CareHintController.MaxPendingCount)
            _recentCareHints.RemoveAt(0);
        _recentCareHints.Add(hint);
        while (_unreadCareHints.Count >= CareHintController.MaxPendingCount)
            _unreadCareHints.RemoveAt(0);
        _unreadCareHints.Add(hint);
    }

    /// <summary>Clears all unread care hints (e.g. player opened the read list).</summary>
    public void MarkAllCareHintsRead()
    {
        _unreadCareHints.Clear();
    }

    // -----------------------------------------------------------------------
    // Reset
    // -----------------------------------------------------------------------

    /// <summary>Resets transient state (e.g. on return-to-title).</summary>
    public void Reset()
    {
        PendingChatRequestId = null;
        PendingChatMode = null;
        ChatStatus = LifeChatStatus.Idle;
        ReplyText = null;
        ErrorText = null;
        QueuePosition = null;
        PendingProfileSetRequestId = null;
        PendingMemoryEditRequestId = null;
        MemoryEditFeedback = null;
        HasProfileState = false;
        WorkGoal = null;
        ActiveGoalSummaries = Array.Empty<string>();
        RecentTodoSummaries = Array.Empty<string>();
        WaitingConditions = Array.Empty<string>();
        PlanWaitReason = null;
        _unreadCareHints.Clear();
        _recentCareHints.Clear();
    }
}

/// <summary>Life-chat status values.</summary>
public enum LifeChatStatus
{
    /// <summary>No exchange in progress.</summary>
    Idle,
    /// <summary>Request has been created locally but not yet sent.</summary>
    Sending,
    /// <summary>Request has been sent; waiting for the first reply.</summary>
    Processing,
    /// <summary>Request is queued behind another active exchange.</summary>
    Queued,
    /// <summary>Exchange completed successfully.</summary>
    Completed,
    /// <summary>Exchange failed.</summary>
    Failed
}

/// <summary>Snapshot of a single memory entry from <c>life.memory.state</c>.</summary>
/// <param name="Id">Unique identifier.</param>
/// <param name="Kind"><c>"preference"</c>, <c>"agreement"</c>, or <c>"event"</c>.</param>
/// <param name="Text">Display text (natural language).</param>
/// <param name="Source"><c>"player"</c>, <c>"companion"</c>, or <c>"system"</c>.</param>
/// <param name="GameDate">Game date in <c>"y:Season:d"</c> format.</param>
/// <param name="CreatedAt">RFC3339 creation timestamp.</param>
public sealed record MemoryEntrySnapshot(
    string Id,
    string Kind,
    string Text,
    string Source,
    string GameDate,
    string CreatedAt);
