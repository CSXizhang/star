using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.GameData.Shops;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>
/// Tick-driven state machine executing the "purchase-items" skill.
/// Enforces:
/// 1. Companion is inside the specified shop location and adjacent to the shop counter.
/// 2. Shop open threshold is re-evaluated at the exact moment of execution (no cached open status).
/// 3. Genuine stock lookup and pricing via ShopBuilder; limited-stock items strictly rejected.
/// 4. Explicit single-purchase budget limit and team wallet funds validation.
/// 5. Inventory capacity check: items that do not fit are skipped in order.
/// 6. Authoritative wallet deduction via AddIndividualMoney and genuine item instance delivery.
/// 7. Two-sided balance and backpack delta verification with automatic rollback on mismatch.
/// </summary>
public sealed class PurchaseStateMachine : ISkillExecutionMachine
{
    private readonly IFarmerActor _actor;
    private readonly IWorldObserver _observer;
    private readonly IPurchaseAdapter _adapter;
    private readonly CompanionAvatar? _avatar;
    private readonly Action<string, LogLevel>? _log;

    private readonly object _stateLock = new();

    private PurchaseRequest? _currentRequest;
    private TileCoordinate _adjacentCounterTile;
    private long _elapsedTicks;
    private int _startClock;

    private volatile bool _cancelRequested;
    private volatile string? _cancelReason;
    private volatile bool _pauseRequested;

    public ExecutionState CurrentState { get; private set; } = ExecutionState.Created;
    public bool IsExecuting => CurrentState is ExecutionState.Validating or ExecutionState.Facing
        or ExecutionState.Acting or ExecutionState.Paused or ExecutionState.Cancelling;
    public bool IsPaused => CurrentState == ExecutionState.Paused;
    public PurchaseResult? FinalResult { get; private set; }

    public PurchaseRequest? CurrentRequest => _currentRequest;
    public string? ActiveTaskId => _currentRequest?.TaskId;
    public int TotalTargets => _currentRequest?.Items.Count ?? 0;
    public int CurrentTargetIndex => 0;

    public event Action<PurchaseResult>? OnCompleted;
    public event Action<string>? OnNotification;

    public PurchaseStateMachine(
        IFarmerActor actor,
        IWorldObserver observer,
        IPurchaseAdapter adapter,
        CompanionAvatar? avatar = null,
        Action<string, LogLevel>? log = null)
    {
        _actor = actor ?? throw new ArgumentNullException(nameof(actor));
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _adapter = adapter ?? throw new ArgumentNullException(nameof(adapter));
        _avatar = avatar;
        _log = log;
    }

    private void Log(string message, LogLevel level = LogLevel.Info) => _log?.Invoke(message, level);
    private void NotifyPlayer(string message) => OnNotification?.Invoke(message);

    public bool Start(PurchaseRequest request, out PurchaseResult? earlyTerminalResult)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_stateLock)
        {
            _currentRequest = request;
            _startClock = _observer.TimeOfDay;
            _elapsedTicks = 0;
            _cancelRequested = false;
            _cancelReason = null;
            _pauseRequested = false;

            // 1. Validate request parameters
            if (request.Items == null || request.Items.Count == 0)
            {
                earlyTerminalResult = BuildResult(ExecutionState.Rejected,
                    errorMessage: "Items list is empty.",
                    errorCode: "EMPTY_ITEMS");
                CurrentState = ExecutionState.Rejected;
                FinalResult = earlyTerminalResult;
                return false;
            }

            if (request.BudgetLimit <= 0)
            {
                earlyTerminalResult = BuildResult(ExecutionState.Rejected,
                    errorMessage: "budget_limit must be a positive integer.",
                    errorCode: "INVALID_BUDGET");
                CurrentState = ExecutionState.Rejected;
                FinalResult = earlyTerminalResult;
                return false;
            }

            foreach (var item in request.Items)
            {
                if (string.IsNullOrWhiteSpace(item.ItemId) || item.Count <= 0)
                {
                    earlyTerminalResult = BuildResult(ExecutionState.Rejected,
                        errorMessage: $"Invalid item specification: '{item.ItemId}' x {item.Count}.",
                        errorCode: "INVALID_ITEM");
                    CurrentState = ExecutionState.Rejected;
                    FinalResult = earlyTerminalResult;
                    return false;
                }
            }

            if (string.IsNullOrWhiteSpace(request.LocationId))
            {
                earlyTerminalResult = BuildResult(ExecutionState.Rejected,
                    errorMessage: "locationId is required.",
                    errorCode: "MISSING_LOCATION");
                CurrentState = ExecutionState.Rejected;
                FinalResult = earlyTerminalResult;
                return false;
            }

            // 2. Validate companion current location
            if (!string.Equals(_actor.LocationName, request.LocationId, StringComparison.OrdinalIgnoreCase))
            {
                earlyTerminalResult = BuildResult(ExecutionState.Rejected,
                    errorMessage: $"Companion is at {_actor.LocationName}, expected {request.LocationId}.",
                    errorCode: "LOCATION_MISMATCH");
                CurrentState = ExecutionState.Rejected;
                FinalResult = earlyTerminalResult;
                return false;
            }

            // 3. Validate counter adjacency
            var counterTiles = _adapter.GetShopCounterTiles(request.ShopId, request.LocationId);
            if (counterTiles.Count == 0)
            {
                earlyTerminalResult = BuildResult(ExecutionState.Rejected,
                    errorMessage: $"No shop counter for '{request.ShopId}' found in {request.LocationId}.",
                    errorCode: "COUNTER_NOT_FOUND");
                CurrentState = ExecutionState.Rejected;
                FinalResult = earlyTerminalResult;
                return false;
            }

            var adjacentCounter = counterTiles.FirstOrDefault(t => _actor.Tile.IsAdjacentTo(t));
            if (adjacentCounter == default)
            {
                earlyTerminalResult = BuildResult(ExecutionState.Rejected,
                    errorMessage: $"Companion at {_actor.Tile} is not adjacent to shop counter for '{request.ShopId}'.",
                    errorCode: "NOT_ADJACENT_TO_COUNTER");
                CurrentState = ExecutionState.Rejected;
                FinalResult = earlyTerminalResult;
                return false;
            }

            _adjacentCounterTile = adjacentCounter;
            CurrentState = ExecutionState.Facing;
            earlyTerminalResult = null;
            return true;
        }
    }

    public void Update(GameTime? time, long tickCount)
    {
        lock (_stateLock)
        {
            if (!IsExecuting || IsPaused) return;

            _elapsedTicks++;

            if (_pauseRequested && CurrentState != ExecutionState.Paused)
            {
                _pauseRequested = false;
                CurrentState = ExecutionState.Paused;
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                NotifyPlayer("AI Companion: Purchase task paused.");
                return;
            }

            if (_cancelRequested)
            {
                FinishExecution(ExecutionState.Cancelled, _cancelReason ?? "Purchase cancelled by user.", "CANCELLED");
                return;
            }

            if (CurrentState == ExecutionState.Facing)
            {
                var dir = FacingDirectionExtensions.DirectionToAdjacent(_actor.Tile, _adjacentCounterTile);
                if (dir.HasValue)
                {
                    _actor.Face(dir.Value);
                }
                CurrentState = ExecutionState.Acting;
                return;
            }

            if (CurrentState == ExecutionState.Acting)
            {
                ExecutePurchaseTransaction();
            }
        }
    }

    private void ExecutePurchaseTransaction()
    {
        if (_currentRequest == null) return;

        // 1. Re-evaluate shop opening threshold at this exact moment (do not cache)
        var shopScan = _adapter.ReevaluateShop(_currentRequest.ShopId, _actor);
        if (!shopScan.IsOpen)
        {
            var skipped = _currentRequest.Items
                .Select(i => new SkippedPurchaseItemInfo(i.ItemId, i.Count, "shop-closed"))
                .ToList();
            FinishExecution(ExecutionState.Rejected,
                shopScan.ClosedMessage ?? $"Shop '{_currentRequest.ShopId}' is closed.",
                "SHOP_CLOSED",
                skippedItems: skipped);
            return;
        }

        // 2. Retrieve dynamic shop stock
        var stock = _adapter.GetRawShopStock(_currentRequest.ShopId, _actor);
        if (stock == null)
        {
            var skipped = _currentRequest.Items
                .Select(i => new SkippedPurchaseItemInfo(i.ItemId, i.Count, "shop-stock-unavailable"))
                .ToList();
            FinishExecution(ExecutionState.Failed,
                $"Failed to retrieve stock for shop '{_currentRequest.ShopId}'.",
                "STOCK_UNAVAILABLE",
                skippedItems: skipped);
            return;
        }

        // Helper to match salable in stock
        (ISalable? Salable, ItemStockInformation? Info) FindSalable(string itemId)
        {
            foreach (var (s, info) in stock)
            {
                if (s == null || info == null) continue;
                string qid = s.QualifiedItemId;
                string rawId = s is Item itm ? itm.ItemId : (qid.StartsWith("(") && qid.IndexOf(')') > 0 ? qid.Substring(qid.IndexOf(')') + 1) : qid);
                if (string.Equals(qid, itemId, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(rawId, itemId, StringComparison.OrdinalIgnoreCase))
                {
                    return (s, info);
                }
            }
            return (null, null);
        }

        // 3. Evaluate requested items against stock, limited stock mode, and inventory space
        var candidates = new List<(ISalable Salable, ItemStockInformation Info, int Count, int UnitPrice)>();
        var skippedItems = new List<SkippedPurchaseItemInfo>();

        int freeSlots = _actor.FreeInventorySlots;
        var currentSnapshot = _actor.GetInventorySnapshot();
        var remainingStackCapacity = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        foreach (var slot in currentSnapshot)
        {
            int maxStack = 999;
            int availInSlot = maxStack - slot.Stack;
            if (availInSlot > 0)
            {
                remainingStackCapacity.TryGetValue(slot.ItemId, out int cur);
                remainingStackCapacity[slot.ItemId] = cur + availInSlot;
            }
        }

        foreach (var reqItem in _currentRequest.Items)
        {
            var (salable, stockInfo) = FindSalable(reqItem.ItemId);
            if (salable == null || stockInfo == null)
            {
                skippedItems.Add(new SkippedPurchaseItemInfo(reqItem.ItemId, reqItem.Count, "item-not-in-stock"));
                continue;
            }

            // Check effective finiteness: infinite stock items (Stock == int.MaxValue || IsInfiniteStock())
            // do not require stock bookkeeping and are supported regardless of LimitedStockMode or SyncedKey.
            // Truly finite stock items are rejected as limited-stock-unsupported in this batch.
            bool isInfinite = stockInfo.Stock == int.MaxValue || salable.IsInfiniteStock();
            if (!isInfinite)
            {
                skippedItems.Add(new SkippedPurchaseItemInfo(reqItem.ItemId, reqItem.Count, "limited-stock-unsupported"));
                continue;
            }

            // Check inventory capacity
            int countToPlace = reqItem.Count;
            string qId = salable.QualifiedItemId;
            if (remainingStackCapacity.TryGetValue(qId, out int stackCap) && stackCap > 0)
            {
                int stacked = Math.Min(stackCap, countToPlace);
                countToPlace -= stacked;
                remainingStackCapacity[qId] = stackCap - stacked;
            }

            if (countToPlace > 0)
            {
                if (freeSlots <= 0)
                {
                    skippedItems.Add(new SkippedPurchaseItemInfo(reqItem.ItemId, reqItem.Count, "inventory-full"));
                    continue;
                }
                freeSlots--;
            }

            candidates.Add((salable, stockInfo, reqItem.Count, stockInfo.Price));
        }

        if (candidates.Count == 0)
        {
            FinishExecution(ExecutionState.Rejected,
                "None of the requested items could be purchased.",
                "NO_PURCHASEABLE_ITEMS",
                skippedItems: skippedItems);
            return;
        }

        // 4. Budget limit and team wallet funds check
        int totalCost = candidates.Sum(c => c.UnitPrice * c.Count);
        if (totalCost > _currentRequest.BudgetLimit)
        {
            var allSkipped = _currentRequest.Items
                .Select(i => new SkippedPurchaseItemInfo(i.ItemId, i.Count, "budget-exceeded"))
                .ToList();
            FinishExecution(ExecutionState.Rejected,
                $"Total purchase cost {totalCost}g exceeds budget limit {_currentRequest.BudgetLimit}g.",
                "BUDGET_EXCEEDED",
                skippedItems: allSkipped);
            return;
        }

        int? availableMoney = _adapter.GetCompanionMoney(_actor);
        if (availableMoney.HasValue && totalCost > availableMoney.Value)
        {
            var allSkipped = _currentRequest.Items
                .Select(i => new SkippedPurchaseItemInfo(i.ItemId, i.Count, "insufficient-funds"))
                .ToList();
            FinishExecution(ExecutionState.Rejected,
                $"Total purchase cost {totalCost}g exceeds available funds {availableMoney.Value}g.",
                "INSUFFICIENT_FUNDS",
                skippedItems: allSkipped);
            return;
        }

        // 5. Pre-transaction snapshot for verification
        int moneyBefore = availableMoney ?? 0;
        var initialCounts = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        foreach (var c in candidates)
        {
            string qId = c.Salable.QualifiedItemId;
            if (!initialCounts.ContainsKey(qId))
            {
                initialCounts[qId] = _adapter.CountItemInBackpack(_actor, qId);
            }
        }

        // 6. Native wallet deduction
        if (!_adapter.TryDeductCompanionMoney(_actor, totalCost, out int moneyAfter))
        {
            FinishExecution(ExecutionState.Failed,
                "Failed to deduct funds from wallet.",
                "DEDUCTION_FAILED",
                skippedItems: skippedItems);
            return;
        }

        // 7. Deliver genuine items
        var purchasedInfos = new List<PurchasedItemInfo>();
        var addedItemsForRollback = new List<(string ItemId, int Count)>();
        bool deliveryFailed = false;

        foreach (var (salable, _, count, unitPrice) in candidates)
        {
            var instance = salable.GetSalableInstance();
            if (instance is Item gameItem)
            {
                gameItem.Stack = count;
                bool added = _actor.TryAddItemToInventory(gameItem);
                if (added)
                {
                    purchasedInfos.Add(new PurchasedItemInfo(salable.QualifiedItemId, count, unitPrice, unitPrice * count));
                    addedItemsForRollback.Add((salable.QualifiedItemId, count));
                }
                else
                {
                    deliveryFailed = true;
                    break;
                }
            }
            else
            {
                string qid = salable.QualifiedItemId;
                string name = !string.IsNullOrEmpty(salable.DisplayName) ? salable.DisplayName : (salable.Name ?? "");
                bool added = _actor.TryAddItemToInventory(new InventoryItem(qid, name, stack: count));
                if (added)
                {
                    purchasedInfos.Add(new PurchasedItemInfo(qid, count, unitPrice, unitPrice * count));
                    addedItemsForRollback.Add((qid, count));
                }
                else
                {
                    deliveryFailed = true;
                    break;
                }
            }
        }

        // 8. Post-transaction verification
        int moneyDeducted = moneyBefore - moneyAfter;
        bool moneyMatches = moneyDeducted == totalCost;

        bool inventoryMatches = true;
        foreach (var (salable, _, expectedCount, _) in candidates)
        {
            string qId = salable.QualifiedItemId;
            int before = initialCounts[qId];
            int after = _adapter.CountItemInBackpack(_actor, qId);
            if (after - before != expectedCount)
            {
                inventoryMatches = false;
                break;
            }
        }

        if (deliveryFailed || !moneyMatches || !inventoryMatches)
        {
            // Discrepancy detected: execute automatic rollback
            _adapter.RefundCompanionMoney(_actor, totalCost);
            foreach (var (rollbackId, rollbackCount) in addedItemsForRollback)
            {
                _actor.TryConsumeItem(rollbackId, rollbackCount);
            }

            string rollbackDetail = $"Transaction discrepancy detected (deliveryFailed={deliveryFailed}, moneyReduced={moneyDeducted}/{totalCost}, inventoryVerified={inventoryMatches}). Refunded {totalCost}g and reverted items.";
            Log(rollbackDetail, LogLevel.Error);

            FinishExecution(ExecutionState.Failed,
                "Purchase transaction failed post-condition check. Rollback executed.",
                "TRANSACTION_FAILED",
                purchasedItems: new List<PurchasedItemInfo>(),
                skippedItems: skippedItems,
                totalCost: 0,
                availableMoneyAfter: moneyBefore,
                rollbackPerformed: true,
                rollbackDetails: rollbackDetail);
            return;
        }

        // 9. Successful completion
        ExecutionState finalState = skippedItems.Count > 0
            ? ExecutionState.PartiallySucceeded
            : ExecutionState.Succeeded;

        FinishExecution(finalState,
            errorMessage: null,
            errorCode: null,
            purchasedItems: purchasedInfos,
            skippedItems: skippedItems,
            totalCost: totalCost,
            availableMoneyAfter: moneyAfter,
            rollbackPerformed: false,
            rollbackDetails: null);
    }

    private void FinishExecution(
        ExecutionState finalState,
        string? errorMessage = null,
        string? errorCode = null,
        IReadOnlyList<PurchasedItemInfo>? purchasedItems = null,
        IReadOnlyList<SkippedPurchaseItemInfo>? skippedItems = null,
        int totalCost = 0,
        int? availableMoneyAfter = null,
        bool rollbackPerformed = false,
        string? rollbackDetails = null)
    {
        CurrentState = finalState;
        FinalResult = BuildResult(
            finalState,
            errorMessage,
            errorCode,
            purchasedItems,
            skippedItems,
            totalCost,
            availableMoneyAfter,
            rollbackPerformed,
            rollbackDetails
        );

        Log($"purchase-items finished with state {finalState} (totalCost={totalCost}g).");
        OnCompleted?.Invoke(FinalResult);
    }

    private PurchaseResult BuildResult(
        ExecutionState finalState,
        string? errorMessage = null,
        string? errorCode = null,
        IReadOnlyList<PurchasedItemInfo>? purchasedItems = null,
        IReadOnlyList<SkippedPurchaseItemInfo>? skippedItems = null,
        int totalCost = 0,
        int? availableMoneyAfter = null,
        bool rollbackPerformed = false,
        string? rollbackDetails = null)
    {
        int minutesElapsed = IWorldObserver.CalculateGameMinutesElapsed(_startClock, _observer.TimeOfDay);
        int budget = _currentRequest?.BudgetLimit ?? 0;
        int remainingBudget = budget - totalCost;

        return new PurchaseResult(
            commandId: _currentRequest?.CommandId ?? "unknown",
            taskId: _currentRequest?.TaskId ?? "unknown",
            finalState: finalState,
            shopId: _currentRequest?.ShopId ?? "SeedShop",
            totalCost: totalCost,
            budgetLimit: budget,
            remainingBudget: remainingBudget,
            availableMoneyAfter: availableMoneyAfter,
            purchasedItems: purchasedItems ?? Array.Empty<PurchasedItemInfo>(),
            skippedItems: skippedItems ?? Array.Empty<SkippedPurchaseItemInfo>(),
            failedItems: Array.Empty<FailedPurchaseItemInfo>(),
            rollbackPerformed: rollbackPerformed,
            rollbackDetails: rollbackDetails,
            staminaUsed: 0f,
            waterUsed: 0,
            gameMinutesElapsed: minutesElapsed,
            finalWorldRevision: _currentRequest?.ExpectedWorldRevision ?? 1,
            errorMessage: errorMessage,
            errorCode: errorCode
        );
    }

    public void RequestCancel(string reason)
    {
        lock (_stateLock)
        {
            if (!IsExecuting) return;
            _cancelRequested = true;
            _cancelReason = reason;
        }
    }

    public void RequestPause()
    {
        lock (_stateLock)
        {
            if (CurrentState is ExecutionState.Acting or ExecutionState.Facing)
            {
                CurrentState = ExecutionState.Paused;
            }
        }
    }

    public void Resume()
    {
        lock (_stateLock)
        {
            if (CurrentState == ExecutionState.Paused)
            {
                CurrentState = ExecutionState.Acting;
            }
        }
    }
}
