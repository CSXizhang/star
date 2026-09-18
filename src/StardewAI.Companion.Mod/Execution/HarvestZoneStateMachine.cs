using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>
/// Tick-driven execution state machine for the "harvest-zone" skill.
/// Hand-harvests mature crops into the companion inventory through genuine game
/// mechanics (crop.harvest via IHarvestAdapter), with the same lifecycle guarantees
/// as the water-zone machine: safe pause/resume/cancel boundaries, measured clock
/// budget, monotonic timeout, result verification, and conservative inventory
/// capacity handling (skip "inventory-full", early stop with details.inventoryFull).
/// </summary>
public sealed class HarvestZoneStateMachine : ISkillExecutionMachine
{
    private const float WalkPixelsPerTick = 4f;
    private const int MaxReplansPerTarget = 3;
    private const long MaxMonotonicTicks = 3600; // 60 seconds at 60 fps safety timeout
    private const int ActionDurationTicks = 4; // brief pause per harvest action (no fabricated tool animation)

    private readonly IFarmerActor _actor;
    private readonly CompanionAvatar? _avatar;
    private readonly IWorldObserver _observer;
    private readonly SameMapNavigator _navigator;
    private readonly IHarvestAdapter _adapter;
    private readonly Action<string, LogLevel>? _log;

    private readonly object _stateLock = new();

    // Active execution context
    private HarvestZoneRequest? _currentRequest;
    private List<TileCoordinate> _pendingTargets = new();
    private int _currentTargetIndex;
    private TileCoordinate _currentTargetTile;
    private List<PathStep> _currentPath = new();
    private int _currentPathIndex;
    private int _replanCount;
    private int _actionTicks;
    private bool _actionEffectExecuted;
    private HarvestTileResult? _lastHarvestResult;

    // Output tracking
    private readonly List<HarvestedTileInfo> _harvestedTiles = new();
    private readonly List<SkippedTileInfo> _skippedTiles = new();
    private readonly List<FailedTileInfo> _failedTiles = new();
    private float _staminaUsed;
    private int _waterUsed;
    private long _elapsedTicks;
    private int _startClock;

    // Signals
    private volatile bool _cancelRequested;
    private volatile string? _cancelReason;
    private volatile bool _pauseRequested;

    public ExecutionState CurrentState { get; private set; } = ExecutionState.Created;
    public bool IsExecuting => CurrentState is ExecutionState.Validating or ExecutionState.Preparing
        or ExecutionState.Navigating or ExecutionState.Facing or ExecutionState.Acting or ExecutionState.Verifying
        or ExecutionState.Paused or ExecutionState.Cancelling;
    public bool IsPaused => CurrentState == ExecutionState.Paused;
    public HarvestZoneResult? FinalResult { get; private set; }

    public HarvestZoneRequest? CurrentRequest => _currentRequest;
    public string? ActiveTaskId => _currentRequest?.TaskId;
    public int TotalTargets => _pendingTargets.Count;
    public int CurrentTargetIndex => _currentTargetIndex;
    public int HarvestedCount => _harvestedTiles.Count;
    public int SkippedCount => _skippedTiles.Count;
    public int FailedCount => _failedTiles.Count;

    public event Action<HarvestZoneResult>? OnCompleted;
    public event Action<string>? OnNotification;

    private void NotifyPlayer(string message) => OnNotification?.Invoke(message);

    public HarvestZoneStateMachine(
        IFarmerActor actor,
        IWorldObserver observer,
        SameMapNavigator navigator,
        IHarvestAdapter adapter,
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

    /// <summary>
    /// Starts execution of a HarvestZoneRequest.
    /// </summary>
    public bool Start(HarvestZoneRequest request, out HarvestZoneResult? earlyTerminalResult)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_stateLock)
        {
            _currentRequest = request;
            _harvestedTiles.Clear();
            _skippedTiles.Clear();
            _failedTiles.Clear();
            _staminaUsed = 0f;
            _waterUsed = 0;
            _elapsedTicks = 0;
            _replanCount = 0;
            _actionTicks = 0;
            _actionEffectExecuted = false;
            _lastHarvestResult = null;
            _startClock = _observer.TimeOfDay;
            _cancelRequested = false;
            _cancelReason = null;
            _pauseRequested = false;
            FinalResult = null;

            _actor.SetActiveTask(request.TaskId);

            // Phase 1: Validating
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
                    "MaxGameMinutes budget must be at least 1.",
                    "INVALID_PARAMETERS");
                return false;
            }

            // Phase 2: Preparing & Target Filtering
            CurrentState = ExecutionState.Preparing;
            _pendingTargets.Clear();

            foreach (var tile in request.TargetTiles)
            {
                var dirt = _observer.GetDirtState(request.LocationId, tile);
                if (!dirt.IsTilled || !dirt.HasCrop || !dirt.IsHarvestable)
                {
                    _skippedTiles.Add(new SkippedTileInfo(tile, "not-ready"));
                }
                else if (dirt.RequiresScythe)
                {
                    _skippedTiles.Add(new SkippedTileInfo(tile, "scythe-required"));
                }
                else
                {
                    _pendingTargets.Add(tile);
                }
            }

            if (_pendingTargets.Count == 0)
            {
                CurrentState = ExecutionState.Succeeded;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Succeeded);
                return true;
            }

            // Conservative capacity precondition (mirrors water's CAN_EMPTY rejection):
            // harvesting is impossible without at least one empty inventory slot.
            if (_actor.FreeInventorySlots <= 0)
            {
                foreach (var tile in _pendingTargets)
                {
                    _skippedTiles.Add(new SkippedTileInfo(tile, "inventory-full"));
                }
                _pendingTargets.Clear();
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    "Companion inventory has no empty slot. Precondition failed.", "INVENTORY_FULL",
                    retryRecommended: true, inventoryFull: true);
                return false;
            }

            // Order targets by Manhattan distance from actor's current tile
            _pendingTargets.Sort((a, b) => _actor.Tile.ManhattanDistanceTo(a).CompareTo(_actor.Tile.ManhattanDistanceTo(b)));

            _currentTargetIndex = 0;
            _currentTargetTile = _pendingTargets[0];
            PlanPathToCurrentTarget();

            earlyTerminalResult = null;
            NotifyPlayer($"AI Companion: Harvest task started ({_pendingTargets.Count} tiles).");
            return true;
        }
    }

    /// <summary>
    /// Advances state machine execution by one tick.
    /// Called every game update tick from SMAPI UpdateTicked.
    /// </summary>
    public void Update(GameTime? time, long tickCount)
    {
        lock (_stateLock)
        {
            if (!IsExecuting || _currentRequest is null)
                return;

            // 1. High-priority cancellation: MUST service cancellation even if paused or menu open
            if (_cancelRequested)
            {
                _cancelRequested = false;

                // Safe in-progress action resolution:
                // If the harvest effect already executed on the current tile, verify it before halting
                if (CurrentState == ExecutionState.Acting && _actionEffectExecuted &&
                    !_harvestedTiles.Any(h => h.Tile == _currentTargetTile))
                {
                    var dirt = _observer.GetDirtState(_currentRequest.LocationId, _currentTargetTile);
                    if (!dirt.IsHarvestable && _lastHarvestResult is not null)
                    {
                        _harvestedTiles.Add(new HarvestedTileInfo(
                            _currentTargetTile,
                            _lastHarvestResult.CropId,
                            _lastHarvestResult.ItemId,
                            _lastHarvestResult.ItemName,
                            _lastHarvestResult.Stack,
                            _lastHarvestResult.Quality));
                    }
                }

                _actor.Halt();
                _avatar?.SetAnimation("idle");
                var terminalState = _harvestedTiles.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Cancelled;
                FinishExecution(terminalState, _cancelReason ?? "Execution cancelled by request.");
                return;
            }

            // 2. High-priority pause at safe boundary (Navigating or Facing)
            if (_pauseRequested && CurrentState is ExecutionState.Navigating or ExecutionState.Facing)
            {
                _pauseRequested = false;
                CurrentState = ExecutionState.Paused;
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                NotifyPlayer("AI Companion: Task paused.");
                return;
            }

            // 3. Game-level pause, open menus, saving, world not ready
            try
            {
                if (Game1.paused || Game1.activeClickableMenu != null || !Context.IsWorldReady)
                {
                    _actor.Halt();
                    return;
                }
            }
            catch
            {
                // Headless test or pre-load safe guard
            }

            if (IsPaused)
                return;

            _elapsedTicks++;

            // 4. Actual game clock delta measurement & budget check
            int gameMinutesElapsed = IWorldObserver.CalculateGameMinutesElapsed(_startClock, _observer.TimeOfDay);
            if (gameMinutesElapsed > _currentRequest.MaxGameMinutes)
            {
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                var terminalState = _harvestedTiles.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Failed;
                FinishExecution(terminalState, $"MaxGameMinutes budget ({_currentRequest.MaxGameMinutes} min) exceeded.", "BUDGET_EXHAUSTED");
                return;
            }

            // 5. Monotonic timeout guard
            if (_elapsedTicks > MaxMonotonicTicks)
            {
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                var terminalState = _harvestedTiles.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Failed;
                FinishExecution(terminalState, "Execution exceeded monotonic tick timeout limit.", "TIMEOUT");
                return;
            }

            // State Dispatch
            try
            {
                switch (CurrentState)
                {
                    case ExecutionState.Navigating:
                        TickNavigating();
                        break;

                    case ExecutionState.Facing:
                        TickFacing();
                        break;

                    case ExecutionState.Acting:
                        TickActing();
                        break;

                    case ExecutionState.Verifying:
                        TickVerifying();
                        break;
                }
            }
            catch (Exception ex)
            {
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                Log($"Exception during execution (state={CurrentState}, tile=({_currentTargetTile.X}, {_currentTargetTile.Y})): {ex}", LogLevel.Error);
                _failedTiles.Add(new FailedTileInfo(_currentTargetTile, $"Exception during execution: {ex.Message}"));
                FinishExecution(ExecutionState.Failed, $"Execution failed due to exception: {ex.Message}", "EXECUTION_EXCEPTION");
            }
        }
    }

    private void PlanPathToCurrentTarget()
    {
        if (_currentRequest is null) return;

        var pathResult = _navigator.FindPathToInteract(_actor, _currentRequest.LocationId, _currentTargetTile);
        if (!pathResult.Success)
        {
            Log($"Navigation to tile ({_currentTargetTile.X}, {_currentTargetTile.Y}) failed: {pathResult.ErrorMessage ?? "Navigation failed"}", LogLevel.Warn);
            _failedTiles.Add(new FailedTileInfo(_currentTargetTile, pathResult.ErrorMessage ?? "Navigation failed"));
            AdvanceToNextTarget();
            return;
        }

        _currentPath = pathResult.Steps.ToList();
        _currentPathIndex = 0;
        CurrentState = ExecutionState.Navigating;
    }

    private void TickNavigating()
    {
        if (_currentPathIndex >= _currentPath.Count)
        {
            // Destination reached
            _actor.Halt();
            CurrentState = ExecutionState.Facing;
            return;
        }

        var step = _currentPath[_currentPathIndex];

        // Dynamic collision check BEFORE stepping towards destination tile:
        // Refuse entry if blocked by map terrain, dynamic obstacle, or human player!
        bool isPassable = step.Tile == _actor.Tile ||
                          (_observer.IsTilePassable(_currentRequest!.LocationId, step.Tile) &&
                           !_observer.IsPlayerOnTile(_currentRequest!.LocationId, step.Tile));

        if (!isPassable)
        {
            _actor.Halt();
            _replanCount++;
            if (_replanCount > MaxReplansPerTarget)
            {
                _failedTiles.Add(new FailedTileInfo(_currentTargetTile, "Dynamic obstacle blocked path and maximum replans exceeded."));
                AdvanceToNextTarget();
                return;
            }

            // Replan from current tile
            var replanResult = _navigator.FindPathToInteract(_actor, _currentRequest!.LocationId, _currentTargetTile);
            if (!replanResult.Success)
            {
                _failedTiles.Add(new FailedTileInfo(_currentTargetTile, replanResult.ErrorMessage ?? "Navigation replan failed"));
                AdvanceToNextTarget();
                return;
            }

            _currentPath = replanResult.Steps.ToList();
            _currentPathIndex = 0;
            return;
        }

        var targetPixelX = step.Tile.X * 64f + 32f;
        var targetPixelY = step.Tile.Y * 64f + 32f;

        var currentPos = _actor.PixelPosition;
        float dx = targetPixelX - (currentPos.X + 32f);
        float dy = targetPixelY - (currentPos.Y + 32f);

        float dist = MathF.Sqrt(dx * dx + dy * dy);
        if (dist <= WalkPixelsPerTick)
        {
            // Arrived at step tile
            _actor.PixelPosition = new Vector2(step.Tile.X * 64, step.Tile.Y * 64);
            _actor.Face(step.Facing);
            _currentPathIndex++;
            _replanCount = 0;
        }
        else
        {
            // Continuous physical movement step
            float moveX = (dx / dist) * WalkPixelsPerTick;
            float moveY = (dy / dist) * WalkPixelsPerTick;
            _actor.MovePixels(moveX, moveY);
        }
    }

    private void TickFacing()
    {
        // 1. Safe boundary: Cancellation before the action
        if (_cancelRequested)
        {
            _cancelRequested = false;
            _actor.Halt();
            _avatar?.SetAnimation("idle");
            var terminalState = _harvestedTiles.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Cancelled;
            FinishExecution(terminalState, _cancelReason ?? "Execution cancelled by request.");
            return;
        }

        // 2. Safe boundary: Pause before the action
        if (_pauseRequested)
        {
            _pauseRequested = false;
            CurrentState = ExecutionState.Paused;
            _actor.Halt();
            _avatar?.SetAnimation("idle");
            return;
        }

        var requiredFacing = FacingDirectionExtensions.DirectionToAdjacent(_actor.Tile, _currentTargetTile);
        if (requiredFacing.HasValue)
        {
            _actor.Face(requiredFacing.Value);
        }

        // Pre-action re-observation: check if the crop was harvested concurrently or changed
        var dirt = _observer.GetDirtState(_currentRequest!.LocationId, _currentTargetTile);
        if (!dirt.IsTilled || !dirt.HasCrop || !dirt.IsHarvestable)
        {
            _skippedTiles.Add(new SkippedTileInfo(_currentTargetTile, "not-ready"));
            AdvanceToNextTarget();
            return;
        }
        if (dirt.RequiresScythe)
        {
            _skippedTiles.Add(new SkippedTileInfo(_currentTargetTile, "scythe-required"));
            AdvanceToNextTarget();
            return;
        }

        // Conservative capacity check before the action
        if (_actor.FreeInventorySlots <= 0)
        {
            StopForInventoryFull();
            return;
        }

        // Initiate the brief harvest action (no fabricated tool animation)
        CurrentState = ExecutionState.Acting;
        _actionTicks = 0;
        _actionEffectExecuted = false;
        _lastHarvestResult = null;
        _avatar?.SetAnimation("action");
        Log($"Beginning harvest action on tile ({_currentTargetTile.X}, {_currentTargetTile.Y}).");
    }

    private void TickActing()
    {
        _actionTicks++;

        if (!_actionEffectExecuted)
        {
            _actionEffectExecuted = true;
            var harvestResult = _adapter.HarvestTile(_actor, _currentRequest!.LocationId, _currentTargetTile);

            if (harvestResult.PreconditionFailed && harvestResult.SkipReason == "inventory-full")
            {
                StopForInventoryFull();
                return;
            }

            if (harvestResult.PreconditionFailed && harvestResult.SkipReason is not null)
            {
                Log($"Harvest skipped on tile ({_currentTargetTile.X}, {_currentTargetTile.Y}): {harvestResult.SkipReason}.");
                _skippedTiles.Add(new SkippedTileInfo(_currentTargetTile, harvestResult.SkipReason));
                _avatar?.SetAnimation("idle");
                AdvanceToNextTarget();
                return;
            }

            if (!harvestResult.Success)
            {
                Log($"Harvest effect failed on tile ({_currentTargetTile.X}, {_currentTargetTile.Y}): {harvestResult.ErrorMessage ?? "Harvest execution failed"}", LogLevel.Warn);
                _failedTiles.Add(new FailedTileInfo(_currentTargetTile, harvestResult.ErrorMessage ?? "Harvest execution failed"));
                _avatar?.SetAnimation("idle");
                AdvanceToNextTarget();
                return;
            }

            _lastHarvestResult = harvestResult;
            _staminaUsed += harvestResult.StaminaCost;
            _waterUsed += harvestResult.WaterCost;
            Log($"Harvest effect applied on tile ({_currentTargetTile.X}, {_currentTargetTile.Y}): item={harvestResult.ItemId} x{harvestResult.Stack}.");
        }

        if (_actionTicks >= ActionDurationTicks)
        {
            _avatar?.SetAnimation("idle");
            CurrentState = ExecutionState.Verifying;
        }
    }

    private void TickVerifying()
    {
        if (!_actionEffectExecuted || _lastHarvestResult is null)
        {
            _failedTiles.Add(new FailedTileInfo(_currentTargetTile, "Result verification failed: Harvest effect was never executed."));
            AdvanceToNextTarget();
            return;
        }

        var dirt = _observer.GetDirtState(_currentRequest!.LocationId, _currentTargetTile);
        if (!dirt.IsHarvestable)
        {
            _harvestedTiles.Add(new HarvestedTileInfo(
                _currentTargetTile,
                _lastHarvestResult.CropId,
                _lastHarvestResult.ItemId,
                _lastHarvestResult.ItemName,
                _lastHarvestResult.Stack,
                _lastHarvestResult.Quality));
            Log($"Verified tile ({_currentTargetTile.X}, {_currentTargetTile.Y}) is harvested.");
        }
        else
        {
            Log($"Verification failed: tile ({_currentTargetTile.X}, {_currentTargetTile.Y}) remained harvestable after harvest effect.", LogLevel.Warn);
            _failedTiles.Add(new FailedTileInfo(_currentTargetTile, "Result verification failed: Crop remained harvestable."));
        }

        AdvanceToNextTarget();
    }

    /// <summary>
    /// Early stop when the companion inventory has no empty slot: the current target
    /// (when not yet processed) and all remaining targets are skipped "inventory-full"
    /// and the result carries details.inventoryFull=true.
    /// </summary>
    private void StopForInventoryFull()
    {
        // _currentTargetIndex always points at the first unprocessed target here:
        // - from TickFacing/TickActing it still addresses the in-progress target;
        // - from AdvanceToNextTarget it was already incremented past the processed one.
        for (int i = _currentTargetIndex; i < _pendingTargets.Count; i++)
        {
            var tile = _pendingTargets[i];
            if (_harvestedTiles.Any(h => h.Tile == tile)) continue;
            if (_failedTiles.Any(f => f.Tile == tile)) continue;
            if (_skippedTiles.Any(s => s.Tile == tile)) continue;
            _skippedTiles.Add(new SkippedTileInfo(tile, "inventory-full"));
        }

        _actor.Halt();
        _avatar?.SetAnimation("idle");
        var terminalState = _harvestedTiles.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Failed;
        FinishExecution(terminalState,
            "Companion inventory is full; harvest stopped early.", "INVENTORY_FULL",
            retryRecommended: true, inventoryFull: true);
    }

    private void AdvanceToNextTarget()
    {
        // Explicit cursor increment: marks current target completed so pause/resume NEVER repeats it
        _currentTargetIndex++;

        if (_currentTargetIndex >= _pendingTargets.Count)
        {
            // All targets processed
            ExecutionState finalState;
            if (_failedTiles.Count == 0 && _harvestedTiles.Count == _pendingTargets.Count)
                finalState = ExecutionState.Succeeded;
            else if (_harvestedTiles.Count > 0)
                finalState = ExecutionState.PartiallySucceeded;
            else
                finalState = ExecutionState.Failed;

            FinishExecution(finalState);
            return;
        }

        // Safe boundary: Pause between targets
        if (_pauseRequested)
        {
            _pauseRequested = false;
            CurrentState = ExecutionState.Paused;
            _actor.Halt();
            _avatar?.SetAnimation("idle");
            return;
        }

        // Safe boundary: Cancellation between targets
        if (_cancelRequested)
        {
            _cancelRequested = false;
            _actor.Halt();
            _avatar?.SetAnimation("idle");
            var terminalState = _harvestedTiles.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Cancelled;
            FinishExecution(terminalState, _cancelReason ?? "Execution cancelled by request.");
            return;
        }

        // Conservative capacity check before commencing next target
        if (_actor.FreeInventorySlots <= 0)
        {
            StopForInventoryFull();
            return;
        }

        _currentTargetTile = _pendingTargets[_currentTargetIndex];
        _replanCount = 0;
        PlanPathToCurrentTarget();
    }

    private HarvestZoneResult FinishExecution(
        ExecutionState state,
        string? errorMessage = null,
        string? errorCode = null,
        bool retryRecommended = false,
        bool inventoryFull = false)
    {
        CurrentState = state;
        _actor.Halt();
        _actor.SetActiveTask(null);

        // Never emit a failed terminal result without a reason: surface per-tile failure causes.
        if (state == ExecutionState.Failed && errorMessage is null && _failedTiles.Count > 0)
        {
            var first = _failedTiles[0];
            errorMessage = _failedTiles.Count == 1
                ? $"Tile ({first.Tile.X}, {first.Tile.Y}) failed: {first.Reason}"
                : $"{_failedTiles.Count} tiles failed; first: tile ({first.Tile.X}, {first.Tile.Y}): {first.Reason}";
            errorCode ??= "EXECUTION_FAILED";
        }

        int gameMinutesElapsed = IWorldObserver.CalculateGameMinutesElapsed(_startClock, _observer.TimeOfDay);

        var result = new HarvestZoneResult(
            commandId: _currentRequest?.CommandId ?? "cmd-unknown",
            taskId: _currentRequest?.TaskId ?? "task-unknown",
            finalState: state,
            harvestedTiles: _harvestedTiles.ToList(),
            skippedTiles: _skippedTiles.ToList(),
            failedTiles: _failedTiles.ToList(),
            staminaUsed: _staminaUsed,
            waterUsed: _waterUsed,
            gameMinutesElapsed: gameMinutesElapsed,
            finalWorldRevision: _observer.WorldRevision,
            inventoryFull: inventoryFull,
            errorMessage: errorMessage,
            errorCode: errorCode,
            retryRecommended: retryRecommended
        );

        var level = state is ExecutionState.Failed or ExecutionState.Rejected ? LogLevel.Warn : LogLevel.Info;
        Log($"Harvest-zone finished: {state}; harvested={_harvestedTiles.Count}, skipped={_skippedTiles.Count}, failed={_failedTiles.Count}" +
            (errorMessage is not null ? $"; error={errorMessage}" : "."), level);
        foreach (var failed in _failedTiles)
        {
            Log($"  failed tile ({failed.Tile.X}, {failed.Tile.Y}): {failed.Reason}", LogLevel.Warn);
        }

        FinalResult = result;
        OnCompleted?.Invoke(result);

        if (state == ExecutionState.Succeeded)
        {
            NotifyPlayer($"AI Companion: Harvest task completed ({_harvestedTiles.Count} tiles).");
        }
        else if (state == ExecutionState.Cancelled)
        {
            NotifyPlayer($"AI Companion: Task cancelled ({_harvestedTiles.Count}/{_pendingTargets.Count} harvested).");
        }
        else if (state == ExecutionState.PartiallySucceeded)
        {
            if (inventoryFull)
            {
                NotifyPlayer($"AI Companion: Inventory full ({_harvestedTiles.Count}/{_pendingTargets.Count} harvested).");
            }
            else if (errorMessage?.Contains("cancel", StringComparison.OrdinalIgnoreCase) == true)
            {
                NotifyPlayer($"AI Companion: Task cancelled ({_harvestedTiles.Count}/{_pendingTargets.Count} harvested).");
            }
            else
            {
                NotifyPlayer($"AI Companion: Task partially completed ({_harvestedTiles.Count}/{_pendingTargets.Count} harvested).");
            }
        }
        else if (state == ExecutionState.Failed)
        {
            NotifyPlayer($"AI Companion: Task failed: {errorMessage ?? errorCode ?? "Execution failed"}.");
        }
        else if (state == ExecutionState.Rejected)
        {
            NotifyPlayer($"AI Companion: Task rejected: {errorMessage ?? errorCode ?? "Precondition failed"}.");
        }

        return result;
    }

    public void RequestCancel(string reason)
    {
        _cancelRequested = true;
        _cancelReason = reason;
    }

    public void RequestPause()
    {
        _pauseRequested = true;
    }

    public void Resume()
    {
        lock (_stateLock)
        {
            if (IsPaused && _currentRequest != null)
            {
                CurrentState = ExecutionState.Navigating;
                _currentTargetTile = _pendingTargets[_currentTargetIndex];
                _replanCount = 0;
                // Re-observe world and re-plan from current position to next pending target
                PlanPathToCurrentTarget();
                NotifyPlayer("AI Companion: Task resumed.");
            }
        }
    }

    /// <summary>
    /// Helper for tests or headless driver to step the state machine forward deterministically.
    /// </summary>
    public void StepTicks(int count)
    {
        for (int i = 0; i < count && IsExecuting; i++)
        {
            Update(null, i + 1);
        }
    }
}
