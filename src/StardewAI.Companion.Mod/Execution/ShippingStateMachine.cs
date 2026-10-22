using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>
/// Tick-driven execution state machine for the "ship-items" skill.
/// Navigates the companion adjacent to the farm shipping bin, then transfers genuine item
/// instances into the farm shipping bin via IShippingAdapter with full lifecycle guarantees:
/// safe pause/resume/cancel boundaries, measured clock budget, monotonic timeout,
/// result verification, and independent stamina/human-player isolation.
/// </summary>
public sealed class ShippingStateMachine : ISkillExecutionMachine
{
    private const float WalkPixelsPerTick = 4f;
    private const int MaxReplansPerTarget = 3;
    private const long MaxNoProgressTicks = 3600;

    private readonly IFarmerActor _actor;
    private readonly CompanionAvatar? _avatar;
    private readonly IWorldObserver _observer;
    private readonly SameMapNavigator _navigator;
    private readonly IShippingAdapter _adapter;
    private readonly Action<string, LogLevel>? _log;

    private readonly object _stateLock = new();

    private ShippingRequest? _currentRequest;
    private List<ShippingItemRequest> _pendingItems = new();
    private int _currentItemIndex;
    private List<TileCoordinate> _shippingBinFootprint = new();
    private TileCoordinate? _targetFootprintTile;
    private List<PathStep> _currentPath = new();
    private int _currentPathIndex;
    private int _replanCount;

    private readonly List<ShippedItemInfo> _shippedItems = new();
    private readonly List<SkippedShippingItemInfo> _skippedItems = new();
    private readonly List<FailedShippingItemInfo> _failedItems = new();
    private int _estimatedTotalValue;
    private int _shippingBinTotalCount;
    private readonly Dictionary<string, int> _remainingBackpackCounts = new(StringComparer.OrdinalIgnoreCase);

    private long _elapsedTicks;
    private readonly ExecutionProgressWatchdog _progressWatchdog = new();
    private int _startClock;

    private volatile bool _cancelRequested;
    private volatile string? _cancelReason;
    private volatile bool _pauseRequested;

    public ExecutionState CurrentState { get; private set; } = ExecutionState.Created;
    public bool IsExecuting => CurrentState is ExecutionState.Validating or ExecutionState.Preparing
        or ExecutionState.Navigating or ExecutionState.Facing or ExecutionState.Acting
        or ExecutionState.Paused or ExecutionState.Cancelling;
    public bool IsPaused => CurrentState == ExecutionState.Paused;
    public ShippingResult? FinalResult { get; private set; }

    public ShippingRequest? CurrentRequest => _currentRequest;
    public string? ActiveTaskId => _currentRequest?.TaskId;
    public int TotalTargets => _pendingItems.Count;
    public int CurrentTargetIndex => _currentItemIndex;
    public int ShippedCount => _shippedItems.Count;
    public int SkippedCount => _skippedItems.Count;
    public int FailedCount => _failedItems.Count;

    public event Action<ShippingResult>? OnCompleted;
    public event Action<string>? OnNotification;

    private void NotifyPlayer(string message) => OnNotification?.Invoke(message);

    public ShippingStateMachine(
        IFarmerActor actor,
        IWorldObserver observer,
        SameMapNavigator navigator,
        IShippingAdapter adapter,
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

    public bool Start(ShippingRequest request, out ShippingResult? earlyTerminalResult)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_stateLock)
        {
            _currentRequest = request;
            _pendingItems = request.Items.ToList();
            _currentItemIndex = 0;
            _shippedItems.Clear();
            _skippedItems.Clear();
            _failedItems.Clear();
            _remainingBackpackCounts.Clear();
            _estimatedTotalValue = 0;
            _shippingBinTotalCount = 0;
            _elapsedTicks = 0;
            _progressWatchdog.Reset();
            _replanCount = 0;
            _startClock = _observer.TimeOfDay;
            _cancelRequested = false;
            _cancelReason = null;
            _pauseRequested = false;
            FinalResult = null;

            _actor.SetActiveTask(request.TaskId);

            // Phase 1: Validating
            CurrentState = ExecutionState.Validating;

            if (!string.Equals(request.LocationId, "Farm", StringComparison.OrdinalIgnoreCase))
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    $"Shipping is only supported on 'Farm', requested '{request.LocationId}'.", "INVALID_LOCATION");
                return false;
            }

            // The observer's current map belongs to the player. Zone work runs on the
            // companion, which may have navigated into a different location.
            if (!string.Equals(_actor.LocationName, request.LocationId, StringComparison.OrdinalIgnoreCase))
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    $"Location mismatch: Companion is on map '{_actor.LocationName}', but request specifies '{request.LocationId}'.",
                    "LOCATION_MISMATCH");
                return false;
            }

            if (request.MaxGameMinutes < 0)
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    "MaxGameMinutes must be non-negative; 0 means no time limit.", "INVALID_PARAMETERS");
                return false;
            }

            if (_pendingItems.Count == 0)
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    "items list cannot be empty.", "INVALID_PARAMETERS");
                return false;
            }

            // Precheck companion inventory count and shippability for each requested item
            foreach (var itemReq in _pendingItems)
            {
                if (string.IsNullOrWhiteSpace(itemReq.ItemId) || itemReq.Count < 1)
                {
                    CurrentState = ExecutionState.Rejected;
                    _actor.SetActiveTask(null);
                    earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                        "All items entries must specify a valid itemId and count >= 1.", "INVALID_PARAMETERS");
                    return false;
                }

                int availableCount = _actor.GetItemCount(itemReq.ItemId);
                if (availableCount < itemReq.Count)
                {
                    CurrentState = ExecutionState.Rejected;
                    _actor.SetActiveTask(null);
                    earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                        $"Companion has insufficient count for '{itemReq.ItemId}': holds {availableCount}, requested {itemReq.Count}.",
                        "INSUFFICIENT_ITEMS");
                    return false;
                }

                if (!_adapter.CheckItemShippable(_actor, itemReq.ItemId, out var unshippingReason))
                {
                    CurrentState = ExecutionState.Rejected;
                    _actor.SetActiveTask(null);
                    earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                        $"Item '{itemReq.ItemId}' cannot be shipped: {unshippingReason}", "ITEM_NOT_SHIPPABLE");
                    return false;
                }
            }

            // Find shipping bin footprint dynamically
            _shippingBinFootprint = _adapter.GetShippingBinFootprint("Farm").ToList();
            if (_shippingBinFootprint.Count == 0)
            {
                CurrentState = ExecutionState.Failed;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Failed,
                    "No shipping bin found on Farm.", "SHIPPING_BIN_NOT_FOUND");
                return false;
            }

            // Phase 2: Preparing and pathfinding
            CurrentState = ExecutionState.Preparing;
            PlanPathToShippingBin();

            earlyTerminalResult = null;
            NotifyPlayer($"AI Companion: Shipping task started ({_pendingItems.Count} item batch).");
            return true;
        }
    }

    public void Update(GameTime? time, long tickCount)
    {
        lock (_stateLock)
        {
            if (!IsExecuting || _currentRequest is null)
                return;

            // 1. High-priority cancellation
            if (_cancelRequested)
            {
                _cancelRequested = false;
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                var terminalState = _shippedItems.Count > 0
                    ? ExecutionState.PartiallySucceeded
                    : ExecutionState.Cancelled;
                FinishExecution(terminalState, _cancelReason ?? "Execution cancelled by request.");
                return;
            }

            // 2. High-priority pause at safe boundary
            if (_pauseRequested && CurrentState is ExecutionState.Navigating or ExecutionState.Facing)
            {
                _pauseRequested = false;
                CurrentState = ExecutionState.Paused;
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                NotifyPlayer("AI Companion: Shipping task paused.");
                return;
            }

            // 3. Game-level pause safety guard
            try
            {
                if (Game1.paused || StardewAI.Companion.Mod.Menus.CompanionMenuClock.HasBlockingMenu || !Context.IsWorldReady)
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
            if (_currentRequest.MaxGameMinutes > 0 && gameMinutesElapsed > _currentRequest.MaxGameMinutes)
            {
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                var terminalState = _shippedItems.Count > 0
                    ? ExecutionState.PartiallySucceeded
                    : ExecutionState.Failed;
                FinishExecution(terminalState, $"MaxGameMinutes budget ({_currentRequest.MaxGameMinutes} min) exceeded.", "BUDGET_EXHAUSTED");
                return;
            }

            // 5. No-progress timeout guard
            if (_progressWatchdog.HasTimedOut(_elapsedTicks, MaxNoProgressTicks, _actor, CurrentTargetIndex))
            {
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                var terminalState = _shippedItems.Count > 0
                    ? ExecutionState.PartiallySucceeded
                    : ExecutionState.Failed;
                FinishExecution(terminalState, "Execution made no progress within the stall timeout.", "TIMEOUT");
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
                }
            }
            catch (Exception ex)
            {
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                Log($"Exception during shipping execution: {ex}", LogLevel.Error);
                FinishExecution(ExecutionState.Failed, $"Execution failed due to exception: {ex.Message}", "EXECUTION_EXCEPTION");
            }
        }
    }

    private void PlanPathToShippingBin()
    {
        if (_currentRequest is null) return;

        // Check if already standing adjacent to any footprint tile
        var alreadyAdjacent = _shippingBinFootprint.FirstOrDefault(t => _actor.Tile.IsAdjacentTo(t));
        if (alreadyAdjacent != default)
        {
            _targetFootprintTile = alreadyAdjacent;
            CurrentState = ExecutionState.Facing;
            return;
        }

        // Find shortest path to interact with any tile in the footprint
        IReadOnlyList<PathStep>? bestSteps = null;
        TileCoordinate? bestTile = null;

        foreach (var footprintTile in _shippingBinFootprint)
        {
            var pathResult = _navigator.FindPathToInteract(_actor, _currentRequest.LocationId, footprintTile);
            if (pathResult.Success && pathResult.Steps.Count > 0)
            {
                if (bestSteps is null || pathResult.Steps.Count < bestSteps.Count)
                {
                    bestSteps = pathResult.Steps;
                    bestTile = footprintTile;
                }
            }
        }

        if (bestSteps is null || !bestTile.HasValue)
        {
            FinishExecution(ExecutionState.Failed,
                "Shipping bin is unreachable from current position.", "SHIPPING_BIN_UNREACHABLE");
            return;
        }

        _targetFootprintTile = bestTile.Value;
        _currentPath = bestSteps.ToList();
        _currentPathIndex = 0;
        CurrentState = ExecutionState.Navigating;
    }

    private void TickNavigating()
    {
        if (_currentPathIndex >= _currentPath.Count)
        {
            _actor.Halt();
            CurrentState = ExecutionState.Facing;
            return;
        }

        var step = _currentPath[_currentPathIndex];
        if (ActorGuards.HasReachedWalkingTile(_actor, step.Tile)) {
            _actor.Halt(); _currentPathIndex++; return;
        }

        // Dynamic collision check BEFORE stepping
        bool isPassable = step.Tile == _actor.Tile ||
                          (_observer.IsTilePassable(_currentRequest!.LocationId, step.Tile) &&
                           !_observer.IsPlayerOnTile(_currentRequest!.LocationId, step.Tile));

        if (!isPassable)
        {
            _actor.Halt();
            _replanCount++;
            if (_replanCount > MaxReplansPerTarget)
            {
                FinishExecution(ExecutionState.Failed,
                    "Dynamic obstacle blocked path to shipping bin and maximum replans exceeded.", "SHIPPING_BIN_UNREACHABLE");
                return;
            }

            PlanPathToShippingBin();
            return;
        }

        var walkingTarget = ActorGuards.WalkingTargetPixel(_actor, step.Tile);

        var currentPos = _actor.PixelPosition;
        float dx = walkingTarget.X - currentPos.X;
        float dy = walkingTarget.Y - currentPos.Y;

        float dist = MathF.Sqrt(dx * dx + dy * dy);
        if (ActorGuards.IsPlayerBound(_actor))
        {
            // The native actor ignores position setters. Keep driving its held
            // movement until the measured standing center actually reaches the tile.
            _actor.MovePixels(dx, dy);
            return;
        }
        if (dist <= WalkPixelsPerTick)
        {
            _actor.PixelPosition = new Vector2(step.Tile.X * 64, step.Tile.Y * 64);
            _actor.Face(step.Facing);
            _currentPathIndex++;
            _replanCount = 0;
        }
        else
        {
            float moveX = (dx / dist) * WalkPixelsPerTick;
            float moveY = (dy / dist) * WalkPixelsPerTick;
            _actor.MovePixels(moveX, moveY);
        }
    }

    private void TickFacing()
    {
        if (_currentRequest is null) return;

        if (_cancelRequested)
        {
            _cancelRequested = false;
            _actor.Halt();
            _avatar?.SetAnimation("idle");
            FinishExecution(ExecutionState.Cancelled, _cancelReason ?? "Execution cancelled by request.");
            return;
        }

        if (_pauseRequested)
        {
            _pauseRequested = false;
            CurrentState = ExecutionState.Paused;
            _actor.Halt();
            _avatar?.SetAnimation("idle");
            return;
        }

        // Face towards adjacent footprint tile
        var adjacentFootprint = _targetFootprintTile ?? _shippingBinFootprint.FirstOrDefault(t => _actor.Tile.IsAdjacentTo(t));
        if (adjacentFootprint != default)
        {
            var requiredFacing = FacingDirectionExtensions.DirectionToAdjacent(_actor.Tile, adjacentFootprint);
            if (requiredFacing.HasValue)
            {
                _actor.Face(requiredFacing.Value);
            }
        }

        CurrentState = ExecutionState.Acting;
        _avatar?.SetAnimation("action");
    }

    private void TickActing()
    {
        if (_currentRequest is null) return;

        if (_pauseRequested)
        {
            _pauseRequested = false;
            CurrentState = ExecutionState.Paused;
            _actor.Halt();
            _avatar?.SetAnimation("idle");
            return;
        }

        if (_cancelRequested)
        {
            _cancelRequested = false;
            _actor.Halt();
            _avatar?.SetAnimation("idle");
            var terminalState = _shippedItems.Count > 0
                ? ExecutionState.PartiallySucceeded
                : ExecutionState.Cancelled;
            FinishExecution(terminalState, _cancelReason ?? "Execution cancelled by request.");
            return;
        }

        // Process items sequentially
        while (_currentItemIndex < _pendingItems.Count)
        {
            var req = _pendingItems[_currentItemIndex];
            var result = _adapter.ShipItem(_actor, _currentRequest.LocationId, req.ItemId, req.Count);

            if (result.Success && result.ShippedItem != null)
            {
                _shippedItems.Add(result.ShippedItem);
                _estimatedTotalValue += result.ShippedItem.EstimatedTotalValue;
                _shippingBinTotalCount = result.ShippingBinTotalCount;
                _remainingBackpackCounts[req.ItemId] = result.ShippedItem.RemainingBackpackCount;
            }
            else if (result.PreconditionFailed)
            {
                _skippedItems.Add(new SkippedShippingItemInfo(req.ItemId, req.Count, result.SkipReason ?? result.ErrorMessage ?? "precondition-failed"));
            }
            else
            {
                _failedItems.Add(new FailedShippingItemInfo(req.ItemId, req.Count, result.ErrorMessage ?? "shipping-failed"));
            }

            _currentItemIndex++;
        }

        _actor.Halt();
        _avatar?.SetAnimation("idle");

        ExecutionState finalState;
        if (_failedItems.Count == 0 && _skippedItems.Count == 0 && _shippedItems.Count > 0)
        {
            finalState = ExecutionState.Succeeded;
        }
        else if (_shippedItems.Count > 0)
        {
            finalState = ExecutionState.PartiallySucceeded;
        }
        else
        {
            finalState = ExecutionState.Failed;
        }

        FinishExecution(finalState);
    }

    private ShippingResult FinishExecution(
        ExecutionState terminalState,
        string? errorMessage = null,
        string? errorCode = null)
    {
        CurrentState = terminalState;
        _actor.SetActiveTask(null);
        _avatar?.SetAnimation("idle");

        int gameMinutesElapsed = _currentRequest != null
            ? IWorldObserver.CalculateGameMinutesElapsed(_startClock, _observer.TimeOfDay)
            : 0;

        var result = new ShippingResult(
            commandId: _currentRequest?.CommandId ?? "",
            taskId: _currentRequest?.TaskId ?? "",
            finalState: terminalState,
            shippedItems: _shippedItems.ToList(),
            skippedItems: _skippedItems.ToList(),
            failedItems: _failedItems.ToList(),
            estimatedTotalValue: _estimatedTotalValue,
            shippingBinTotalCount: _shippingBinTotalCount > 0 ? _shippingBinTotalCount : _adapter.GetShippingBinTotalCount(_actor),
            remainingBackpackCounts: new Dictionary<string, int>(_remainingBackpackCounts),
            staminaUsed: 0f,
            waterUsed: 0,
            gameMinutesElapsed: gameMinutesElapsed,
            finalWorldRevision: _observer.WorldRevision,
            errorMessage: errorMessage,
            errorCode: errorCode
        );

        FinalResult = result;
        OnCompleted?.Invoke(result);

        string statusText = terminalState switch
        {
            ExecutionState.Succeeded => "完成",
            ExecutionState.PartiallySucceeded => "部分完成",
            ExecutionState.Cancelled => "已取消",
            ExecutionState.Rejected => "未开始",
            _ => "未完成"
        };
        NotifyPlayer($"伙伴出货{statusText}：已投入 {_shippedItems.Sum(item => item.Count)} 件，预计 {_estimatedTotalValue} 金，睡觉后结算。");

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
            if (CurrentState == ExecutionState.Paused)
            {
                CurrentState = _currentPathIndex < _currentPath.Count
                    ? ExecutionState.Navigating
                    : ExecutionState.Facing;
                NotifyPlayer("AI Companion: Shipping task resumed.");
            }
        }
    }
}
