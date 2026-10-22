using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Menus;
using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>Request DTO for the player-mode "sleep" skill (native bed sleep → native new day).</summary>
/// <param name="CommandId">Inbound command id, echoed into the result for correlation.</param>
/// <param name="TaskId">Task id shared with the coordinator's one-active-task invariant.</param>
/// <param name="IdempotencyKey">Optional transport idempotency key, echoed into the result.</param>
/// <param name="ExpectedWorldRevision">World revision the caller planned against (informational; observers bump it).</param>
/// <param name="MaxGameMinutes">Game-clock budget for the whole sleep flow (also passed to the navigation leg).</param>
/// <param name="CancelPolicy">Cancellation policy label, mirrors the other skill requests.</param>
public sealed record PlayerSleepRequest(
    string CommandId,
    string TaskId,
    string? IdempotencyKey = null,
    long ExpectedWorldRevision = 1,
    int MaxGameMinutes = 0,
    string CancelPolicy = "safe-point"
);

/// <summary>Terminal result of a "sleep" skill execution.</summary>
/// <param name="CommandId">Inbound command id, echoed for correlation.</param>
/// <param name="TaskId">Task id shared with the coordinator.</param>
/// <param name="FinalState">Terminal execution state.</param>
/// <param name="StartDay"><c>Game1.Date.TotalDays</c> captured when execution started.</param>
/// <param name="EndDay"><c>Game1.Date.TotalDays</c> observed at termination (equals <paramref name="StartDay"/> unless the day actually rolled).</param>
/// <param name="DayAdvancedVia">"none", "native-sleep" (answered the bed question) or "native-pass-out" (2AM pass-out en route; the native gold fee applies).</param>
/// <param name="BedTile">The farmhouse bed tile that was targeted, when it could be resolved.</param>
/// <param name="GameMinutesElapsed">Game-clock minutes elapsed between start and termination.</param>
/// <param name="FinalWorldRevision">Observer world revision at termination.</param>
/// <param name="ErrorMessage">Human-readable failure detail, null on success.</param>
/// <param name="ErrorCode">Machine-readable failure code, null on success.</param>
public sealed record PlayerSleepResult(
    string CommandId,
    string TaskId,
    ExecutionState FinalState,
    int StartDay,
    int EndDay,
    string DayAdvancedVia,
    TileCoordinate? BedTile,
    int GameMinutesElapsed,
    long FinalWorldRevision,
    string? ErrorMessage = null,
    string? ErrorCode = null
)
{
    /// <summary>Converts this result into the canonical protocol SkillResultPayload DTO.</summary>
    public SkillResultPayload ToTransportPayload()
    {
        string terminalState = FinalState switch
        {
            ExecutionState.Succeeded => "succeeded",
            ExecutionState.PartiallySucceeded => "partially-succeeded",
            ExecutionState.Cancelled => "cancelled",
            ExecutionState.Rejected => "rejected",
            _ => "failed"
        };

        var effects = new List<Dictionary<string, object>>();
        if (FinalState is ExecutionState.Succeeded or ExecutionState.PartiallySucceeded)
        {
            effects.Add(new Dictionary<string, object>
            {
                ["kind"] = "day-advanced",
                ["via"] = DayAdvancedVia,
                ["startDay"] = StartDay,
                ["endDay"] = EndDay
            });
        }

        var details = new Dictionary<string, object>
        {
            ["startDay"] = StartDay,
            ["endDay"] = EndDay,
            ["dayAdvancedVia"] = DayAdvancedVia
        };
        if (BedTile is { } bedTile)
        {
            details["bedTile"] = new Dictionary<string, object> { ["x"] = bedTile.X, ["y"] = bedTile.Y };
        }

        SkillResultResources? resources = new(0f, 0, GameMinutesElapsed);
        SkillResultError? error = ErrorMessage is not null
            ? new(ErrorCode ?? "EXECUTION_ERROR", ErrorMessage, null, Retryable: false)
            : null;

        bool dayRolled = EndDay != StartDay;
        return new SkillResultPayload(
            CommandId: CommandId,
            TaskId: TaskId,
            TerminalState: terminalState,
            CompletedCount: FinalState == ExecutionState.Succeeded && dayRolled ? 1 : 0,
            SkippedCount: 0,
            FailedCount: FinalState == ExecutionState.Failed ? 1 : 0,
            FinalWorldRevision: FinalWorldRevision,
            Effects: effects,
            Resources: resources,
            Error: error,
            RetryRecommended: false,
            PlayerActionRequired: false,
            SkillId: "sleep",
            Details: details
        );
    }
}

/// <summary>
/// Tick-driven execution state machine for the player-mode "sleep" skill. The bound actor
/// IS the real local player, so the whole flow is deliberately native end to end: navigate
/// onto the farmhouse bed tile (walkable in 1.6 — stepping on it fires the native Sleep
/// touch action which opens the yes/no question dialogue), answer through
/// <c>GameLocation.answerDialogueAction("Sleep_Yes", ...)</c> (the public native handler
/// that runs startSleep → doSleep → <c>Game1.NewDay</c>), then wait out the native
/// end-of-night menu stack (shipping / save / level-up), confirming native controls
/// without bypassing the native save enumerator, until the day has
/// actually rolled and the player regains control. No teleporting, no date edits, no
/// resource grants: a pass-out en route (2AM) is detected as a day change before the
/// question was answered and reported as an unsuccessful sleep with observed day advance via "native-pass-out"
/// (the native gold fee applies).
///
/// Preconditions fail explicitly with real causes: the actor must be the player-bound
/// actor (<c>ActorGuards.IsPlayerBound</c>), the world must be ready, no event may be up
/// and no menu may be open (festivals always imply an event or a menu, so they are covered
/// without a dedicated check), and the farmhouse bed must resolve via
/// <c>FarmHouse.GetPlayerBed()</c>.
///
/// Cancellation stops the embedded navigation, halts the actor and, if the native sleep
/// question is still open, answers "Sleep_No" (a native no-op — the key exists but has no
/// handler, so nothing sleeps) and closes the dialogue box via its own cleanup path.
///
/// The embedded <see cref="NavigationStateMachine"/> must be a DEDICATED instance created
/// by the wiring layer (like <c>CompanionRestController</c>'s), never the coordinator's
/// shared navigation machine, and only this machine drives it. Completion is signalled to
/// the coordinator through <see cref="OnCompleted"/>; the coordinator mirrors the other
/// skills: <c>HandleSleepCompleted</c> → <c>CompleteExecution(result.ToTransportPayload())</c>,
/// plus <c>Update</c> fan-out, <c>ActiveMachine</c>/<c>TryCheckIdle</c> membership and a
/// <c>TryStartSleep</c> entry point with the authority gate.
/// </summary>
public sealed class PlayerSleepStateMachine : ISkillExecutionMachine
{
    private const long MaxMonotonicTicks = 10800; // ~3 min of ticks; the navigation leg has its own tighter caps.
    private const int QuestionWaitTicks = 180;    // ~3s for the native Sleep question dialogue to appear.
    private const int NewDayWaitTicks = 1800;     // ~30s for the end-of-night menu stack to clear.

    private const string PhaseIdle = "idle";
    private const string PhaseNavigatingToBed = "navigating-to-bed";
    private const string PhaseAwaitingQuestion = "awaiting-question";
    private const string PhaseBedReentry = "bed-reentry";
    private const string PhaseAnswering = "answering";
    private const string PhaseWaitingForMorning = "waiting-for-morning";

    private readonly IFarmerActor _actor;
    private readonly IWorldObserver _observer;
    private readonly NavigationStateMachine _navigation;
    private readonly CompanionAvatar? _avatar;
    private readonly Action<string, LogLevel>? _log;

    private readonly object _stateLock = new();

    private PlayerSleepRequest? _currentRequest;
    private string _phase = PhaseIdle;
    private TileCoordinate? _bedTile;
    private string? _bedLocationName;
    private int _startDay;
    private string _dayAdvancedVia = "none";
    private int _startClock;
    private long _elapsedTicks;
    private int _phaseTicks;
    private int _bedReentryAttempts;
    private bool _reentryReturnLeg;
    private bool _approachingBedFromOutside;
    // A timeout ends a command, not the already committed native overnight flow.
    // Keep its provenance separately from FinalResult (which a rejected retry resets).
    private sealed record PendingOvernight(Farmer Farmer, ulong SaveId, int StartDay,
        int StartClock, string Via, TileCoordinate? BedTile, string? BedLocation);
    private PendingOvernight? _pendingOvernight;

    /// <summary>Only this actor/save's already committed overnight may bypass a menu admission block.</summary>
    public bool CanResumeOwnedOvernight
    {
        get
        {
            lock (_stateLock)
            {
                try
                {
                    if (_pendingOvernight is not { } pending || !ReferenceEquals(Game1.player, pending.Farmer)
                        || Game1.uniqueIDForThisGame != pending.SaveId || Game1.eventUp)
                        return false;
                    int day = Game1.Date.TotalDays;
                    return day >= pending.StartDay && day <= pending.StartDay + 1
                        && (Game1.showingEndOfNightStuff || Game1.newDay || Game1.newDaySync?.hasInstance() == true
                            || Game1.activeClickableMenu is ShippingMenu or SaveGameMenu or LevelUpMenu
                            || day == pending.StartDay + 1 && Game1.activeClickableMenu is DialogueBox { isQuestion: false, characterDialogue: null }
                            || day == pending.StartDay + 1 && Game1.activeClickableMenu is null);
                }
                catch { return false; }
            }
        }
    }

    private volatile bool _cancelRequested;
    private volatile string? _cancelReason;
    private volatile bool _pauseRequested;

    /// <summary>Current execution state, using the shared lifecycle enum.</summary>
    public ExecutionState CurrentState { get; private set; } = ExecutionState.Created;

    /// <inheritdoc />
    public bool IsExecuting => CurrentState is ExecutionState.Validating or ExecutionState.Preparing
        or ExecutionState.Navigating or ExecutionState.Acting or ExecutionState.Verifying
        or ExecutionState.Paused or ExecutionState.Cancelling;

    /// <inheritdoc />
    public bool IsPaused => CurrentState == ExecutionState.Paused;

    /// <summary>Terminal result once finished, else null.</summary>
    public PlayerSleepResult? FinalResult { get; private set; }

    /// <summary>The request currently being executed, else null.</summary>
    public PlayerSleepRequest? CurrentRequest => _currentRequest;

    /// <summary>Fine-grained sleep flow phase (idle → navigating-to-bed → awaiting-question → answering → waiting-for-morning).</summary>
    public string Phase => _phase;

    /// <inheritdoc />
    public string? ActiveTaskId => _currentRequest?.TaskId;

    /// <inheritdoc />
    public int TotalTargets => 1;

    /// <inheritdoc />
    public int CurrentTargetIndex => IsExecuting ? 0 : (FinalResult is null ? 0 : 1);

    /// <summary>Fired synchronously when execution reaches a terminal state.</summary>
    public event Action<PlayerSleepResult>? OnCompleted;

    /// <summary>Fired for player-facing status messages (forwarded by the coordinator).</summary>
    public event Action<string>? OnNotification;

    private void NotifyPlayer(string message) => OnNotification?.Invoke(message);

    /// <summary>
    /// Creates the machine. <paramref name="navigation"/> must be a dedicated
    /// <see cref="NavigationStateMachine"/> instance owned by this machine (driven only
    /// from <see cref="Update"/>), never the coordinator's shared one.
    /// </summary>
    public PlayerSleepStateMachine(
        IFarmerActor actor,
        IWorldObserver observer,
        NavigationStateMachine navigation,
        CompanionAvatar? avatar = null,
        Action<string, LogLevel>? log = null)
    {
        _actor = actor ?? throw new ArgumentNullException(nameof(actor));
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _navigation = navigation ?? throw new ArgumentNullException(nameof(navigation));
        _avatar = avatar;
        _log = log;
    }

    private void Log(string message, LogLevel level = LogLevel.Info) => _log?.Invoke(message, level);

    /// <summary>
    /// Validates preconditions and, when they hold, starts the embedded navigation to the
    /// farmhouse bed tile. On rejection/failure the terminal result is returned through
    /// <paramref name="earlyTerminalResult"/> and the method returns false, mirroring the
    /// other state machines' start contract.
    /// </summary>
    public bool Start(PlayerSleepRequest request, out PlayerSleepResult? earlyTerminalResult, bool answerExistingQuestion = false)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_stateLock)
        {
            _currentRequest = request;
            _phase = PhaseIdle;
            _bedTile = null;
            _bedLocationName = null;
            _startDay = 0;
            _dayAdvancedVia = "none";
            _startClock = _observer.TimeOfDay;
            _elapsedTicks = 0;
            _phaseTicks = 0;
            _bedReentryAttempts = 0;
            _reentryReturnLeg = false;
            _approachingBedFromOutside = false;
            _cancelRequested = false;
            _cancelReason = null;
            _pauseRequested = false;
            FinalResult = null;

            _actor.SetActiveTask(request.TaskId);
            CurrentState = ExecutionState.Validating;

            if (!ActorGuards.IsPlayerBound(_actor))
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    "The 'sleep' skill requires the player-bound mechanics actor (ActorGuards.IsPlayerBound).",
                    "ACTOR_NOT_PLAYER_BOUND");
                return false;
            }

            bool worldReady;
            try
            {
                worldReady = Context.IsWorldReady && Game1.player is not null;
            }
            catch
            {
                worldReady = false;
            }

            if (!worldReady)
            {
                CurrentState = ExecutionState.Failed;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Failed,
                    "The world is not ready; cannot start native sleep.", "WORLD_NOT_READY");
                return false;
            }

            if (TryResumeOvernightUnlocked())
            {
                earlyTerminalResult = null;
                return true;
            }

            if (Game1.eventUp)
            {
                CurrentState = ExecutionState.Failed;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Failed,
                    "An event or cutscene is active; native sleep is unavailable.", "EVENT_ACTIVE");
                return false;
            }

            if (Game1.activeClickableMenu is not null && !(answerExistingQuestion && SleepQuestionOpenUnlocked()))
            {
                CurrentState = ExecutionState.Failed;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Failed,
                    "A menu is already open; close it before starting native sleep.", "MENU_OPEN");
                return false;
            }

            var house = Game1.getLocationFromName("FarmHouse") as FarmHouse;
            var bed = house?.GetPlayerBed();
            if (house is null || bed is null)
            {
                CurrentState = ExecutionState.Failed;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Failed,
                    "No player bed found in the FarmHouse.", "BED_NOT_FOUND");
                return false;
            }

            var spot = bed.GetBedSpot();
            _bedTile = new TileCoordinate(spot.X, spot.Y);
            _bedLocationName = house.NameOrUniqueName;
            try
            {
                _startDay = Game1.Date.TotalDays;
            }
            catch
            {
                _startDay = 0;
            }

            if (answerExistingQuestion)
            {
                if (!SleepQuestionOpenUnlocked())
                {
                    earlyTerminalResult = FinishExecution(ExecutionState.Rejected, "The original Sleep question is no longer open.", "NOT_A_SLEEP_QUESTION");
                    return false;
                }
                _phase = PhaseAnswering;
                CurrentState = ExecutionState.Acting;
                earlyTerminalResult = null;
                return true;
            }
            CurrentState = ExecutionState.Preparing;
            var initialTarget = _bedTile.Value;
            if (!string.Equals(_actor.LocationName, _bedLocationName, StringComparison.OrdinalIgnoreCase))
            {
                // Native bed collision depends on the farmer actually being in
                // the house. First walk onto ordinary observed floor, then plan
                // the owned-bed touch action using the current native location.
                TileCoordinate? approach = null;
                for (int radius = 1; radius <= 2 && approach is null; radius++)
                    for (int dx = -radius; dx <= radius && approach is null; dx++)
                        for (int dy = -radius; dy <= radius && approach is null; dy++)
                        {
                            if (Math.Abs(dx) + Math.Abs(dy) != radius) continue;
                            var tile = new TileCoordinate(initialTarget.X + dx, initialTarget.Y + dy);
                            if (_observer.IsTilePassable(_bedLocationName, tile)
                                && !_observer.IsWarpOrDoorTile(_bedLocationName, tile)) approach = tile;
                        }
                if (approach is null)
                {
                    earlyTerminalResult = FinishExecution(ExecutionState.Failed,
                        "No native walkable floor approach beside the owned bed.", "BED_APPROACH_UNAVAILABLE");
                    return false;
                }
                initialTarget = approach.Value;
                _approachingBedFromOutside = true;
            }
            var navRequest = new NavigationRequest(
                request.CommandId,
                request.TaskId,
                _bedLocationName,
                initialTarget,
                request.MaxGameMinutes);
            if (!_navigation.Start(navRequest, out var navigationEarlyResult))
            {
                CurrentState = ExecutionState.Failed;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Failed,
                    $"Cannot reach the farmhouse bed: {navigationEarlyResult?.ErrorMessage ?? "navigation start rejected."}",
                    navigationEarlyResult?.ErrorCode ?? "NAVIGATION_START_FAILED");
                return false;
            }

            _phase = PhaseNavigatingToBed;
            CurrentState = ExecutionState.Navigating;
            Log($"sleep accepted: navigating to bed at '{_bedLocationName}' {_bedTile} (commandId={request.CommandId}, task={request.TaskId}).");

            earlyTerminalResult = null;
            return true;
        }
    }

    /// <inheritdoc />
    public void Update(GameTime? time, long tickCount)
    {
        lock (_stateLock)
        {
            if (!IsExecuting || _currentRequest is null)
                return;

            if (_cancelRequested)
            {
                HandleCancelUnlocked();
                return;
            }

            if (IsPaused)
                return;

            if (_pauseRequested)
            {
                _pauseRequested = false;
                if (_phase == PhaseWaitingForMorning) return; // Native save is already committed; it must finish.
                CurrentState = ExecutionState.Paused;
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                if (_phase == PhaseNavigatingToBed && _navigation.IsExecuting)
                    _navigation.RequestPause();
                NotifyPlayer("AI Companion: Sleep task paused.");
                return;
            }

            // Keep ticking through end-of-night even though Game1.paused may be true during
            // the sleep fade — closing those menus is this machine's whole job there.
            try
            {
                if (!Context.IsWorldReady)
                    return;
            }
            catch
            {
                return;
            }

            _elapsedTicks++;
            if (_elapsedTicks > MaxMonotonicTicks)
            {
                FinishExecution(ExecutionState.Failed, "Monotonic tick limit exceeded.", "TIMEOUT");
                return;
            }

            switch (_phase)
            {
                case PhaseNavigatingToBed:
                    TickNavigatingToBed(time, tickCount);
                    break;
                case PhaseAwaitingQuestion:
                    TickAwaitingQuestion();
                    break;
                case PhaseBedReentry:
                    TickBedReentry(time, tickCount);
                    break;
                case PhaseAnswering:
                    DriveNavigationToTerminalUnlocked(time, tickCount);
                    TickAnswering();
                    break;
                case PhaseWaitingForMorning:
                    DriveNavigationToTerminalUnlocked(time, tickCount);
                    TickWaitingForMorning();
                    break;
            }
        }
    }

    /// <summary>
    /// Precondition: called with <see cref="_stateLock"/> held. The embedded navigation was
    /// cancelled when the question appeared or the day rolled; keep feeding it ticks until it
    /// reaches its own terminal state so its bookkeeping (including actor task cleanup) completes.
    /// </summary>
    private void DriveNavigationToTerminalUnlocked(GameTime? time, long tickCount)
    {
        if (_navigation.IsExecuting)
            _navigation.Update(time, tickCount);
    }

    private void TickNavigatingToBed(GameTime? time, long tickCount)
    {
        // 2AM pass-out wins: the native day rollover happened without us ever answering.
        if (DayRolledUnlocked())
        {
            _dayAdvancedVia = "native-pass-out";
            EnterWaitingForMorningUnlocked();
            return;
        }

        // The bed touch action can fire while the navigation machine is still settling.
        if (SleepQuestionOpenUnlocked())
        {
            if (_navigation.IsExecuting)
                _navigation.RequestCancel("sleep-question-shown");
            _phase = PhaseAnswering;
            CurrentState = ExecutionState.Acting;
            return;
        }

        if (!_navigation.IsExecuting)
        {
            var navigationResult = _navigation.FinalResult;
            if (navigationResult is null)
                return;

            if (navigationResult.FinalState is ExecutionState.Succeeded or ExecutionState.PartiallySucceeded)
            {
                if (_approachingBedFromOutside)
                {
                    _approachingBedFromOutside = false;
                    if (!string.Equals(_actor.LocationName, _bedLocationName, StringComparison.OrdinalIgnoreCase))
                    {
                        FinishExecution(ExecutionState.Failed, "Native farmhouse approach did not enter the house.", "BED_APPROACH_NOT_REACHED");
                        return;
                    }
                    var bedRequest = new NavigationRequest(_currentRequest!.CommandId,
                        _currentRequest.TaskId, _bedLocationName!, _bedTile!.Value, _currentRequest.MaxGameMinutes);
                    if (!_navigation.Start(bedRequest, out var early))
                        FinishExecution(ExecutionState.Failed, early?.ErrorMessage ?? "Native bed entry could not start.",
                            early?.ErrorCode ?? "BED_ENTRY_REJECTED");
                    return;
                }
                if (_bedTile == null || _actor.Tile != _bedTile.Value) {
                    FinishExecution(ExecutionState.Failed,"Navigation adjusted away from the native bed spot.","BED_NOT_REACHED"); return;
                }
                _phase = PhaseAwaitingQuestion;
                CurrentState = ExecutionState.Acting;
                _phaseTicks = 0;
            }
            else
            {
                FinishExecution(ExecutionState.Failed,
                    $"Could not reach the bed: {navigationResult.ErrorMessage ?? "navigation failed."}",
                    navigationResult.ErrorCode ?? "NAVIGATION_FAILED");
            }

            return;
        }

        _navigation.Update(time, tickCount);
    }

    private void TickAwaitingQuestion()
    {
        if (DayRolledUnlocked())
        {
            _dayAdvancedVia = "native-pass-out";
            EnterWaitingForMorningUnlocked();
            return;
        }

        if (SleepQuestionOpenUnlocked())
        {
            _phase = PhaseAnswering;
            return;
        }

        _phaseTicks++;
        if (_phaseTicks > QuestionWaitTicks)
        {
            if (_bedTile != null && _bedReentryAttempts < MaxBedReentryAttempts && TryStartBedReentryUnlocked())
            {
                _bedReentryAttempts++;
                _reentryReturnLeg = false;
                _phase = PhaseBedReentry;
                _phaseTicks = 0;
                CurrentState = ExecutionState.Navigating;
                Log("sleep question did not appear; stepping off the bed tile and back on to re-trigger the native touch action.", LogLevel.Warn);
                return;
            }
            FinishExecution(ExecutionState.Failed,
                "The native sleep question dialogue did not appear after reaching the bed (including one off-and-on re-entry).",
                "sleep-question-not-shown");
        }
    }

    /// <summary>At most one native re-entry recovery per sleep execution.</summary>
    private const int MaxBedReentryAttempts = 1;

    /// <summary>
    /// Native touch actions fire on tile ENTRY: <c>GameLocation.lastTouchActionLocation</c>
    /// only resets when the farmer changes tiles, and the 1.6 Sleep touch additionally
    /// requires <c>Game1.shouldTimePass(false)</c> — a held clock suppresses it. When the
    /// actor already stands on the bed spot (a previous navigation ended there, or the
    /// touch was suppressed), no pending entry event exists, so walk one tile off and
    /// back on through the real navigation machine to reproduce the native entry.
    /// </summary>
    private bool TryStartBedReentryUnlocked()
    {
        var bed = _bedTile!.Value;
        foreach (var (dx, dy) in new[] { (0, 1), (1, 0), (0, -1), (-1, 0) })
        {
            var off = new TileCoordinate(bed.X + dx, bed.Y + dy);
            if (off == _actor.Tile || !_observer.IsTilePassable(_bedLocationName!, off))
                continue;
            var request = new NavigationRequest(
                _currentRequest!.CommandId, _currentRequest.TaskId,
                _bedLocationName!, off, _currentRequest.MaxGameMinutes);
            return _navigation.Start(request, out _);
        }
        return false;
    }

    private void TickBedReentry(GameTime? time, long tickCount)
    {
        if (DayRolledUnlocked())
        {
            _dayAdvancedVia = "native-pass-out";
            EnterWaitingForMorningUnlocked();
            return;
        }

        if (SleepQuestionOpenUnlocked())
        {
            if (_navigation.IsExecuting)
                _navigation.RequestCancel("sleep-question-shown");
            _phase = PhaseAnswering;
            CurrentState = ExecutionState.Acting;
            return;
        }

        if (_navigation.IsExecuting)
        {
            _navigation.Update(time, tickCount);
            return;
        }

        var result = _navigation.FinalResult;
        if (result is null)
            return;
        if (result.FinalState is not (ExecutionState.Succeeded or ExecutionState.PartiallySucceeded))
        {
            FinishExecution(ExecutionState.Failed,
                $"Bed re-entry walk failed: {result.ErrorMessage ?? "navigation failed."}",
                result.ErrorCode ?? "NAVIGATION_FAILED");
            return;
        }
        if (!_reentryReturnLeg)
        {
            _reentryReturnLeg = true;
            var back = new NavigationRequest(
                _currentRequest!.CommandId, _currentRequest.TaskId,
                _bedLocationName!, _bedTile!.Value, _currentRequest.MaxGameMinutes);
            if (!_navigation.Start(back, out var early))
            {
                FinishExecution(ExecutionState.Failed,
                    $"Bed re-entry return walk rejected: {early?.ErrorMessage ?? "navigation start rejected."}",
                    early?.ErrorCode ?? "NAVIGATION_START_FAILED");
            }
            return;
        }

        // Both legs complete; the bed tile entry now has a live chance to fire the
        // native touch action — arm the question wait one more time.
        _phase = PhaseAwaitingQuestion;
        CurrentState = ExecutionState.Acting;
        _phaseTicks = 0;
    }

    private void TickAnswering()
    {
        // The public native handler: "Sleep_Yes" runs startSleep → doSleep → Game1.NewDay.
        bool accepted;
        var answeredQuestion=Game1.activeClickableMenu;
        try
        {
            accepted = Game1.currentLocation.answerDialogueAction("Sleep_Yes", null);
            // The normal DialogueBox response path also cleans up its question.
            // Only close that exact menu, never a newly opened overnight screen.
            if (accepted && ReferenceEquals(Game1.activeClickableMenu,answeredQuestion) && answeredQuestion is DialogueBox { isQuestion:true })
                ((DialogueBox)answeredQuestion).closeDialogue();
        }
        catch (Exception ex)
        {
            FinishExecution(ExecutionState.Failed,
                $"Answering the native sleep question threw: {ex.Message}",
                "sleep-answer-failed");
            return;
        }

        if (!accepted)
        {
            FinishExecution(ExecutionState.Failed,
                "The native sleep dialogue handler rejected the 'Sleep_Yes' answer.",
                "sleep-answer-rejected");
            return;
        }

        _dayAdvancedVia = "native-sleep";
        EnterWaitingForMorningUnlocked();
    }

    private void TickWaitingForMorning()
    {
        _phaseTicks++;
        if (_phaseTicks > NewDayWaitTicks)
        {
            FinishExecution(ExecutionState.Failed,
                $"Timed out waiting for the native overnight save (menu={Game1.activeClickableMenu?.GetType().Name ?? "none"}, " +
                $"paused={Game1.paused}, windowActive={Game1.game1.IsActive}, " +
                $"outOfFocusPause={Game1.options.pauseWhenOutOfFocus}, endOfNight={Game1.showingEndOfNightStuff}). " +
                "The committed sleep flow can be resumed by sleep; the visible new date alone is not a saved day.",
                "new-day-timeout");
            return;
        }

        // Close at most ONE end-of-night menu per tick via the native menu exit — the same
        // code path as the player pressing OK/ESC on it.
        try
        {
            // Native system notices (e.g. the Summer 3 earthquake) can precede
            // ShippingMenu with endOfNight still false. They contain no choice.
            // A committed sleep owns this mechanical continuation only after
            // the day rolled; NPC dialogue and questions remain model-owned.
            if (DayRolledUnlocked() && Game1.activeClickableMenu is DialogueBox { isQuestion: false, characterDialogue: null } notice
                && !Game1.eventUp)
            {
                notice.receiveLeftClick(notice.x + notice.width / 2, notice.y + notice.height / 2, true);
                return;
            }
            if (Game1.activeClickableMenu is SaveGameMenu) return; // Native update owns saving and closure; never skip its enumerator.
            if (Game1.activeClickableMenu is ShippingMenu shipping)
            {
                // Escape is ignored when native gamepad controls are active. Use
                // the actual visible native button and let ShippingMenu update
                // run its outro, nested SaveGameMenu and new-day synchronization.
                if (shipping.CanReceiveInput())
                {
                    var button = shipping.currentPage == -1 ? shipping.okButton : shipping.backButton;
                    if (button is not null)
                        shipping.receiveLeftClick(button.bounds.Center.X, button.bounds.Center.Y);
                }
                return;
            }
            if (Game1.activeClickableMenu is LevelUpMenu)
            {
                Game1.activeClickableMenu.receiveKeyPress(Microsoft.Xna.Framework.Input.Keys.Escape); // Native semantic confirm, not OS input.
                return;
            }
        }
        catch
        {
            // Menu closed between checks; nothing to do this tick.
        }

        if (!DayRolledUnlocked())
            return;

        try
        {
            if (Game1.showingEndOfNightStuff || Game1.newDay || Game1.newDaySync?.hasInstance() == true)
                return;
            if (Game1.activeClickableMenu is not null)
                return;
            if (Game1.player is not null && !Game1.player.CanMove)
                return;
        }
        catch
        {
            return;
        }

        if (_dayAdvancedVia != "native-sleep")
            FinishExecution(ExecutionState.PartiallySucceeded,
                "The native day was saved by pass-out before answering the own-bed sleep question; this is not successful sleep.",
                "NATIVE_PASS_OUT");
        else
            FinishExecution(ExecutionState.Succeeded, null, null);
    }

    private void EnterWaitingForMorningUnlocked()
    {
        if (_navigation.IsExecuting)
            _navigation.RequestCancel("day-advanced");

        _phase = PhaseWaitingForMorning;
        CurrentState = ExecutionState.Verifying;
        _phaseTicks = 0;
        _pendingOvernight ??= new PendingOvernight(Game1.player, Game1.uniqueIDForThisGame,
            _startDay, _startClock, _dayAdvancedVia, _bedTile, _bedLocationName);
    }

    private bool TryResumeOvernightUnlocked()
    {
        if (_pendingOvernight is not { } pending)
            return false;
        int day = Game1.Date.TotalDays;
        if (!ReferenceEquals(Game1.player, pending.Farmer) || Game1.uniqueIDForThisGame != pending.SaveId
            || day < pending.StartDay || day > pending.StartDay + 1)
        {
            _pendingOvernight = null;
            return false;
        }
        // Only this machine's committed transition can be resumed. An arbitrary
        // open menu or visible date is never evidence of a successful sleep.
        if (!CanResumeOwnedOvernight)
            return false;
        _startDay = pending.StartDay;
        _startClock = pending.StartClock;
        _dayAdvancedVia = pending.Via;
        _bedTile = pending.BedTile;
        _bedLocationName = pending.BedLocation;
        EnterWaitingForMorningUnlocked();
        Log($"sleep resumed its owned native overnight transition (day {_startDay}->{day}).");
        return true;
    }

    private void HandleCancelUnlocked()
    {
        _cancelRequested = false;
        if (_phase == PhaseWaitingForMorning) {
            Log("Native overnight transition already committed; completing its save before reporting the observed result.");
            return;
        }
        _actor.Halt();
        _avatar?.SetAnimation("idle");

        if (_navigation.IsExecuting)
            _navigation.RequestCancel(_cancelReason ?? "sleep-cancelled");

        // If the native sleep question is still open, take the "No" path: 'Sleep_No' is a
        // native no-op key (no handler in GameLocation.answerDialogueAction), so nothing
        // sleeps; the dialogue box itself is closed through its own cleanup path.
        try
        {
            if (SleepQuestionOpenUnlocked() && Game1.activeClickableMenu is DialogueBox questionBox)
            {
                try
                {
                    Game1.currentLocation?.answerDialogueAction("Sleep_No", null);
                }
                catch
                {
                    // The question may already be gone; closing the box below is what matters.
                }

                questionBox.closeDialogue();
            }
        }
        catch
        {
            // Headless or menu changed mid-cancel; the actor is already halted.
        }

        FinishExecution(ExecutionState.Cancelled, _cancelReason ?? "Execution cancelled by request.", "CANCELLED");
    }

    private PlayerSleepResult FinishExecution(
        ExecutionState terminalState,
        string? errorMessage,
        string? errorCode)
    {
        CurrentState = terminalState;
        _actor.SetActiveTask(null);
        _actor.Halt();

        int gameMinutesElapsed = IWorldObserver.CalculateGameMinutesElapsed(_startClock, _observer.TimeOfDay);
        int endDay;
        try
        {
            endDay = Game1.Date.TotalDays;
        }
        catch
        {
            endDay = _startDay;
        }

        var result = new PlayerSleepResult(
            CommandId: _currentRequest?.CommandId ?? "unknown",
            TaskId: _currentRequest?.TaskId ?? "unknown",
            FinalState: terminalState,
            StartDay: _startDay,
            EndDay: endDay,
            DayAdvancedVia: _dayAdvancedVia,
            BedTile: _bedTile,
            GameMinutesElapsed: gameMinutesElapsed,
            FinalWorldRevision: _observer.WorldRevision,
            ErrorMessage: errorMessage,
            ErrorCode: errorCode
        );

        FinalResult = result;
        if (terminalState == ExecutionState.Succeeded || errorCode == "NATIVE_PASS_OUT")
            _pendingOvernight = null;
        Log($"sleep finished: {terminalState} (via={_dayAdvancedVia}, day {_startDay}->{endDay}).");

        string statusText = terminalState switch
        {
            ExecutionState.Succeeded => "completed — a new day has started",
            ExecutionState.Cancelled => "cancelled",
            ExecutionState.Rejected => "rejected",
            _ => "failed"
        };
        NotifyPlayer($"AI Companion: Sleep task {statusText}.");
        OnCompleted?.Invoke(result);
        return result;
    }

    private bool DayRolledUnlocked()
    {
        try
        {
            return Game1.Date.TotalDays != _startDay;
        }
        catch
        {
            return false;
        }
    }

    private static bool SleepQuestionOpenUnlocked()
    {
        try
        {
            return Game1.activeClickableMenu is DialogueBox { isQuestion: true }
                && string.Equals(Game1.currentLocation?.lastQuestionKey?.Split(' ')[0], "Sleep", StringComparison.Ordinal);
        }
        catch
        {
            return false;
        }
    }

    /// <inheritdoc />
    public void RequestCancel(string reason = "Cancellation requested.")
    {
        lock (_stateLock)
        {
            if (IsExecuting && CurrentState is not (ExecutionState.Cancelling or ExecutionState.Cancelled))
            {
                _cancelRequested = true;
                _cancelReason = reason;
                Log($"sleep cancel requested: {reason} (task={ActiveTaskId}).");
            }
        }
    }

    /// <inheritdoc />
    public void RequestPause()
    {
        lock (_stateLock)
        {
            if (IsExecuting && !IsPaused)
            {
                _pauseRequested = true;
                Log($"sleep pause requested (task={ActiveTaskId}).");
            }
        }
    }

    /// <inheritdoc />
    public void Resume()
    {
        lock (_stateLock)
        {
            if (CurrentState != ExecutionState.Paused)
                return;

            CurrentState = _phase switch
            {
                PhaseNavigatingToBed => ExecutionState.Navigating,
                PhaseAwaitingQuestion or PhaseAnswering => ExecutionState.Acting,
                PhaseWaitingForMorning => ExecutionState.Verifying,
                _ => ExecutionState.Preparing
            };
            if (_phase == PhaseNavigatingToBed)
                _navigation.Resume();
            NotifyPlayer("AI Companion: Sleep task resumed.");
        }
    }
}
