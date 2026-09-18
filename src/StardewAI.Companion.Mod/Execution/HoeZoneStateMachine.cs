using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>
/// Tick-driven execution state machine for the "hoe-tiles" skill.
/// Tills designated clear ground into farm soil using genuine game mechanics
/// (Hoe.DoFunction via IHoeAdapter), with the standard execution lifecycle guarantees:
/// safe pause/resume/cancel boundaries, measured clock budget, monotonic timeout,
/// result verification, crop preservation, and independent stamina accounting.
/// </summary>
public sealed class HoeZoneStateMachine : ISkillExecutionMachine
{
    private const float WalkPixelsPerTick = 4f;
    private const int MaxReplansPerTarget = 3;
    private const long MaxMonotonicTicks = 3600;
    private const int ActionDurationTicks = 8; // Tool action duration

    private readonly IFarmerActor _actor;
    private readonly CompanionAvatar? _avatar;
    private readonly IWorldObserver _observer;
    private readonly SameMapNavigator _navigator;
    private readonly IHoeAdapter _adapter;
    private readonly Action<string, LogLevel>? _log;

    private readonly object _stateLock = new();

    private HoeZoneRequest? _currentRequest;
    private List<TileCoordinate> _pendingTargets = new();
    private int _currentTargetIndex;
    private TileCoordinate _currentTargetTile;
    private List<PathStep> _currentPath = new();
    private int _currentPathIndex;
    private int _replanCount;
    private int _actionTicks;
    private bool _actionEffectExecuted;
    private HoeTileResult? _lastHoeResult;

    private readonly List<HoedTileInfo> _hoedTiles = new();
    private readonly List<SkippedTileInfo> _skippedTiles = new();
    private readonly List<FailedTileInfo> _failedTiles = new();
    private float _staminaUsed;
    private int _waterUsed;
    private long _elapsedTicks;
    private int _startClock;

    private volatile bool _cancelRequested;
    private volatile string? _cancelReason;
    private volatile bool _pauseRequested;

    public ExecutionState CurrentState { get; private set; } = ExecutionState.Created;
    public bool IsExecuting => CurrentState is ExecutionState.Validating or ExecutionState.Preparing
        or ExecutionState.Navigating or ExecutionState.Facing or ExecutionState.Acting or ExecutionState.Verifying
        or ExecutionState.Paused or ExecutionState.Cancelling;
    public bool IsPaused => CurrentState == ExecutionState.Paused;
    public HoeZoneResult? FinalResult { get; private set; }

    public HoeZoneRequest? CurrentRequest => _currentRequest;
    public string? ActiveTaskId => _currentRequest?.TaskId;
    public int TotalTargets => _pendingTargets.Count;
    public int CurrentTargetIndex => _currentTargetIndex;
    public int HoedCount => _hoedTiles.Count;
    public int SkippedCount => _skippedTiles.Count;
    public int FailedCount => _failedTiles.Count;

    public event Action<HoeZoneResult>? OnCompleted;
    public event Action<string>? OnNotification;

    private void NotifyPlayer(string message) => OnNotification?.Invoke(message);

    public HoeZoneStateMachine(
        IFarmerActor actor,
        IWorldObserver observer,
        SameMapNavigator navigator,
        IHoeAdapter adapter,
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

    public bool Start(HoeZoneRequest request, out HoeZoneResult? earlyTerminalResult)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_stateLock)
        {
            _currentRequest = request;
            _hoedTiles.Clear();
            _skippedTiles.Clear();
            _failedTiles.Clear();
            _staminaUsed = 0f;
            _waterUsed = 0;
            _elapsedTicks = 0;
            _replanCount = 0;
            _actionTicks = 0;
            _actionEffectExecuted = false;
            _lastHoeResult = null;
            _startClock = _observer.TimeOfDay;
            _cancelRequested = false;
            _cancelReason = null;
            _pauseRequested = false;
            FinalResult = null;

            _actor.SetActiveTask(request.TaskId);

            CurrentState = ExecutionState.Validating;

            if (request.TargetTiles.Count == 0)
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected, "Target tiles list cannot be empty.", "INVALID_PARAMETERS");
                return false;
            }

            if (_observer.CurrentLocationName is not null &&
                !string.Equals(_observer.CurrentLocationName, request.LocationId, StringComparison.OrdinalIgnoreCase))
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    $"Location mismatch: Companion is on map '{_observer.CurrentLocationName}', but request specifies '{request.LocationId}'.",
                    "LOCATION_MISMATCH");
                return false;
            }

            if (request.MaxGameMinutes < 1)
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    $"Invalid maxGameMinutes: {request.MaxGameMinutes}. Must be at least 1.", "INVALID_PARAMETERS");
                return false;
            }



            if (_actor.Hoe is null)
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    "Companion does not possess a Hoe tool in inventory.",
                    "MISSING_TOOL");
                return false;
            }

            _pendingTargets = request.TargetTiles.Distinct().ToList();
            _currentTargetIndex = 0;

            CurrentState = ExecutionState.Preparing;
            Log($"hoe-tiles started: {_pendingTargets.Count} tile(s) on '{request.LocationId}' (commandId={request.CommandId}).");

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
                Log($"hoe-tiles pause requested (task={ActiveTaskId}).");
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
                Log($"hoe-tiles resumed (task={ActiveTaskId}).");
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
                Log($"hoe-tiles cancel requested: {reason} (task={ActiveTaskId}).");
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
                NotifyPlayer("AI Companion: Task paused.");
                return;
            }

            if (_cancelRequested && CurrentState is not (ExecutionState.Cancelling or ExecutionState.Cancelled))
            {
                CurrentState = ExecutionState.Cancelling;
            }

            _elapsedTicks++;

            if (_elapsedTicks > MaxMonotonicTicks)
            {
                Log($"hoe-tiles monotonic timeout exceeded ({MaxMonotonicTicks} ticks). Terminating.", LogLevel.Warn);
                FinishExecution(ExecutionState.Failed, "Monotonic tick limit exceeded.", "TIMEOUT");
                return;
            }

            int elapsedMinutes = IWorldObserver.CalculateGameMinutesElapsed(_startClock, _observer.TimeOfDay);
            if (_currentRequest is not null && elapsedMinutes > _currentRequest.MaxGameMinutes)
            {
                Log($"hoe-tiles budget exceeded: {elapsedMinutes} > {_currentRequest.MaxGameMinutes} minutes.", LogLevel.Warn);
                FinishExecution(ExecutionState.PartiallySucceeded, "Game-clock minute budget exceeded.", "BUDGET_EXCEEDED");
                return;
            }

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
    }

    private void HandlePreparing()
    {
        if (CheckPauseOrCancel()) return;

        if (_currentTargetIndex >= _pendingTargets.Count)
        {
            var terminal = _failedTiles.Count > 0
                ? (_hoedTiles.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Failed)
                : (_skippedTiles.Count > 0 && _hoedTiles.Count == 0 ? ExecutionState.Succeeded : ExecutionState.Succeeded);

            FinishExecution(terminal, null, null);
            return;
        }

        _currentTargetTile = _pendingTargets[_currentTargetIndex];

        if (_actor.Tile.IsAdjacentTo(_currentTargetTile))
        {
            CurrentState = ExecutionState.Facing;
            return;
        }

        var pathResult = _navigator.FindPathToInteract(_actor, _currentRequest!.LocationId, _currentTargetTile);
        if (!pathResult.Success || pathResult.Steps.Count == 0)
        {
            _skippedTiles.Add(new SkippedTileInfo(_currentTargetTile, "unreachable"));
            _currentTargetIndex++;
            CurrentState = ExecutionState.Preparing;
            return;
        }

        _currentPath = pathResult.Steps.ToList();
        _currentPathIndex = 0;
        _replanCount = 0;
        CurrentState = ExecutionState.Navigating;
    }

    private void HandleNavigating()
    {
        if (_currentPathIndex >= _currentPath.Count)
        {
            _actor.Halt();
            if (_actor.Tile.IsAdjacentTo(_currentTargetTile))
            {
                CurrentState = ExecutionState.Facing;
            }
            else
            {
                CurrentState = ExecutionState.Preparing;
            }
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
                _skippedTiles.Add(new SkippedTileInfo(_currentTargetTile, "player-obstruction"));
                _currentTargetIndex++;
                CurrentState = ExecutionState.Preparing;
                return;
            }

            var replan = _navigator.FindPathToInteract(_actor, _currentRequest!.LocationId, _currentTargetTile);
            if (!replan.Success || replan.Steps.Count == 0)
            {
                _skippedTiles.Add(new SkippedTileInfo(_currentTargetTile, "unreachable"));
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
        var dir = DirectionToAdjacent(_actor.Tile, _currentTargetTile);
        _actor.Face(dir);
        _actionTicks = 0;
        _actionEffectExecuted = false;
        _lastHoeResult = null;
        _actor.BeginUsingTool();
        CurrentState = ExecutionState.Acting;
    }

    private void HandleActing(GameTime? time, long tickCount)
    {
        _actionTicks++;
        _actor.UpdateToolAnimation(time, tickCount);

        if (!_actionEffectExecuted && _actionTicks >= ActionDurationTicks / 2)
        {
            _actionEffectExecuted = true;
            _lastHoeResult = _adapter.HoeTile(_actor, _currentRequest!.LocationId, _currentTargetTile);
        }

        if (_actionTicks >= ActionDurationTicks)
        {
            _actor.EndUsingTool();
            CurrentState = ExecutionState.Verifying;
        }
    }

    private void HandleVerifying()
    {
        if (_lastHoeResult is null || !_lastHoeResult.Success)
        {
            if (_lastHoeResult is not null && _lastHoeResult.PreconditionFailed && _lastHoeResult.SkipReason is not null)
            {
                _skippedTiles.Add(new SkippedTileInfo(_currentTargetTile, _lastHoeResult.SkipReason));

                if (_lastHoeResult.SkipReason == "stamina-exhausted")
                {
                    FinishExecution(ExecutionState.PartiallySucceeded, "Stamina exhausted.", "STAMINA_EXHAUSTED");
                    return;
                }
            }
            else
            {
                _failedTiles.Add(new FailedTileInfo(_currentTargetTile,
                    _lastHoeResult?.ErrorMessage ?? "Hoe operation failed."));
            }
        }
        else
        {
            _hoedTiles.Add(new HoedTileInfo(_currentTargetTile, _lastHoeResult.StaminaCost));
            _staminaUsed += _lastHoeResult.StaminaCost;
        }

        _currentTargetIndex++;

        if (CheckPauseOrCancel()) return;

        CurrentState = ExecutionState.Preparing;
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
            Log($"hoe-tiles paused at safe boundary (task={ActiveTaskId}).");
            return true;
        }

        return false;
    }

    private HoeZoneResult FinishExecution(ExecutionState terminalState, string? errorMessage, string? errorCode)
    {
        CurrentState = terminalState;
        _actor.SetActiveTask(null);
        _actor.Halt();

        int elapsedMinutes = IWorldObserver.CalculateGameMinutesElapsed(_startClock, _observer.TimeOfDay);

        var result = new HoeZoneResult(
            commandId: _currentRequest?.CommandId ?? "unknown",
            taskId: _currentRequest?.TaskId ?? "unknown",
            finalState: terminalState,
            hoedTiles: _hoedTiles.ToList(),
            skippedTiles: _skippedTiles.ToList(),
            failedTiles: _failedTiles.ToList(),
            staminaUsed: _staminaUsed,
            waterUsed: _waterUsed,
            gameMinutesElapsed: elapsedMinutes,
            finalWorldRevision: _observer.WorldRevision,
            errorMessage: errorMessage,
            errorCode: errorCode
        );

        FinalResult = result;
        Log($"hoe-tiles finished: {terminalState} - {_hoedTiles.Count} hoed, {_skippedTiles.Count} skipped, {_failedTiles.Count} failed.");

        OnCompleted?.Invoke(result);
        return result;
    }

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
        {
            return dx > 0 ? FacingDirection.Right : FacingDirection.Left;
        }
        return dy > 0 ? FacingDirection.Down : FacingDirection.Up;
    }
}
