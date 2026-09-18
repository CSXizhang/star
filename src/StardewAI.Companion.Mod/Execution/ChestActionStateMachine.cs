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
/// Tick-driven execution state machine for the "deposit-chest" and "organize-chest" skills.
/// Navigates the companion adjacent to a normal farm chest, then either moves companion
/// inventory items into the chest (genuine chest transfer via IChestAdapter, tools always
/// excluded, stops early when the chest is full) or merges same-type stacks inside the
/// chest (Item.addToStack, conservation-checked). Lifecycle mirrors the water-zone machine:
/// safe pause/resume/cancel boundaries, measured clock budget, and monotonic timeout.
/// </summary>
public sealed class ChestActionStateMachine : ISkillExecutionMachine
{
    private const float WalkPixelsPerTick = 4f;
    private const int MaxReplansPerTarget = 3;
    private const long MaxMonotonicTicks = 3600; // 60 seconds at 60 fps safety timeout

    private readonly IFarmerActor _actor;
    private readonly CompanionAvatar? _avatar;
    private readonly IWorldObserver _observer;
    private readonly SameMapNavigator _navigator;
    private readonly IChestAdapter _adapter;
    private readonly Action<string, LogLevel>? _log;

    private readonly object _stateLock = new();

    // Active execution context
    private ChestActionRequest? _currentRequest;
    private List<InventoryItem> _depositPlan = new();
    private int _depositPlanIndex;
    private int _withdrawPlanIndex;
    private List<PathStep> _currentPath = new();
    private int _currentPathIndex;
    private int _replanCount;

    // Output tracking
    private readonly List<DepositedItemInfo> _depositedItems = new();
    private readonly List<WithdrawnItemInfo> _withdrawnItems = new();
    private readonly List<SkippedItemInfo> _skippedItems = new();
    private readonly List<FailedItemInfo> _failedItems = new();
    private readonly List<ChestMergeInfo> _merges = new();
    private long _elapsedTicks;
    private int _startClock;

    // Signals
    private volatile bool _cancelRequested;
    private volatile string? _cancelReason;
    private volatile bool _pauseRequested;

    public ExecutionState CurrentState { get; private set; } = ExecutionState.Created;
    public bool IsExecuting => CurrentState is ExecutionState.Validating or ExecutionState.Preparing
        or ExecutionState.Navigating or ExecutionState.Facing or ExecutionState.Acting
        or ExecutionState.Paused or ExecutionState.Cancelling;
    public bool IsPaused => CurrentState == ExecutionState.Paused;
    public ChestActionResult? FinalResult { get; private set; }

    public ChestActionRequest? CurrentRequest => _currentRequest;
    public string? ActiveTaskId => _currentRequest?.TaskId;
    public int TotalTargets => _currentRequest?.Kind == ChestActionKind.Organize ? 1
        : _currentRequest?.Kind == ChestActionKind.Withdraw ? (_currentRequest.WithdrawItems?.Count ?? 0)
        : _depositPlan.Count;
    public int CurrentTargetIndex => _currentRequest?.Kind == ChestActionKind.Withdraw ? _withdrawPlanIndex : _depositPlanIndex;
    public int DepositedCount => _depositedItems.Count;
    public int WithdrawnCount => _withdrawnItems.Count;
    public int SkippedCount => _skippedItems.Count;
    public int FailedCount => _failedItems.Count;
    public int MergedCount => _merges.Count;

    public event Action<ChestActionResult>? OnCompleted;
    public event Action<string>? OnNotification;

    private void NotifyPlayer(string message) => OnNotification?.Invoke(message);

    public ChestActionStateMachine(
        IFarmerActor actor,
        IWorldObserver observer,
        SameMapNavigator navigator,
        IChestAdapter adapter,
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
    /// Starts execution of a ChestActionRequest.
    /// </summary>
    public bool Start(ChestActionRequest request, out ChestActionResult? earlyTerminalResult)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_stateLock)
        {
            _currentRequest = request;
            _depositedItems.Clear();
            _withdrawnItems.Clear();
            _skippedItems.Clear();
            _failedItems.Clear();
            _merges.Clear();
            _elapsedTicks = 0;
            _replanCount = 0;
            _depositPlanIndex = 0;
            _withdrawPlanIndex = 0;
            _startClock = _observer.TimeOfDay;
            _cancelRequested = false;
            _cancelReason = null;
            _pauseRequested = false;
            FinalResult = null;

            _actor.SetActiveTask(request.TaskId);

            // Phase 1: Validating
            CurrentState = ExecutionState.Validating;

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

            // The chest must exist and be a normal chest (fridge/special types excluded by the observer)
            if (_observer.GetChestAt(request.LocationId, request.ChestTile) is null)
            {
                CurrentState = ExecutionState.Failed;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Failed,
                    $"No normal chest found at tile ({request.ChestTile.X}, {request.ChestTile.Y}).",
                    "CHEST_NOT_FOUND");
                return false;
            }

            // Phase 2: Preparing (deposit / withdraw planning)
            CurrentState = ExecutionState.Preparing;
            _depositPlan.Clear();

            if (request.Kind == ChestActionKind.Withdraw)
            {
                if (request.WithdrawItems == null || request.WithdrawItems.Count == 0)
                {
                    CurrentState = ExecutionState.Rejected;
                    _actor.SetActiveTask(null);
                    earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                        "WithdrawItems list cannot be empty for withdraw-chest.",
                        "INVALID_PARAMETERS");
                    return false;
                }
            }
            else if (request.Kind == ChestActionKind.Deposit)
            {
                var inventory = _actor.GetInventorySnapshot();
                HashSet<string>? filter = request.ItemIds is { Count: > 0 }
                    ? new HashSet<string>(request.ItemIds.Select(id => id.StartsWith("(") ? id : $"(O){id}"), StringComparer.Ordinal)
                    : null;

                foreach (var slot in inventory)
                {
                    if (slot.IsTool)
                    {
                        if (filter is null || filter.Contains(slot.ItemId))
                        {
                            _skippedItems.Add(new SkippedItemInfo(slot.ItemId, "tool-excluded"));
                        }
                        continue;
                    }

                    if (filter is not null && !filter.Contains(slot.ItemId))
                    {
                        continue;
                    }

                    _depositPlan.Add(slot);
                }

                if (_depositPlan.Count == 0)
                {
                    CurrentState = ExecutionState.Succeeded;
                    _actor.SetActiveTask(null);
                    earlyTerminalResult = FinishExecution(ExecutionState.Succeeded);
                    return true;
                }
            }

            PlanPathToChest();

            earlyTerminalResult = null;
            NotifyPlayer(request.Kind switch
            {
                ChestActionKind.Deposit => $"AI Companion: Deposit task started ({_depositPlan.Count} items).",
                ChestActionKind.Withdraw => $"AI Companion: Withdraw task started ({request.WithdrawItems?.Count ?? 0} items).",
                _ => "AI Companion: Organize task started."
            });
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
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                var terminalState = _depositedItems.Count > 0 || _merges.Count > 0
                    ? ExecutionState.PartiallySucceeded
                    : ExecutionState.Cancelled;
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
                var terminalState = _depositedItems.Count > 0 || _merges.Count > 0
                    ? ExecutionState.PartiallySucceeded
                    : ExecutionState.Failed;
                FinishExecution(terminalState, $"MaxGameMinutes budget ({_currentRequest.MaxGameMinutes} min) exceeded.", "BUDGET_EXHAUSTED");
                return;
            }

            // 5. Monotonic timeout guard
            if (_elapsedTicks > MaxMonotonicTicks)
            {
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                var terminalState = _depositedItems.Count > 0 || _merges.Count > 0
                    ? ExecutionState.PartiallySucceeded
                    : ExecutionState.Failed;
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
                }
            }
            catch (Exception ex)
            {
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                Log($"Exception during execution (state={CurrentState}, chest=({_currentRequest.ChestTile.X}, {_currentRequest.ChestTile.Y})): {ex}", LogLevel.Error);
                FinishExecution(ExecutionState.Failed, $"Execution failed due to exception: {ex.Message}", "EXECUTION_EXCEPTION");
            }
        }
    }

    private void PlanPathToChest()
    {
        if (_currentRequest is null) return;

        var pathResult = _navigator.FindPathToInteract(_actor, _currentRequest.LocationId, _currentRequest.ChestTile);
        if (!pathResult.Success)
        {
            Log($"Navigation to chest ({_currentRequest.ChestTile.X}, {_currentRequest.ChestTile.Y}) failed: {pathResult.ErrorMessage ?? "Navigation failed"}", LogLevel.Warn);
            FinishExecution(ExecutionState.Failed,
                $"Chest at ({_currentRequest.ChestTile.X}, {_currentRequest.ChestTile.Y}) is unreachable: {pathResult.ErrorMessage ?? "Navigation failed"}",
                "CHEST_UNREACHABLE");
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

        // Dynamic collision check BEFORE stepping towards destination tile
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
                    "Dynamic obstacle blocked path and maximum replans exceeded.", "CHEST_UNREACHABLE");
                return;
            }

            // Replan from current tile
            var replanResult = _navigator.FindPathToInteract(_actor, _currentRequest!.LocationId, _currentRequest.ChestTile);
            if (!replanResult.Success)
            {
                FinishExecution(ExecutionState.Failed,
                    $"Chest became unreachable: {replanResult.ErrorMessage ?? "Navigation replan failed"}", "CHEST_UNREACHABLE");
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
        if (_currentRequest is null) return;

        // 1. Safe boundary: Cancellation before the chest action
        if (_cancelRequested)
        {
            _cancelRequested = false;
            _actor.Halt();
            _avatar?.SetAnimation("idle");
            var terminalState = _depositedItems.Count > 0 || _merges.Count > 0
                ? ExecutionState.PartiallySucceeded
                : ExecutionState.Cancelled;
            FinishExecution(terminalState, _cancelReason ?? "Execution cancelled by request.");
            return;
        }

        // 2. Safe boundary: Pause before the chest action
        if (_pauseRequested)
        {
            _pauseRequested = false;
            CurrentState = ExecutionState.Paused;
            _actor.Halt();
            _avatar?.SetAnimation("idle");
            return;
        }

        var requiredFacing = FacingDirectionExtensions.DirectionToAdjacent(_actor.Tile, _currentRequest.ChestTile);
        if (requiredFacing.HasValue)
        {
            _actor.Face(requiredFacing.Value);
        }

        // Pre-action re-observation: the chest must still exist
        if (_observer.GetChestAt(_currentRequest.LocationId, _currentRequest.ChestTile) is null)
        {
            FinishExecution(ExecutionState.Failed,
                $"Chest at ({_currentRequest.ChestTile.X}, {_currentRequest.ChestTile.Y}) no longer exists.",
                "CHEST_NOT_FOUND");
            return;
        }

        CurrentState = ExecutionState.Acting;
        _avatar?.SetAnimation("action");
    }

    private void TickActing()
    {
        if (_currentRequest is null) return;

        // Safe boundary between atomic item actions: Pause
        if (_pauseRequested)
        {
            _pauseRequested = false;
            CurrentState = ExecutionState.Paused;
            _actor.Halt();
            _avatar?.SetAnimation("idle");
            return;
        }

        // Safe boundary between atomic item actions: Cancellation
        if (_cancelRequested)
        {
            _cancelRequested = false;
            _actor.Halt();
            _avatar?.SetAnimation("idle");
            var terminalState = _depositedItems.Count > 0 || _merges.Count > 0 || _withdrawnItems.Count > 0
                ? ExecutionState.PartiallySucceeded
                : ExecutionState.Cancelled;
            FinishExecution(terminalState, _cancelReason ?? "Execution cancelled by request.");
            return;
        }

        if (_currentRequest.Kind == ChestActionKind.Organize)
        {
            TickOrganize();
        }
        else if (_currentRequest.Kind == ChestActionKind.Withdraw)
        {
            TickWithdrawNextItem();
        }
        else
        {
            TickDepositNextItem();
        }
    }

    private void TickWithdrawNextItem()
    {
        var withdrawList = _currentRequest?.WithdrawItems;
        if (withdrawList is null || _withdrawPlanIndex >= withdrawList.Count)
        {
            ExecutionState finalState;
            if (_failedItems.Count == 0 && _withdrawnItems.Count > 0)
                finalState = ExecutionState.Succeeded;
            else if (_withdrawnItems.Count > 0)
                finalState = ExecutionState.PartiallySucceeded;
            else
                finalState = ExecutionState.Failed;

            _avatar?.SetAnimation("idle");
            FinishExecution(finalState);
            return;
        }

        var spec = withdrawList[_withdrawPlanIndex];
        var result = _adapter.WithdrawItem(_actor, _currentRequest!.LocationId, _currentRequest.ChestTile, spec.ItemId, spec.Count);

        if (result.Success)
        {
            _withdrawnItems.Add(new WithdrawnItemInfo(
                result.ItemId ?? spec.ItemId,
                result.ItemName ?? spec.ItemId,
                result.Quality,
                result.MovedStack));
            _withdrawPlanIndex++;
            Log($"Withdrew {result.ItemId} x{result.MovedStack} from chest ({_currentRequest.ChestTile.X}, {_currentRequest.ChestTile.Y}).");

            if (result.BackpackFull)
            {
                Log("Companion backpack is full; stopping withdrawal early.");
                ExecutionState endState = _withdrawnItems.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Failed;
                _avatar?.SetAnimation("idle");
                FinishExecution(endState, "Companion backpack is full.", "BACKPACK_FULL");
                return;
            }
            return;
        }

        if (result.PreconditionFailed && result.SkipReason == "backpack-full")
        {
            _skippedItems.Add(new SkippedItemInfo(spec.ItemId, "backpack-full"));
            Log("Companion backpack is full; cannot withdraw.");
            ExecutionState endState = _withdrawnItems.Count > 0 ? ExecutionState.PartiallySucceeded : ExecutionState.Failed;
            _avatar?.SetAnimation("idle");
            FinishExecution(endState, "Companion backpack is full.", "BACKPACK_FULL");
            return;
        }

        if (result.PreconditionFailed && result.SkipReason == "item-not-found")
        {
            _failedItems.Add(new FailedItemInfo(spec.ItemId, "item-not-found"));
            _withdrawPlanIndex++;
            Log($"Item {spec.ItemId} not found in chest ({_currentRequest.ChestTile.X}, {_currentRequest.ChestTile.Y}).");
            return;
        }

        _failedItems.Add(new FailedItemInfo(spec.ItemId, result.ErrorMessage ?? "Unknown withdrawal error"));
        _avatar?.SetAnimation("idle");
        FinishExecution(ExecutionState.Failed,
            result.ErrorMessage ?? $"Withdraw failed for item {spec.ItemId}.",
            "WITHDRAW_FAILED");
    }

    private void TickOrganize()
    {
        var result = _adapter.OrganizeChest(_actor, _currentRequest!.LocationId, _currentRequest.ChestTile);
        _avatar?.SetAnimation("idle");

        if (!result.Success)
        {
            FinishExecution(ExecutionState.Failed,
                result.ErrorMessage ?? "Chest organize failed.",
                result.PreconditionFailed ? "CHEST_NOT_FOUND" : "EXECUTION_FAILED");
            return;
        }

        _merges.AddRange(result.Merges);
        Log($"Chest organize completed at ({_currentRequest!.ChestTile.X}, {_currentRequest.ChestTile.Y}): {_merges.Count} merges.");
        FinishExecution(ExecutionState.Succeeded);
    }

    private void TickDepositNextItem()
    {
        if (_depositPlanIndex >= _depositPlan.Count)
        {
            // All planned items processed
            ExecutionState finalState;
            if (_failedItems.Count == 0 && _depositedItems.Count == _depositPlan.Count)
                finalState = ExecutionState.Succeeded;
            else if (_depositedItems.Count > 0)
                finalState = ExecutionState.PartiallySucceeded;
            else
                finalState = ExecutionState.Failed;

            _avatar?.SetAnimation("idle");
            FinishExecution(finalState);
            return;
        }

        var planItem = _depositPlan[_depositPlanIndex];
        var result = _adapter.DepositItem(_actor, _currentRequest!.LocationId, _currentRequest.ChestTile, planItem.SlotIndex);

        if (result.Success)
        {
            _depositedItems.Add(new DepositedItemInfo(
                planItem.SlotIndex,
                result.ItemId ?? planItem.ItemId,
                result.ItemName ?? planItem.Name,
                result.Quality,
                result.MovedStack));
            _depositPlanIndex++;
            Log($"Deposited {result.ItemId} x{result.MovedStack} into chest ({_currentRequest.ChestTile.X}, {_currentRequest.ChestTile.Y}).");

            if (result.ChestFull)
            {
                // Partial transfer: the chest could not accept the full stack; stop early.
                StopForChestFull();
                return;
            }
            return;
        }

        if (result.PreconditionFailed && result.SkipReason == "chest-full")
        {
            // Nothing was accepted; stop early.
            StopForChestFull();
            return;
        }

        if (result.PreconditionFailed && result.SkipReason == "tool-excluded")
        {
            _skippedItems.Add(new SkippedItemInfo(planItem.ItemId, "tool-excluded"));
            _depositPlanIndex++;
            return;
        }

        if (result.PreconditionFailed && result.ErrorMessage?.Contains("chest", StringComparison.OrdinalIgnoreCase) == true)
        {
            // Chest disappeared or became invalid mid-run
            _avatar?.SetAnimation("idle");
            FinishExecution(ExecutionState.Failed, result.ErrorMessage, "CHEST_NOT_FOUND");
            return;
        }

        // Bounded per-item failure: record and continue with the next item
        Log($"Deposit failed for item {planItem.ItemId} (slot {planItem.SlotIndex}): {result.ErrorMessage ?? "Deposit failed"}", LogLevel.Warn);
        _failedItems.Add(new FailedItemInfo(planItem.ItemId, result.ErrorMessage ?? "Deposit failed"));
        _depositPlanIndex++;
    }

    /// <summary>
    /// Early stop when the chest cannot accept more items: the current and all remaining
    /// planned items are skipped "chest-full" and the result carries details.chestFull=true.
    /// </summary>
    private void StopForChestFull()
    {
        for (int i = _depositPlanIndex; i < _depositPlan.Count; i++)
        {
            var item = _depositPlan[i];
            if (_depositedItems.Any(d => d.SlotIndex == item.SlotIndex)) continue;
            if (_skippedItems.Any(s => s.ItemId == item.ItemId)) continue;
            _skippedItems.Add(new SkippedItemInfo(item.ItemId, "chest-full"));
        }

        _actor.Halt();
        _avatar?.SetAnimation("idle");
        if (_depositedItems.Count > 0)
        {
            FinishExecution(ExecutionState.PartiallySucceeded,
                "Chest is full; deposit stopped early.", "CHEST_FULL",
                retryRecommended: true, chestFull: true);
        }
        else
        {
            FinishExecution(ExecutionState.Failed,
                "Chest is full; nothing could be deposited.", "CHEST_FULL",
                retryRecommended: true, chestFull: true);
        }
    }

    private ChestActionResult FinishExecution(
        ExecutionState state,
        string? errorMessage = null,
        string? errorCode = null,
        bool retryRecommended = false,
        bool chestFull = false)
    {
        CurrentState = state;
        _actor.Halt();
        _actor.SetActiveTask(null);

        // Never emit a failed terminal result without a reason: surface per-item failure causes.
        if (state == ExecutionState.Failed && errorMessage is null && _failedItems.Count > 0)
        {
            var first = _failedItems[0];
            errorMessage = _failedItems.Count == 1
                ? $"Item {first.ItemId} failed: {first.Reason}"
                : $"{_failedItems.Count} items failed; first: {first.ItemId}: {first.Reason}";
            errorCode ??= "EXECUTION_FAILED";
        }

        int gameMinutesElapsed = IWorldObserver.CalculateGameMinutesElapsed(_startClock, _observer.TimeOfDay);
        var kind = _currentRequest?.Kind ?? ChestActionKind.Deposit;

        var result = new ChestActionResult(
            commandId: _currentRequest?.CommandId ?? "cmd-unknown",
            taskId: _currentRequest?.TaskId ?? "task-unknown",
            kind: kind,
            finalState: state,
            chestTile: _currentRequest?.ChestTile ?? new TileCoordinate(0, 0),
            depositedItems: _depositedItems.ToList(),
            skippedItems: _skippedItems.ToList(),
            failedItems: _failedItems.ToList(),
            merges: _merges.ToList(),
            staminaUsed: 0f,
            waterUsed: 0,
            gameMinutesElapsed: gameMinutesElapsed,
            finalWorldRevision: _observer.WorldRevision,
            chestFull: chestFull,
            errorMessage: errorMessage,
            errorCode: errorCode,
            retryRecommended: retryRecommended,
            withdrawnItems: _withdrawnItems.ToList()
        );

        var level = state is ExecutionState.Failed or ExecutionState.Rejected ? LogLevel.Warn : LogLevel.Info;
        Log($"Chest action ({kind}) finished: {state}; deposited={_depositedItems.Count}, withdrawn={_withdrawnItems.Count}, merged={_merges.Count}, skipped={_skippedItems.Count}, failed={_failedItems.Count}" +
            (errorMessage is not null ? $"; error={errorMessage}" : "."), level);

        FinalResult = result;
        OnCompleted?.Invoke(result);

        string verb = kind switch
        {
            ChestActionKind.Deposit => "Deposit",
            ChestActionKind.Withdraw => "Withdraw",
            _ => "Organize"
        };

        if (state == ExecutionState.Succeeded)
        {
            NotifyPlayer(kind switch
            {
                ChestActionKind.Deposit => $"AI Companion: Deposit task completed ({_depositedItems.Count} items).",
                ChestActionKind.Withdraw => $"AI Companion: Withdraw task completed ({_withdrawnItems.Count} items).",
                _ => $"AI Companion: Organize task completed ({_merges.Count} merges)."
            });
        }
        else if (state == ExecutionState.Cancelled)
        {
            NotifyPlayer($"AI Companion: Task cancelled ({_depositedItems.Count + _withdrawnItems.Count} processed).");
        }
        else if (state == ExecutionState.PartiallySucceeded)
        {
            NotifyPlayer(chestFull
                ? $"AI Companion: Chest full ({_depositedItems.Count}/{_depositPlan.Count} deposited)."
                : $"AI Companion: {verb} task partially completed ({_depositedItems.Count + _withdrawnItems.Count} items).");
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
                _replanCount = 0;
                // Re-observe world and re-plan from current position to the chest
                PlanPathToChest();
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
