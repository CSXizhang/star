using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>
/// Tick-driven lifecycle shared by the explicit native agricultural/husbandry
/// skills (refill-watering-can, apply-fertilizer, clear-debris, pickup-items,
/// insert-machine, collect-machine, pet-animal, feed-animals,
/// toggle-animal-door, collect-animal-produce).
///
/// It reuses the standard execution guarantees every other skill has: single-task
/// dispatch, safe pause/resume/cancel boundaries, measured game-clock budget,
/// monotonic timeout, per-target verification and native results only. The
/// action-specific native work lives in <see cref="INativeActionAdapter"/>; this
/// machine never fabricates an effect.
/// </summary>
public sealed class NativeActionStateMachine : ISkillExecutionMachine
{
    private const float WalkPixelsPerTick = 4f;
    private const int MaxReplansPerTarget = 3;
    private const long MaxMonotonicTicks = 3600;
    private const int ActionDurationTicks = 8;

    private readonly IFarmerActor _actor;
    private readonly CompanionAvatar? _avatar;
    private readonly IWorldObserver _observer;
    private readonly SameMapNavigator _navigator;
    private readonly INativeActionAdapter _adapter;
    private readonly Action<string, LogLevel>? _log;

    private readonly object _stateLock = new();

    private NativeActionRequest? _currentRequest;
    private List<NativeActionTarget> _pendingTargets = new();
    private int _currentTargetIndex;
    private NativeActionTarget _currentTarget = null!;
    private List<PathStep> _currentPath = new();
    private int _currentPathIndex;
    private int _replanCount;
    private int _actionTicks;
    private bool _actionEffectExecuted;
    private NativeActionStepResult? _lastStepResult;

    private readonly List<NativeActionEffect> _completed = new();
    private readonly List<NativeActionEffect> _skipped = new();
    private readonly List<NativeActionEffect> _failed = new();
    private float _staminaUsed;
    private int _waterUsed;
    private long _elapsedTicks;
    private int _startClock;
    private bool _playerActionRequired;

    private volatile bool _cancelRequested;
    private volatile string? _cancelReason;
    private volatile bool _pauseRequested;

    public ExecutionState CurrentState { get; private set; } = ExecutionState.Created;
    public bool IsExecuting => CurrentState is ExecutionState.Validating or ExecutionState.Preparing
        or ExecutionState.Navigating or ExecutionState.Facing or ExecutionState.Acting or ExecutionState.Verifying
        or ExecutionState.Paused or ExecutionState.Cancelling;
    public bool IsPaused => CurrentState == ExecutionState.Paused;
    public NativeActionResult? FinalResult { get; private set; }

    public NativeActionRequest? CurrentRequest => _currentRequest;
    public string? ActiveTaskId => _currentRequest?.TaskId;
    public int TotalTargets => _pendingTargets.Count;
    public int CurrentTargetIndex => _currentTargetIndex;

    public event Action<NativeActionResult>? OnCompleted;
    public event Action<string>? OnNotification;

    /// <summary>
    /// Structured progress for the currently running native action (Q5). Phases are
    /// the real state-machine phases: started / navigating / acting / verifying /
    /// completed / failed / skipped. <c>total</c> is the real target count.
    /// </summary>
    public event Action<NativeActionProgress>? OnProgress;

    private NativeActionProgress? _lastProgress;

    public NativeActionProgress? LastProgress => _lastProgress;

    private void EmitProgress(string phase, string? reasonCode = null)
    {
        var request = _currentRequest;
        if (request is null)
            return;

        int total = _pendingTargets.Count;
        var progress = new NativeActionProgress(
            Action: request.SkillId,
            Phase: phase,
            Completed: _completed.Count,
            Total: total,
            ReasonCode: reasonCode);
        _lastProgress = progress;
        try { OnProgress?.Invoke(progress); }
        catch (Exception ex) { Log($"Progress listener threw: {ex.Message}", LogLevel.Warn); }
    }

    public NativeActionStateMachine(
        IFarmerActor actor,
        IWorldObserver observer,
        SameMapNavigator navigator,
        INativeActionAdapter adapter,
        CompanionAvatar? avatar = null,
        Action<string, LogLevel>? log = null)
    {
        _actor = actor ?? throw new ArgumentNullException(nameof(actor));
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _navigator = navigator ?? throw new ArgumentNullException(nameof(navigator));
        _adapter = adapter ?? throw new ArgumentNullException(nameof(adapter));
        _avatar = avatar;
        _log = log;
    }

    private void Log(string message, LogLevel level = LogLevel.Info) => _log?.Invoke(message, level);

    // BeginUsingTool is the actor's watering/pour lifecycle and requires water.
    // Refill must reach its native adapter even when the real can is empty.

    public bool Start(NativeActionRequest request, out NativeActionResult? earlyTerminalResult)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_stateLock)
        {
            _currentRequest = request;
            _completed.Clear();
            _skipped.Clear();
            _failed.Clear();
            _staminaUsed = 0f;
            _waterUsed = 0;
            _elapsedTicks = 0;
            _replanCount = 0;
            _actionTicks = 0;
            _actionEffectExecuted = false;
            _lastStepResult = null;
            _playerActionRequired = false;
            _startClock = _observer.TimeOfDay;
            _cancelRequested = false;
            _cancelReason = null;
            _pauseRequested = false;
            FinalResult = null;

            CurrentState = ExecutionState.Validating;

            if (request.Targets.Count == 0)
            {
                CurrentState = ExecutionState.Rejected;
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected, "Target list cannot be empty.", "INVALID_PARAMETERS");
                return false;
            }

            // The observer's current map belongs to the player. Native actions run
            // on the companion, which may have navigated into a different interior.
            string companionLocation = _actor.LocationName;
            bool sameMap = string.Equals(companionLocation, request.LocationId, StringComparison.OrdinalIgnoreCase);
            bool selfTargeted = request.Kind == NativeActionKind.FeedAnimals;
            if (!sameMap && !selfTargeted)
            {
                CurrentState = ExecutionState.Rejected;
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    $"Location mismatch: Companion is on map '{companionLocation}', but request specifies '{request.LocationId}'.",
                    "LOCATION_MISMATCH");
                return false;
            }

            if (request.MaxGameMinutes < 1)
            {
                CurrentState = ExecutionState.Rejected;
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    $"Invalid maxGameMinutes: {request.MaxGameMinutes}. Must be at least 1.", "INVALID_PARAMETERS");
                return false;
            }

            _pendingTargets = request.Targets.ToList();
            _currentTargetIndex = 0;
            _actor.SetActiveTask(request.TaskId);
            CurrentState = ExecutionState.Preparing;
            Log($"{request.SkillId} started: {_pendingTargets.Count} target(s) on '{request.LocationId}' (commandId={request.CommandId}).");
            EmitProgress("started");

            earlyTerminalResult = null;
            return true;
        }
    }

    public void RequestPause()
    {
        lock (_stateLock)
        {
            if (IsExecuting && !IsPaused)
            {
                _pauseRequested = true;
                Log($"{_currentRequest?.SkillId} pause requested (task={ActiveTaskId}).");
            }
        }
    }

    public void Resume()
    {
        lock (_stateLock)
        {
            if (CurrentState == ExecutionState.Paused)
            {
                _pauseRequested = false;
                CurrentState = ExecutionState.Preparing;
                Log($"{_currentRequest?.SkillId} resumed (task={ActiveTaskId}).");
            }
        }
    }

    public void RequestCancel(string reason = "Cancellation requested.")
    {
        lock (_stateLock)
        {
            if (IsExecuting && CurrentState is not (ExecutionState.Cancelling or ExecutionState.Cancelled))
            {
                _cancelRequested = true;
                _cancelReason = reason;
                Log($"{_currentRequest?.SkillId} cancel requested: {reason} (task={ActiveTaskId}).");
            }
        }
    }

    public void Update(GameTime? time, long tickCount)
    {
        lock (_stateLock)
        {
            if (!IsExecuting) return;
            if (IsPaused) return;

            if (_pauseRequested && CurrentState is ExecutionState.Navigating or ExecutionState.Facing or ExecutionState.Preparing)
            {
                _pauseRequested = false;
                CurrentState = ExecutionState.Paused;
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                OnNotification?.Invoke("AI Companion: Task paused.");
                return;
            }

            if (_cancelRequested && CurrentState is not (ExecutionState.Cancelling or ExecutionState.Cancelled))
                CurrentState = ExecutionState.Cancelling;

            _elapsedTicks++;

            if (_elapsedTicks > MaxMonotonicTicks)
            {
                Log($"{_currentRequest?.SkillId} monotonic timeout exceeded ({MaxMonotonicTicks} ticks). Terminating.", LogLevel.Warn);
                FinishExecution(ExecutionState.Failed, "Monotonic tick limit exceeded.", "TIMEOUT");
                return;
            }

            int elapsedMinutes = IWorldObserver.CalculateGameMinutesElapsed(_startClock, _observer.TimeOfDay);
            if (_currentRequest is not null && elapsedMinutes > _currentRequest.MaxGameMinutes)
            {
                Log($"{_currentRequest.SkillId} budget exceeded: {elapsedMinutes} > {_currentRequest.MaxGameMinutes} minutes.", LogLevel.Warn);
                FinishExecution(_completed.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Failed,
                    "Game-clock minute budget exceeded.", "BUDGET_EXCEEDED");
                return;
            }

            try
            {
                switch (CurrentState)
                {
                    case ExecutionState.Preparing:
                        HandlePreparing();
                        break;
                    case ExecutionState.Navigating:
                        HandleNavigating();
                        break;
                    case ExecutionState.Facing:
                        HandleFacing();
                        break;
                    case ExecutionState.Acting:
                        HandleActing(time, tickCount);
                        break;
                    case ExecutionState.Verifying:
                        HandleVerifying();
                        break;
                    case ExecutionState.Cancelling:
                        FinishExecution(ExecutionState.Cancelled, _cancelReason ?? "Cancelled by request.", "CANCELLED");
                        break;
                }
            }
            catch (Exception ex)
            {
                Log($"Native action phase {CurrentState} failed: {ex}", LogLevel.Error);
                try { if (_actor.IsUsingTool) _actor.EndUsingTool(); }
                catch (Exception cleanup) { Log($"Tool cleanup failed: {cleanup.Message}", LogLevel.Warn); }
                if (_currentTarget is not null)
                    _failed.Add(new NativeActionEffect(Describe(_currentTarget), "failed", "native-action-exception", Tile: _currentTarget.Tile));
                FinishExecution(_completed.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Failed,
                    ex.Message, "NATIVE_ACTION_EXCEPTION");
            }
        }
    }

    private void HandlePreparing()
    {
        if (CheckPauseOrCancel()) return;

        if (_currentTargetIndex >= _pendingTargets.Count)
        {
            var terminal = SelectTerminalState(out var errorMessage, out var errorCode);
            FinishExecution(terminal, errorMessage, errorCode);
            return;
        }

        _currentTarget = _pendingTargets[_currentTargetIndex];
        bool selfTarget = _currentTarget.Tile == _actor.Tile;

        if (selfTarget || _actor.Tile.IsAdjacentTo(_currentTarget.Tile))
        {
            CurrentState = ExecutionState.Facing;
            return;
        }

        var pathResult = _navigator.FindPathToInteract(_actor, _currentRequest!.LocationId, _currentTarget.Tile);
        if (!pathResult.Success || pathResult.Steps.Count == 0)
        {
            _skipped.Add(new NativeActionEffect(Describe(_currentTarget), "skipped", "unreachable", Tile: _currentTarget.Tile));
            _currentTargetIndex++;
            CurrentState = ExecutionState.Preparing;
            return;
        }

        _currentPath = pathResult.Steps.ToList();
        _currentPathIndex = 0;
        _replanCount = 0;
        CurrentState = ExecutionState.Navigating;
        EmitProgress("navigating");
    }

    private void HandleNavigating()
    {
        if (_currentPathIndex >= _currentPath.Count)
        {
            _actor.Halt();
            CurrentState = _actor.Tile.IsAdjacentTo(_currentTarget.Tile) || _currentTarget.Tile == _actor.Tile
                ? ExecutionState.Facing
                : ExecutionState.Preparing;
            return;
        }

        var targetStep = _currentPath[_currentPathIndex];
        var targetTile = targetStep.Tile;
        var targetPixel = new Vector2(targetTile.X * 64, targetTile.Y * 64);
        var currentPixel = _actor.PixelPosition;

        if (_observer.IsPlayerOnTile(_currentRequest!.LocationId, targetTile))
        {
            _actor.Halt();
            _replanCount++;
            if (_replanCount > MaxReplansPerTarget)
            {
                _skipped.Add(new NativeActionEffect(Describe(_currentTarget), "skipped", "player-obstruction", Tile: _currentTarget.Tile));
                _currentTargetIndex++;
                CurrentState = ExecutionState.Preparing;
                return;
            }

            var replan = _navigator.FindPathToInteract(_actor, _currentRequest!.LocationId, _currentTarget.Tile);
            if (!replan.Success || replan.Steps.Count == 0)
            {
                _skipped.Add(new NativeActionEffect(Describe(_currentTarget), "skipped", "unreachable", Tile: _currentTarget.Tile));
                _currentTargetIndex++;
                CurrentState = ExecutionState.Preparing;
                return;
            }

            _currentPath = replan.Steps.ToList();
            _currentPathIndex = 0;
            return;
        }

        var diff = targetPixel - currentPixel;
        float dist = diff.Length();

        if (dist <= WalkPixelsPerTick)
        {
            _actor.PixelPosition = targetPixel;
            _currentPathIndex++;
        }
        else
        {
            diff.Normalize();
            _actor.Facing = targetStep.Facing;
            _actor.MovePixels(diff.X * WalkPixelsPerTick, diff.Y * WalkPixelsPerTick);
        }
    }

    private void HandleFacing()
    {
        if (_currentTarget.Tile != _actor.Tile)
            _actor.Face(DirectionToAdjacent(_actor.Tile, _currentTarget.Tile));

        _actionTicks = 0;
        _actionEffectExecuted = false;
        _lastStepResult = null;


        CurrentState = ExecutionState.Acting;
    }

    private void HandleActing(GameTime? time, long tickCount)
    {
        _actionTicks++;


        if (!_actionEffectExecuted && _actionTicks >= ActionDurationTicks / 2)
        {
            _actionEffectExecuted = true;
            _lastStepResult = _adapter.Execute(_actor, _currentRequest!, _currentTarget);
        }

        if (_actionTicks >= ActionDurationTicks)
        {
            CurrentState = ExecutionState.Verifying;
        }
    }

    private void HandleVerifying()
    {
        var result = _lastStepResult;
        string targetLabel = Describe(_currentTarget);

        if (result is null)
        {
            _failed.Add(new NativeActionEffect(targetLabel, "failed", "no-result", Tile: _currentTarget.Tile));
        }
        else if (result.Success)
        {
            _completed.Add(new NativeActionEffect(targetLabel, result.State, null, result.ItemId, result.ItemCount, _currentTarget.Tile));
            _staminaUsed += result.StaminaCost;
            _waterUsed += result.WaterUsed;
            EmitProgress("verifying", result.State);
        }
        else if (result.PreconditionFailed)
        {
            _skipped.Add(new NativeActionEffect(targetLabel, "skipped", result.SkipReason, result.ItemId, 0, _currentTarget.Tile));
            _playerActionRequired |= result.PlayerActionRequired;
            EmitProgress("waiting", result.SkipReason);
        }
        else
        {
            _failed.Add(new NativeActionEffect(targetLabel, "failed", result.ErrorMessage, result.ItemId, 0, _currentTarget.Tile));
            _playerActionRequired |= result.PlayerActionRequired;
            EmitProgress("failed", result.ErrorMessage);
        }

        if (_actor.IsExhausted)
        {
            _currentTargetIndex++;
            FinishExecution(_completed.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Failed,
                "Companion stamina exhausted.", "STAMINA_EXHAUSTED");
            return;
        }

        _currentTargetIndex++;

        if (CheckPauseOrCancel()) return;

        CurrentState = ExecutionState.Preparing;
    }

    public static bool IsSatisfiedSkipReason(string? reason)
    {
        if (string.IsNullOrEmpty(reason)) return false;
        return reason.StartsWith("already-", StringComparison.OrdinalIgnoreCase)
            || string.Equals(reason, "no-work", StringComparison.OrdinalIgnoreCase);
    }

    private ExecutionState SelectTerminalState(out string? errorMessage, out string? errorCode)
    {
        errorMessage = null;
        errorCode = null;

        if (_failed.Count > 0)
        {
            errorMessage = _failed[0].Reason;
            errorCode = "TARGET_FAILED";
            return _completed.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Failed;
        }

        var unfulfilledSkip = _skipped.FirstOrDefault(s => !IsSatisfiedSkipReason(s.Reason));
        if (unfulfilledSkip is not null)
        {
            errorCode = unfulfilledSkip.Reason ?? "PRECONDITION_FAILED";
            errorMessage = $"Precondition not satisfied: {unfulfilledSkip.Reason}.";
            return _completed.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Rejected;
        }

        return ExecutionState.Succeeded;
    }

    private bool CheckPauseOrCancel()
    {
        if (_cancelRequested)
        {
            CurrentState = ExecutionState.Cancelling;
            return true;
        }

        if (_pauseRequested)
        {
            CurrentState = ExecutionState.Paused;
            Log($"{_currentRequest?.SkillId} paused at safe boundary (task={ActiveTaskId}).");
            return true;
        }

        return false;
    }

    private NativeActionResult FinishExecution(ExecutionState terminalState, string? errorMessage, string? errorCode)
    {
        CurrentState = terminalState;
        _actor.SetActiveTask(null);
        _actor.Halt();

        int elapsedMinutes = IWorldObserver.CalculateGameMinutesElapsed(_startClock, _observer.TimeOfDay);

        EmitProgress(
            terminalState switch
            {
                ExecutionState.Succeeded => "completed",
                ExecutionState.PartiallySucceeded => "partial",
                ExecutionState.Rejected => "rejected",
                ExecutionState.Cancelled => "cancelled",
                _ => "failed"
            },
            errorCode);

        var result = new NativeActionResult(
            commandId: _currentRequest?.CommandId ?? "unknown",
            taskId: _currentRequest?.TaskId ?? "unknown",
            skillId: _currentRequest?.SkillId ?? "native-action",
            finalState: terminalState,
            effects: _completed.ToList(),
            skipped: _skipped.ToList(),
            failed: _failed.ToList(),
            staminaUsed: _staminaUsed,
            waterUsed: _waterUsed,
            gameMinutesElapsed: elapsedMinutes,
            finalWorldRevision: _observer.WorldRevision,
            errorMessage: errorMessage ?? (_failed.Count > 0 ? _failed[0].Reason : null),
            errorCode: errorCode ?? (_failed.Count > 0 ? "TARGET_FAILED" : null),
            retryRecommended: false,
            playerActionRequired: _playerActionRequired,
            progress: _lastProgress
        );

        FinalResult = result;
        Log($"{result.SkillId} finished: {terminalState} - {_completed.Count} completed, {_skipped.Count} skipped, {_failed.Count} failed.");

        OnCompleted?.Invoke(result);
        return result;
    }

    private static string Describe(NativeActionTarget target) =>
        string.IsNullOrEmpty(target.TargetId) ? target.Tile.ToString() : $"{target.TargetId}@{target.Tile}";

    public void StepTicks(int count)
    {
        for (int i = 0; i < count; i++)
        {
            Update(null, i);
            if (!IsExecuting) break;
        }
    }

    private static FacingDirection DirectionToAdjacent(TileCoordinate from, TileCoordinate to)
    {
        int dx = to.X - from.X;
        int dy = to.Y - from.Y;

        if (Math.Abs(dx) >= Math.Abs(dy))
            return dx > 0 ? FacingDirection.Right : FacingDirection.Left;
        return dy > 0 ? FacingDirection.Down : FacingDirection.Up;
    }
}
