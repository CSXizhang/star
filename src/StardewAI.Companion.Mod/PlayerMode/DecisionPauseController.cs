using StardewModdingAPI;
using StardewValley;

namespace StardewAI.Companion.Mod.PlayerMode;

/// <summary>
/// Single-player "model thinking pauses the world clock" capability, implemented as a
/// lease over <c>Game1.isTimePaused</c>. IL-verified against the 1.6 build: the flag is
/// consumed only by <c>Game1.shouldTimePass</c>, so setting it freezes just the clock —
/// world updates, rendering and SMAPI <c>UpdateTicked</c> keep running, which is exactly
/// what keeps transport heartbeats, snapshot publishing and command receipts alive during
/// model deliberation. <c>Game1.paused</c> must NEVER be used for this: it halts the whole
/// <c>Game1._update</c> loop, including warp and sleep fade processing.
///
/// The preserve-preexisting-state rule is paramount: this controller only ever clears a
/// flag it set itself, and it begins a lease only when the flag was clear and no menu
/// already stops the clock (menus independently stop time via <c>shouldTimePass</c>, so
/// pausing behind one would be invisible state at best). When the clock is already stopped
/// the lease is tracked-but-inactive: <see cref="Begin"/> returns false, no flag is set,
/// and <see cref="End"/> still works as a clean no-op bookkeeping path.
///
/// This controller is the single-player policy/capability point. Future multiplayer MUST
/// NOT globally pause the human world clock; multiplayer support would replace this with
/// per-player action leases and leave <c>Game1.isTimePaused</c> alone.
///
/// All methods are game-thread only; <see cref="Tick"/> must be driven from SMAPI
/// <c>UpdateTicked</c>. Integration wires <see cref="ReleaseAll"/> to human takeover,
/// transport disconnect, <c>DayEnding</c>, <c>Saving</c> and <c>ReturnedToTitle</c>.
/// </summary>
public sealed class DecisionPauseController
{
    private readonly object _leaseLock = new();
    private readonly TimeSpan _maxLease;
    private readonly Action<string, LogLevel>? _log;

    private string? _leaseId;
    private DateTime _leaseStartedUtc;
    private bool _weSetPauseFlag;
    private bool _leaseMaySetFlag;
    private string? _failureReason;
    private bool _failureOwnsFlag;
    private bool _failureSuspendedForAction;
    private string? _failureHoldId;
    private DateTime? _failureSinceUtc;

    public string? FailureReason => _failureReason;

    /// <summary>Structured program-hold state for snapshots and recovery scheduling.
    /// Null when no failure hold is active. The hold id is unique per StopForFailure
    /// so the runtime can dedupe exactly one recovery decision per hold.</summary>
    public (bool Active, string? Reason, string? HoldId, string? SinceUtc) CurrentHold =>
        _failureReason is null
            ? (false, null, null, null)
            : (true, _failureReason, _failureHoldId, _failureSinceUtc?.ToString("o"));

    /// <summary>Explicit program stop, distinct from a renewable thinking lease.
    /// Cleared by a fresh turn or human takeover; does not change resources or dates.</summary>
    public void StopForFailure(string reason)
    {
        lock (_leaseLock)
        {
            if (_failureReason == null)
            {
                _failureHoldId = Guid.NewGuid().ToString("N")[..8];
                _failureSinceUtc = DateTime.UtcNow;
            }
            _failureReason = reason;
            if (_weSetPauseFlag) { _weSetPauseFlag = false; _failureOwnsFlag = true; }
            if (Context.IsWorldReady && !Game1.IsMultiplayer && !Game1.isTimePaused && !_failureSuspendedForAction)
            { Game1.isTimePaused = true; _failureOwnsFlag = true; }
            Log($"program stopped with clock held: {reason}", LogLevel.Warn);
        }
    }

    public void ClearFailureStop()
    {
        lock (_leaseLock)
        {
            _failureReason = null;
            _failureHoldId = null;
            _failureSinceUtc = null;
            _failureSuspendedForAction = false;
            if (_failureOwnsFlag)
            {
                if (_leaseId != null) { _weSetPauseFlag = true; _leaseMaySetFlag = true; }
                else Game1.isTimePaused = false;
            }
            _failureOwnsFlag = false;
        }
    }

    /// <summary>
    /// Creates the controller with the maximum lease duration; a lease older than this is
    /// expired by <see cref="Tick"/> (or any later <see cref="ReleaseAll"/>).
    /// </summary>
    /// <param name="maxLease">Maximum time since the last renewal. Must be positive.</param>
    /// <param name="log">Optional SMAPI-style log sink.</param>
    public DecisionPauseController(TimeSpan maxLease, Action<string, LogLevel>? log = null)
    {
        if (maxLease <= TimeSpan.Zero)
            throw new ArgumentOutOfRangeException(nameof(maxLease), maxLease, "Max lease must be positive.");
        _maxLease = maxLease;
        _log = log;
    }

    private void Log(string message, LogLevel level = LogLevel.Info) => _log?.Invoke(message, level);

    /// <summary>
    /// True while this controller currently holds an ACTIVE clock pause, i.e. it set
    /// <c>Game1.isTimePaused</c> itself and has not yet cleared it. False for
    /// tracked-but-inactive leases (clock already stopped by a menu or a foreign flag).
    /// </summary>
    public bool IsPausing => _weSetPauseFlag || _failureOwnsFlag;

    /// <summary>The id of the currently tracked lease (active or tracked-but-inactive), else null.</summary>
    public string? ActiveLeaseId => _leaseId;

    /// <summary>
    /// Attempts to begin a thinking pause lease.
    ///
    /// Refuses (returns false, logs at Debug, sets no flag) when:
    /// <list type="bullet">
    /// <item>the world is not ready, an event is up, the day is ending or end-of-night
    /// screens are showing — unsafe to touch the flag at all (lease not tracked);</item>
    /// <item>a menu is open or <c>Game1.isTimePaused</c> is already true — time is already
    /// stopped, so the lease is tracked-but-inactive and <see cref="End"/> still works.</item>
    /// </list>
    /// Only one lease exists at a time; re-beginning a still-live matching lease renews it
    /// and a different id while a lease is tracked is refused.
    /// </summary>
    /// <param name="leaseId">Opaque lease id (e.g. the transport lease id), used by <see cref="End"/>.</param>
    /// <returns>True only when this call actually set <c>Game1.isTimePaused = true</c>.</returns>
    public bool Begin(string leaseId)
    {
        if (string.IsNullOrWhiteSpace(leaseId))
            throw new ArgumentNullException(nameof(leaseId));

        lock (_leaseLock)
        {
            if (_leaseId is not null)
            {
                if (string.Equals(_leaseId, leaseId, StringComparison.Ordinal))
                {
                    if (DateTime.UtcNow-_leaseStartedUtc > _maxLease) { ClearOwnFlagUnlocked(); return false; }
                    _leaseStartedUtc = DateTime.UtcNow;
                    Log($"thinking pause begin '{leaseId}' ignored: lease already tracked.", LogLevel.Debug);
                    return _weSetPauseFlag;
                }

                Log($"thinking pause begin '{leaseId}' refused: another lease '{_leaseId}' is tracked.", LogLevel.Debug);
                return false;
            }

            bool worldReady;
            try
            {
                worldReady = Context.IsWorldReady;
            }
            catch
            {
                worldReady = false; // Headless test or pre-load safety.
            }

            bool eventUp, newDay, endOfNight;
            try
            {
                eventUp = Game1.eventUp;
                newDay = Game1.newDay;
                endOfNight = Game1.showingEndOfNightStuff;
            }
            catch
            {
                eventUp = newDay = endOfNight = true; // Fail closed when the game state is unreachable.
            }

            if (!worldReady || eventUp || newDay || endOfNight)
            {
                Log($"thinking pause begin '{leaseId}' refused: unsafe now " +
                    $"(worldReady={worldReady}, eventUp={eventUp}, newDay={newDay}, endOfNight={endOfNight}).", LogLevel.Debug);
                return false;
            }

            bool menuOpen;
            try
            {
                menuOpen = Game1.activeClickableMenu is not null;
            }
            catch
            {
                menuOpen = true;
            }

            bool priorValue;
            try
            {
                priorValue = Game1.isTimePaused;
            }
            catch
            {
                priorValue = true;
            }

            // Track the lease either way so End(leaseId) stays a valid bookkeeping path.
            _leaseId = leaseId;
            _leaseMaySetFlag = !priorValue;
            _leaseStartedUtc = DateTime.UtcNow;

            if (menuOpen || priorValue)
            {
                // Time is already stopped (menu path in shouldTimePass, or a flag set by
                // someone/something else). Never layer on top of it; never unpause behind
                // the user's own menu later — we simply hold no flag.
                _weSetPauseFlag = false;
                Log($"thinking pause begin '{leaseId}' tracked-but-inactive " +
                    $"(menuOpen={menuOpen}, priorIsTimePaused={priorValue}); the clock is already stopped.", LogLevel.Debug);
                return false;
            }

            Game1.isTimePaused = true;
            _weSetPauseFlag = true;
            Log($"thinking pause began (lease='{leaseId}', max {_maxLease.TotalSeconds:0}s).");
            return true;
        }
    }

    /// <summary>
    /// Ends a lease by id. A stale or unknown id (including a null tracked lease) is a
    /// logged no-op — stale releases from aborted provider calls must never clear a
    /// fresher lease. When this controller set the pause flag itself it restores the
    /// prior (clear) value; a flag it did not set is never touched.
    /// </summary>
    /// <summary>
    /// Releases the clock while a native machine executes, including a failure-owned
    /// hold: recovery actions (walking to the bed, answering the native sleep
    /// question) need a live world — the 1.6 Sleep touch action requires
    /// <c>Game1.shouldTimePass(false)</c>, which is false while the clock is held.
    /// Only flags this controller owns are touched; the failure hold is re-asserted
    /// by <see cref="ResumeAfterAction"/> when the action ends and the stop stands.
    /// </summary>
    public void SuspendForAction()
    {
        lock (_leaseLock) {
            if (_weSetPauseFlag) { Game1.isTimePaused=false; _weSetPauseFlag=false; }
            if (_failureReason != null && _failureOwnsFlag) {
                Game1.isTimePaused = false;
                _failureOwnsFlag = false;
                _failureSuspendedForAction = true;
            }
        }
    }
    public void ResumeAfterAction()
    {
        lock (_leaseLock) {
            if (_failureReason != null && _failureSuspendedForAction) {
                _failureSuspendedForAction = false;
                if (_leaseId == null && !Game1.isTimePaused && !Game1.eventUp && !Game1.newDay && !Game1.showingEndOfNightStuff && Game1.activeClickableMenu == null) {
                    Game1.isTimePaused = true; _failureOwnsFlag = true;
                }
            }
            if (_leaseId != null && _leaseMaySetFlag && !_weSetPauseFlag && !Game1.isTimePaused && !Game1.eventUp && !Game1.newDay && !Game1.showingEndOfNightStuff && Game1.activeClickableMenu == null) {
                Game1.isTimePaused=true; _weSetPauseFlag=true;
            }
        }
    }
    public void End(string leaseId)
    {
        lock (_leaseLock)
        {
            if (_leaseId is null || !string.Equals(_leaseId, leaseId, StringComparison.Ordinal))
            {
                Log($"thinking pause end '{leaseId}' ignored: stale or unknown lease (active='{_leaseId ?? "none"}').", LogLevel.Debug);
                return;
            }

            ClearOwnFlagUnlocked();
            Log($"thinking pause ended (lease='{leaseId}').", LogLevel.Debug);
        }
    }

    public bool Renew(string leaseId)
    {
        lock (_leaseLock) {
            // Renewal is not acquisition: an ended/expired lease can never revive.
            if (_leaseId == null || !string.Equals(_leaseId,leaseId,StringComparison.Ordinal)
                || DateTime.UtcNow-_leaseStartedUtc > _maxLease) return false;
            _leaseStartedUtc=DateTime.UtcNow; return true;
        }
    }

    /// <summary>
    /// Lease expiry, driven once per game tick from SMAPI <c>UpdateTicked</c>. When the
    /// tracked lease is older than <c>maxLease</c> it is force-released with a Warn log
    /// so a crashed or cancelled provider call can never pause the clock indefinitely.
    /// </summary>
    public void Tick()
    {
        lock (_leaseLock)
        {
            // The failure hold is re-asserted every tick so nothing else can silently
            // unpause behind it — except while a native recovery action legitimately
            // runs with a live clock (SuspendForAction owns that window).
            if (_failureReason != null && !_failureSuspendedForAction && Context.IsWorldReady && !Game1.IsMultiplayer && !Game1.isTimePaused)
            { Game1.isTimePaused = true; _failureOwnsFlag = true; }
            if (_leaseId is null)
                return;

            if (DateTime.UtcNow - _leaseStartedUtc <= _maxLease)
                return;

            Log($"thinking pause lease '{_leaseId}' exceeded max duration {_maxLease}; releasing (lease-expired).", LogLevel.Warn);
            ClearOwnFlagUnlocked();
        }
    }

    /// <summary>
    /// Idempotent full release, wired by integration to human takeover, transport
    /// disconnect, <c>DayEnding</c>, <c>Saving</c> and <c>ReturnedToTitle</c>. Clears the
    /// pause flag only if this controller set it; safe to call with no tracked lease.
    /// </summary>
    /// <param name="reason">Why the release happened (logged).</param>
    public void ReleaseAll(string reason)
    {
        lock (_leaseLock)
        {
            if (reason != "disconnected") ClearFailureStop();
            else _failureSuspendedForAction = false; // action aborted by the disconnect; Tick re-asserts the hold
            if (_leaseId is null)
            {
                Log($"thinking pause release-all ignored: no lease tracked (reason='{reason}').", LogLevel.Debug);
                return;
            }

            Log($"thinking pause force-released (reason='{reason}', lease='{_leaseId}').", LogLevel.Debug);
            ClearOwnFlagUnlocked();
        }
    }

    /// <summary>
    /// Precondition: called with <see cref="_leaseLock"/> held. Clears <c>Game1.isTimePaused</c>
    /// only when this controller set it, then drops all lease bookkeeping.
    /// </summary>
    private void ClearOwnFlagUnlocked()
    {
        if (_weSetPauseFlag)
        {
            try
            {
                if (Game1.isTimePaused)
                    Game1.isTimePaused = false;
            }
            catch
            {
                // Headless test or unload path: the flag is process-wide and the game is gone.
            }
        }

        _weSetPauseFlag = false;
        _leaseId = null;
        _leaseMaySetFlag = false;
        _leaseStartedUtc = default;
    }
}
