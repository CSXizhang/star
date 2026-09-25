namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// Pure-logic controller for companion care hints (§1.7 <c>life.care</c>).
///
/// Responsibilities:
/// <list type="bullet">
///   <item><description>De-duplication by <c>eventKey</c> (same key is never shown twice).</description></item>
///   <item><description>Deferred display when a menu or event is active.</description></item>
///   <item><description>Day-rollover expiry: un-shown hints from previous game days are discarded.</description></item>
///   <item><description>Pending-read list capped at 5 entries (oldest discarded when full).</description></item>
/// </list>
///
/// No StardewValley game types are referenced; all inputs are primitives.
/// </summary>
public sealed class CareHintController
{
    /// <summary>Maximum number of unread care hints kept in the pending list.</summary>
    public const int MaxPendingCount = 5;

    private readonly HashSet<string> _seenEventKeys = new(StringComparer.Ordinal);
    private readonly List<PendingCareHint> _pendingHints = new();

    // Hints received while a menu/event is blocking; drained when unblocked.
    private readonly Queue<PendingCareHint> _deferredHints = new();

    /// <summary>Current game date token (e.g. <c>"1:Spring:1"</c>).</summary>
    private string _currentGameDate = string.Empty;

    /// <summary>
    /// Snapshot of unread pending care hints ordered oldest-to-newest.
    /// Consumers should call <see cref="MarkRead"/> when shown to the player.
    /// </summary>
    public IReadOnlyList<PendingCareHint> PendingHints => _pendingHints;

    /// <summary>
    /// Notifies the controller that a new game day has started.
    /// All hints whose <c>gameDate</c> differs from <paramref name="newGameDate"/> are discarded.
    /// </summary>
    /// <param name="newGameDate">Game date string in <c>"y:Season:d"</c> format.</param>
    public void OnDayStarted(string newGameDate)
    {
        _currentGameDate = newGameDate;

        // Expire pending hints from prior days.
        _pendingHints.RemoveAll(h => !string.Equals(h.GameDate, _currentGameDate, StringComparison.Ordinal));

        // Expire deferred hints from prior days.
        var remainingDeferred = _deferredHints
            .Where(h => string.Equals(h.GameDate, _currentGameDate, StringComparison.Ordinal))
            .ToList();
        _deferredHints.Clear();
        foreach (var h in remainingDeferred)
            _deferredHints.Enqueue(h);
    }

    /// <summary>
    /// Processes an incoming <c>life.care</c> payload.
    /// Returns a <see cref="PendingCareHint"/> to display via HUD immediately when not blocked,
    /// or <see langword="null"/> when the hint was ignored (duplicate, expired, or deferred).
    /// </summary>
    /// <param name="eventKey">Unique key for de-duplication.</param>
    /// <param name="gameDate">The game date the event applies to.</param>
    /// <param name="kind">Hint category: <c>"morning"</c>, <c>"work-done"</c>, or <c>"evening"</c>.</param>
    /// <param name="text">Display text (max 300 chars per contract).</param>
    /// <param name="isBlocked">
    /// <see langword="true"/> when a menu or in-game event is currently active
    /// (the hint will be deferred until <see cref="TryDrainDeferred"/> is called).
    /// </param>
    /// <returns>The hint to show immediately, or <see langword="null"/>.</returns>
    public PendingCareHint? ReceiveCareHint(
        string eventKey,
        string gameDate,
        string kind,
        string text,
        bool isBlocked)
    {
        // Ignore duplicate event keys.
        if (_seenEventKeys.Contains(eventKey))
            return null;

        // Discard hints for a date that differs from the current day.
        if (!string.IsNullOrEmpty(_currentGameDate) &&
            !string.Equals(gameDate, _currentGameDate, StringComparison.Ordinal))
            return null;

        _seenEventKeys.Add(eventKey);

        var hint = new PendingCareHint(eventKey, gameDate, kind, text);
        AddToPending(hint);

        if (isBlocked)
        {
            _deferredHints.Enqueue(hint);
            return null;
        }

        return hint;
    }

    /// <summary>
    /// Drains deferred hints and returns those that should now be shown (unblocked).
    /// Call this when a menu or event closes.
    /// </summary>
    public IEnumerable<PendingCareHint> TryDrainDeferred()
    {
        while (_deferredHints.Count > 0)
        {
            var hint = _deferredHints.Dequeue();

            // Skip if expired (day changed since the hint was deferred).
            if (!string.IsNullOrEmpty(_currentGameDate) &&
                !string.Equals(hint.GameDate, _currentGameDate, StringComparison.Ordinal))
                continue;

            yield return hint;
        }
    }

    /// <summary>
    /// Marks the hint identified by <paramref name="eventKey"/> as read and removes it
    /// from the pending list.
    /// </summary>
    public void MarkRead(string eventKey)
    {
        _pendingHints.RemoveAll(h => string.Equals(h.EventKey, eventKey, StringComparison.Ordinal));
    }

    // ------------------------------------------------------------------

    private void AddToPending(PendingCareHint hint)
    {
        // Enforce capacity — discard the oldest when full.
        while (_pendingHints.Count >= MaxPendingCount)
            _pendingHints.RemoveAt(0);

        _pendingHints.Add(hint);
    }
}

/// <summary>
/// An individual companion care hint waiting to be read by the player.
/// </summary>
/// <param name="EventKey">Unique key used for de-duplication.</param>
/// <param name="GameDate">The game date this hint was issued for.</param>
/// <param name="Kind"><c>"morning"</c>, <c>"work-done"</c>, or <c>"evening"</c>.</param>
/// <param name="Text">Player-visible hint text (plain natural language, no JSON).</param>
public sealed record PendingCareHint(
    string EventKey,
    string GameDate,
    string Kind,
    string Text);
