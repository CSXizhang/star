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
/// Tick-driven execution state machine for the "water-zone" skill.
/// Advances physical actor step-by-step, respects game pause/menus,
/// enforces collision re-checking BEFORE moving, tool animation lifecycle,
/// result verification, measured clock delta, monotonic timeout, and safe pause/resume/cancel boundaries.
/// </summary>
public sealed class WaterZoneStateMachine : ISkillExecutionMachine
{
    private const float WalkPixelsPerTick = 4f;
    private const int MaxReplansPerTarget = 3;
    private const long MaxMonotonicTicks = 3600; // 60 seconds at 60 fps safety timeout
    // Bounded search: only this many nearest remaining targets are costed with a
    // real path query per reorder. The rest keep a sentinel cost and stay ordered
    // by connectivity then Manhattan distance. This avoids a global TSP search.
    private const int MaxOrderingCandidates = 12;

    private readonly IFarmerActor _actor;
    private readonly CompanionAvatar? _avatar;
    private readonly IWorldObserver _observer;
    private readonly SameMapNavigator _navigator;
    private readonly IWateringCanAdapter _adapter;
    private readonly Action<string, LogLevel>? _log;

    private readonly object _stateLock = new();

    // Active execution context
    private WaterZoneRequest? _currentRequest;
    private List<TileCoordinate> _pendingTargets = new();
    private int _currentTargetIndex;
    private TileCoordinate _currentTargetTile;
    private List<PathStep> _currentPath = new();
    private int _currentPathIndex;
    private int _replanCount;
    private bool _toolEffectExecuted;
    // Tiles reported blocked by a dynamic obstacle during this target's replans.
    // Replanning must not route through the same blocked tile again.
    private readonly HashSet<TileCoordinate> _avoidTiles = new();

    // Output tracking
    private readonly List<TileCoordinate> _wateredTiles = new();
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
        or ExecutionState.Navigating or ExecutionState.Facing or ExecutionState.Watering or ExecutionState.Verifying
        or ExecutionState.Paused or ExecutionState.Cancelling;
    public bool IsPaused => CurrentState == ExecutionState.Paused;
    public WaterZoneResult? FinalResult { get; private set; }

    public WaterZoneRequest? CurrentRequest => _currentRequest;
    public string? ActiveTaskId => _currentRequest?.TaskId;
    public int TotalTargets => _pendingTargets.Count;
    public int CurrentTargetIndex => _currentTargetIndex;
    public int WateredCount => _wateredTiles.Count;
    public int SkippedCount => _skippedTiles.Count;
    public int FailedCount => _failedTiles.Count;

    public event Action<WaterZoneResult>? OnCompleted;
    public event Action<string>? OnNotification;

    private void NotifyPlayer(string message) => OnNotification?.Invoke(message);

    public WaterZoneStateMachine(
        IFarmerActor actor,
        IWorldObserver observer,
        SameMapNavigator navigator,
        IWateringCanAdapter adapter,
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
    /// Starts execution of a WaterZoneRequest.
    /// </summary>
    public bool Start(WaterZoneRequest request, out WaterZoneResult? earlyTerminalResult)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_stateLock)
        {
            _currentRequest = request;
            _wateredTiles.Clear();
            _skippedTiles.Clear();
            _failedTiles.Clear();
            _staminaUsed = 0f;
            _waterUsed = 0;
            _elapsedTicks = 0;
            _replanCount = 0;
            _toolEffectExecuted = false;
            _avoidTiles.Clear();
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

            if (_actor.IsWateringCanEmpty || _actor.WaterLeft < NormalWateringCanAdapter.BaseWaterCost)
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    "Watering can is empty. Precondition failed.", "CAN_EMPTY", retryRecommended: true);
                return false;
            }

            if (_actor.Stamina < NormalWateringCanAdapter.BaseStaminaCost)
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    "Companion stamina exhausted. Precondition failed.", "INSUFFICIENT_STAMINA", retryRecommended: true);
                return false;
            }

            // Enforce first-action budget constraints before starting
            if (request.MaxStamina < NormalWateringCanAdapter.BaseStaminaCost)
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    $"MaxStamina budget ({request.MaxStamina:F1}) is insufficient for even one watering action ({NormalWateringCanAdapter.BaseStaminaCost:F1}).",
                    "BUDGET_EXHAUSTED");
                return false;
            }

            if (request.MaxWater < NormalWateringCanAdapter.BaseWaterCost)
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    $"MaxWater budget ({request.MaxWater}) is insufficient for even one watering action ({NormalWateringCanAdapter.BaseWaterCost}).",
                    "BUDGET_EXHAUSTED");
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
                if (!dirt.IsTilled)
                {
                    _skippedTiles.Add(new SkippedTileInfo(tile, "NotTilled"));
                }
                else if (dirt.IsWatered)
                {
                    _skippedTiles.Add(new SkippedTileInfo(tile, "AlreadyWatered"));
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

            // Pick the first target from the companion's current position using
            // feasible path length, with preference for adjacent/connected tiles.
            ReorderRemainingTargets();

            _currentTargetIndex = 0;
            _currentTargetTile = _pendingTargets[0];
            PlanPathToCurrentTarget();

            earlyTerminalResult = null;
            NotifyPlayer($"AI Companion: Watering task started ({_pendingTargets.Count} tiles).");
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

                // Safe in-progress swing resolution:
                // If the tool effect already executed on the current tile, verify it before halting
                if (CurrentState == ExecutionState.Watering && _toolEffectExecuted && !_wateredTiles.Contains(_currentTargetTile))
                {
                    var dirt = _observer.GetDirtState(_currentRequest.LocationId, _currentTargetTile);
                    if (dirt.IsWatered)
                    {
                        _wateredTiles.Add(_currentTargetTile);
                    }
                }

                _actor.Halt();
                _actor.EndUsingTool();
                _avatar?.SetAnimation("idle");
                var terminalState = _wateredTiles.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Cancelled;
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
                _actor.EndUsingTool();
                _avatar?.SetAnimation("idle");
                var terminalState = _wateredTiles.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Failed;
                FinishExecution(terminalState, $"MaxGameMinutes budget ({_currentRequest.MaxGameMinutes} min) exceeded.", "BUDGET_EXHAUSTED");
                return;
            }

            // 5. Monotonic timeout guard
            if (_elapsedTicks > MaxMonotonicTicks)
            {
                _actor.Halt();
                _actor.EndUsingTool();
                _avatar?.SetAnimation("idle");
                var terminalState = _wateredTiles.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Failed;
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

                    case ExecutionState.Watering:
                        TickWatering(time, tickCount);
                        break;

                    case ExecutionState.Verifying:
                        TickVerifying();
                        break;
                }
            }
            catch (Exception ex)
            {
                _actor.Halt();
                _actor.EndUsingTool();
                _avatar?.SetAnimation("idle");
                Log($"Exception during execution (state={CurrentState}, tile=({_currentTargetTile.X}, {_currentTargetTile.Y})): {ex}", LogLevel.Error);
                _failedTiles.Add(new FailedTileInfo(_currentTargetTile, $"Exception during execution: {ex.Message}"));
                FinishExecution(ExecutionState.Failed, $"Execution failed due to exception: {ex.Message}", "EXECUTION_EXCEPTION");
            }
        }

    }

    /// <summary>
    /// Re-orders the not-yet-processed tail of targets from the companion's *current*
    /// tile. Cost is the length of a real feasible interaction path (not raw Manhattan
    /// distance), so walking order follows the actual obstacle layout. Ties prefer
    /// targets with more remaining neighbours (contiguous groups) and then the closest
    /// Manhattan distance. Only the nearest <see cref="MaxOrderingCandidates"/> are
    /// costed with a path query; the rest stay ordered by connectivity/distance.
    /// </summary>
    private void ReorderRemainingTargets()
    {
        int start = _currentTargetIndex;
        int remainingCount = _pendingTargets.Count - start;
        if (remainingCount <= 1)
            return;

        var remaining = _pendingTargets.GetRange(start, remainingCount);
        var remainingSet = new HashSet<TileCoordinate>(remaining);
        var current = _actor.Tile;

        // Bounded candidate window ordered by Manhattan distance (stable tie-break).
        var window = remaining
            .OrderBy(t => current.ManhattanDistanceTo(t))
            .ThenBy(t => t.Y)
            .ThenBy(t => t.X)
            .Take(MaxOrderingCandidates)
            .ToList();

        var costByTile = new Dictionary<TileCoordinate, int>();
        foreach (var tile in window)
        {
            if (tile == current)
            {
                costByTile[tile] = 1;
                continue;
            }

            var path = _navigator.FindPathToInteract(_actor, _currentRequest!.LocationId, tile, _avoidTiles);
            costByTile[tile] = path.Success && path.Steps.Count > 0 ? path.Steps.Count : int.MaxValue;
        }

        int Connectivity(TileCoordinate tile) =>
            tile.CardinalNeighbors().Count(remainingSet.Contains);

        int Cost(TileCoordinate tile) => costByTile.TryGetValue(tile, out int cost) ? cost : int.MaxValue;

        var ordered = remaining
            .OrderBy(Cost)
            .ThenByDescending(Connectivity)
            .ThenBy(t => current.ManhattanDistanceTo(t))
            .ThenBy(t => t.Y)
            .ThenBy(t => t.X)
            .ToList();

        for (int i = 0; i < ordered.Count; i++)
        {
            _pendingTargets[start + i] = ordered[i];
        }
    }

    private void PlanPathToCurrentTarget()
    {
        if (_currentRequest is null) return;

        var pathResult = _navigator.FindPathToInteract(_actor, _currentRequest.LocationId, _currentTargetTile, _avoidTiles);
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

        // Dynamic collision check BEFORE stepping towards destination tile.
        // This uses exactly the same obstacle set as SameMapNavigator planning:
        // native passability + dynamic player occupancy + recently blocked tiles.
        // The tile the actor already stands on needs no check: it was reached legally,
        // and rejecting it would make every replan fail identically.
        bool isPassable = step.Tile == _actor.Tile ||
                          (_observer.IsTilePassable(_currentRequest!.LocationId, step.Tile) &&
                           !_observer.IsPlayerOnTile(_currentRequest!.LocationId, step.Tile) &&
                           !_avoidTiles.Contains(step.Tile));

        if (!isPassable)
        {
            _actor.Halt();
            // Remember the blocked tile so the replan cannot route through it again.
            _avoidTiles.Add(step.Tile);
            _replanCount++;
            if (_replanCount > MaxReplansPerTarget)
            {
                _failedTiles.Add(new FailedTileInfo(_currentTargetTile, "Dynamic obstacle blocked path and maximum replans exceeded."));
                AdvanceToNextTarget();
                return;
            }

            // Replan from current tile, excluding all known blocked positions.
            var replanResult = _navigator.FindPathToInteract(_actor, _currentRequest!.LocationId, _currentTargetTile, _avoidTiles);
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
        // 1. Safe boundary: Cancellation before tool swing
        if (_cancelRequested)
        {
            _cancelRequested = false;
            _actor.Halt();
            _avatar?.SetAnimation("idle");
            var terminalState = _wateredTiles.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Cancelled;
            FinishExecution(terminalState, _cancelReason ?? "Execution cancelled by request.");
            return;
        }

        // 2. Safe boundary: Pause before tool swing
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

        // Pre-tool re-observation: check if tile was watered concurrently
        var dirt = _observer.GetDirtState(_currentRequest!.LocationId, _currentTargetTile);
        if (dirt.IsWatered)
        {
            _skippedTiles.Add(new SkippedTileInfo(_currentTargetTile, "ConcurrentlyWatered"));
            AdvanceToNextTarget();
            return;
        }

        // Pre-action budget checks
        if (_staminaUsed + NormalWateringCanAdapter.BaseStaminaCost > _currentRequest.MaxStamina)
        {
            _actor.Halt();
            var terminalState = _wateredTiles.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Failed;
            FinishExecution(terminalState, "Stamina budget exhausted.", "BUDGET_EXHAUSTED");
            return;
        }

        if (_waterUsed + NormalWateringCanAdapter.BaseWaterCost > _currentRequest.MaxWater)
        {
            _actor.Halt();
            var terminalState = _wateredTiles.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Failed;
            FinishExecution(terminalState, "Water budget exhausted.", "BUDGET_EXHAUSTED");
            return;
        }

        // Initiate normal tool lifecycle
        CurrentState = ExecutionState.Watering;
        _toolEffectExecuted = false;
        Log($"Beginning watering tool lifecycle on tile ({_currentTargetTile.X}, {_currentTargetTile.Y}).");
        _actor.BeginUsingTool();
        _avatar?.SetAnimation("watering");
    }

    private void TickWatering(GameTime? time, long tickCount)
    {
        var phase = _actor.UpdateToolAnimation(time, tickCount);

        // Real lifecycle effect point
        if (phase == ToolAnimationPhase.EffectPoint && !_toolEffectExecuted)
        {
            _toolEffectExecuted = true;
            var waterResult = _adapter.WaterTile(_actor, _currentRequest!.LocationId, _currentTargetTile);
            if (!waterResult.Success)
            {
                Log($"Watering effect failed on tile ({_currentTargetTile.X}, {_currentTargetTile.Y}): {waterResult.ErrorMessage ?? "Tool execution failed"}", LogLevel.Warn);
                _failedTiles.Add(new FailedTileInfo(_currentTargetTile, waterResult.ErrorMessage ?? "Tool execution failed"));
                _actor.EndUsingTool();
                _avatar?.SetAnimation("idle");
                AdvanceToNextTarget();
                return;
            }

            _staminaUsed += waterResult.StaminaCost;
            _waterUsed += waterResult.WaterCost;
            Log($"Watering effect applied on tile ({_currentTargetTile.X}, {_currentTargetTile.Y}): stamina -{waterResult.StaminaCost}, water -{waterResult.WaterCost}.");
        }
        else if (phase == ToolAnimationPhase.Completed)
        {
            _actor.EndUsingTool();
            _avatar?.SetAnimation("idle");

            if (!_toolEffectExecuted)
            {
                // FAIL CLOSED: tool animation completed without executing watering effect.
                // Forbid verifying or reporting success.
                Log($"Tool animation completed without executing watering effect on tile ({_currentTargetTile.X}, {_currentTargetTile.Y}).", LogLevel.Warn);
                _failedTiles.Add(new FailedTileInfo(_currentTargetTile, "Tool animation completed without executing watering effect."));
                AdvanceToNextTarget();
                return;
            }

            CurrentState = ExecutionState.Verifying;
        }
    }

    private void TickVerifying()
    {
        if (!_toolEffectExecuted)
        {
            _failedTiles.Add(new FailedTileInfo(_currentTargetTile, "Result verification failed: Tool effect was never executed."));
            AdvanceToNextTarget();
            return;
        }

        var dirt = _observer.GetDirtState(_currentRequest!.LocationId, _currentTargetTile);
        if (dirt.IsWatered)
        {
            _wateredTiles.Add(_currentTargetTile);
            Log($"Verified tile ({_currentTargetTile.X}, {_currentTargetTile.Y}) is watered.");
        }
        else
        {
            Log($"Verification failed: tile ({_currentTargetTile.X}, {_currentTargetTile.Y}) remained unwatered after tool effect.", LogLevel.Warn);
            _failedTiles.Add(new FailedTileInfo(_currentTargetTile, "Result verification failed: Dirt remained unwatered."));
        }

        AdvanceToNextTarget();
    }

    private void AdvanceToNextTarget()
    {
        // Explicit cursor increment: marks current target completed so pause/resume NEVER repeats it
        _currentTargetIndex++;

        if (_currentTargetIndex >= _pendingTargets.Count)
        {
            // All targets processed
            ExecutionState finalState;
            if (_failedTiles.Count == 0 && _wateredTiles.Count == _pendingTargets.Count)
                finalState = ExecutionState.Succeeded;
            else if (_wateredTiles.Count > 0)
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
            var terminalState = _wateredTiles.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Cancelled;
            FinishExecution(terminalState, _cancelReason ?? "Execution cancelled by request.");
            return;
        }

        // Resource & budget checks before commencing next target
        if (_staminaUsed + NormalWateringCanAdapter.BaseStaminaCost > _currentRequest!.MaxStamina)
        {
            _actor.Halt();
            FinishExecution(ExecutionState.PartiallySucceeded, "Stamina budget exhausted.", "BUDGET_EXHAUSTED");
            return;
        }

        if (_waterUsed + NormalWateringCanAdapter.BaseWaterCost > _currentRequest.MaxWater)
        {
            _actor.Halt();
            FinishExecution(ExecutionState.PartiallySucceeded, "Water budget exhausted.", "BUDGET_EXHAUSTED");
            return;
        }

        if (_actor.IsWateringCanEmpty)
        {
            _actor.Halt();
            FinishExecution(ExecutionState.PartiallySucceeded, "Watering can became empty.", "CAN_EMPTY", retryRecommended: true);
            return;
        }

        if (_actor.Stamina < NormalWateringCanAdapter.BaseStaminaCost)
        {
            _actor.Halt();
            FinishExecution(ExecutionState.PartiallySucceeded, "Companion stamina exhausted.", "INSUFFICIENT_STAMINA", retryRecommended: true);
            return;
        }

        // A new target starts from a clean obstacle memo: player occupancy is
        // re-read from native state by the navigator, so a stale block for one
        // target must not permanently exclude a tile for later targets.
        _avoidTiles.Clear();
        ReorderRemainingTargets();
        _currentTargetTile = _pendingTargets[_currentTargetIndex];
        _replanCount = 0;
        PlanPathToCurrentTarget();
    }

    private WaterZoneResult FinishExecution(ExecutionState state, string? errorMessage = null, string? errorCode = null, bool retryRecommended = false)
    {
        CurrentState = state;
        _actor.Halt();
        _actor.EndUsingTool();
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

        var result = new WaterZoneResult(
            commandId: _currentRequest?.CommandId ?? "cmd-unknown",
            taskId: _currentRequest?.TaskId ?? "task-unknown",
            finalState: state,
            wateredTiles: _wateredTiles.ToList(),
            skippedTiles: _skippedTiles.ToList(),
            failedTiles: _failedTiles.ToList(),
            staminaUsed: _staminaUsed,
            waterUsed: _waterUsed,
            gameMinutesElapsed: gameMinutesElapsed,
            finalWorldRevision: _observer.WorldRevision,
            errorMessage: errorMessage,
            errorCode: errorCode,
            retryRecommended: retryRecommended
        );

        var level = state is ExecutionState.Failed or ExecutionState.Rejected ? LogLevel.Warn : LogLevel.Info;
        Log($"Water-zone finished: {state}; watered={_wateredTiles.Count}, skipped={_skippedTiles.Count}, failed={_failedTiles.Count}" +
            (errorMessage is not null ? $"; error={errorMessage}" : "."), level);
        foreach (var failed in _failedTiles)
        {
            Log($"  failed tile ({failed.Tile.X}, {failed.Tile.Y}): {failed.Reason}", LogLevel.Warn);
        }

        FinalResult = result;
        OnCompleted?.Invoke(result);

        if (state == ExecutionState.Succeeded)
        {
            NotifyPlayer($"AI Companion: Watering task completed ({_wateredTiles.Count} tiles).");
        }
        else if (state == ExecutionState.Cancelled)
        {
            NotifyPlayer($"AI Companion: Task cancelled ({_wateredTiles.Count}/{_pendingTargets.Count} watered).");
        }
        else if (state == ExecutionState.PartiallySucceeded)
        {
            if (_cancelRequested || errorMessage?.Contains("cancel", StringComparison.OrdinalIgnoreCase) == true)
            {
                NotifyPlayer($"AI Companion: Task cancelled ({_wateredTiles.Count}/{_pendingTargets.Count} watered).");
            }
            else
            {
                NotifyPlayer($"AI Companion: Task partially completed ({_wateredTiles.Count}/{_pendingTargets.Count} watered).");
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
                _avoidTiles.Clear();
                ReorderRemainingTargets();
                _currentTargetTile = _pendingTargets[_currentTargetIndex];
                _replanCount = 0;
                // Re-observe world and re-plan from current position to next unwatered target
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
