using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

public sealed record PurchaseItemRequest(
    string ItemId,
    int Count
);

public sealed record PurchaseRequest(
    string CommandId,
    string TaskId,
    string ShopId,
    string LocationId,
    IReadOnlyList<PurchaseItemRequest> Items,
    int BudgetLimit,
    float MaxStamina = 50f,
    int MaxWater = 0,
    int MaxGameMinutes = 60,
    string CancelPolicy = "safe-point",
    string? IdempotencyKey = null,
    long ExpectedWorldRevision = 1
);

public sealed record PurchasedItemInfo(
    string ItemId,
    int Count,
    int UnitPrice,
    int Subtotal
);

public sealed record SkippedPurchaseItemInfo(
    string ItemId,
    int RequestedCount,
    string Reason
);

public sealed record FailedPurchaseItemInfo(
    string ItemId,
    int RequestedCount,
    string Reason
);

public sealed class PurchaseResult
{
    public string CommandId { get; }
    public string TaskId { get; }
    public ExecutionState FinalState { get; }
    public string ShopId { get; }
    public int TotalCost { get; }
    public int BudgetLimit { get; }
    public int RemainingBudget { get; }
    public int? AvailableMoneyAfter { get; }
    public IReadOnlyList<PurchasedItemInfo> PurchasedItems { get; }
    public IReadOnlyList<SkippedPurchaseItemInfo> SkippedItems { get; }
    public IReadOnlyList<FailedPurchaseItemInfo> FailedItems { get; }
    public bool RollbackPerformed { get; }
    public string? RollbackDetails { get; }
    public float StaminaUsed { get; }
    public int WaterUsed { get; }
    public int GameMinutesElapsed { get; }
    public long FinalWorldRevision { get; }
    public string? ErrorMessage { get; }
    public string? ErrorCode { get; }
    public bool RetryRecommended { get; }
    public bool PlayerActionRequired { get; }

    public PurchaseResult(
        string commandId,
        string taskId,
        ExecutionState finalState,
        string shopId,
        int totalCost,
        int budgetLimit,
        int remainingBudget,
        int? availableMoneyAfter,
        IReadOnlyList<PurchasedItemInfo> purchasedItems,
        IReadOnlyList<SkippedPurchaseItemInfo> skippedItems,
        IReadOnlyList<FailedPurchaseItemInfo> failedItems,
        bool rollbackPerformed = false,
        string? rollbackDetails = null,
        float staminaUsed = 0f,
        int waterUsed = 0,
        int gameMinutesElapsed = 0,
        long finalWorldRevision = 1,
        string? errorMessage = null,
        string? errorCode = null,
        bool retryRecommended = false,
        bool playerActionRequired = false)
    {
        CommandId = commandId;
        TaskId = taskId;
        FinalState = finalState;
        ShopId = shopId;
        TotalCost = totalCost;
        BudgetLimit = budgetLimit;
        RemainingBudget = remainingBudget;
        AvailableMoneyAfter = availableMoneyAfter;
        PurchasedItems = purchasedItems;
        SkippedItems = skippedItems;
        FailedItems = failedItems;
        RollbackPerformed = rollbackPerformed;
        RollbackDetails = rollbackDetails;
        StaminaUsed = staminaUsed;
        WaterUsed = waterUsed;
        GameMinutesElapsed = gameMinutesElapsed;
        FinalWorldRevision = finalWorldRevision;
        ErrorMessage = errorMessage;
        ErrorCode = errorCode;
        RetryRecommended = retryRecommended;
        PlayerActionRequired = playerActionRequired;
    }

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
        foreach (var item in PurchasedItems)
        {
            effects.Add(new Dictionary<string, object>
            {
                ["state"] = "purchased",
                ["itemId"] = item.ItemId,
                ["count"] = item.Count,
                ["unitPrice"] = item.UnitPrice,
                ["subtotal"] = item.Subtotal
            });
        }
        foreach (var skipped in SkippedItems)
        {
            effects.Add(new Dictionary<string, object>
            {
                ["state"] = "skipped",
                ["itemId"] = skipped.ItemId,
                ["requestedCount"] = skipped.RequestedCount,
                ["reason"] = skipped.Reason
            });
        }
        foreach (var failed in FailedItems)
        {
            effects.Add(new Dictionary<string, object>
            {
                ["state"] = "failed",
                ["itemId"] = failed.ItemId,
                ["requestedCount"] = failed.RequestedCount,
                ["reason"] = failed.Reason
            });
        }

        SkillResultResources? resources = new(StaminaUsed, WaterUsed, GameMinutesElapsed);
        SkillResultError? error = ErrorMessage is not null
            ? new(ErrorCode ?? "EXECUTION_ERROR", ErrorMessage, null, RetryRecommended)
            : null;

        var purchasedItemList = PurchasedItems.Select(p => new Dictionary<string, object>
        {
            ["itemId"] = p.ItemId,
            ["count"] = p.Count,
            ["unitPrice"] = p.UnitPrice,
            ["subtotal"] = p.Subtotal
        }).ToList();

        var skippedItemList = SkippedItems.Select(s => new Dictionary<string, object>
        {
            ["itemId"] = s.ItemId,
            ["requestedCount"] = s.RequestedCount,
            ["reason"] = s.Reason
        }).ToList();

        string? firstSkipReason = SkippedItems.FirstOrDefault()?.Reason;

        var details = new Dictionary<string, object?>
        {
            ["shopId"] = ShopId,
            ["totalCost"] = TotalCost,
            ["budgetLimit"] = BudgetLimit,
            ["remainingBudget"] = RemainingBudget,
            ["availableMoneyAfter"] = AvailableMoneyAfter,
            ["purchasedItems"] = purchasedItemList,
            ["skippedItems"] = skippedItemList,
            ["skipReason"] = firstSkipReason,
            ["rejectionReason"] = firstSkipReason,
            ["rollbackPerformed"] = RollbackPerformed,
            ["rollbackDetails"] = RollbackDetails
        };

        int completedCount = PurchasedItems.Sum(p => p.Count);

        return new SkillResultPayload(
            CommandId: CommandId,
            TaskId: TaskId,
            TerminalState: terminalState,
            CompletedCount: completedCount,
            SkippedCount: SkippedItems.Count,
            FailedCount: FailedItems.Count,
            FinalWorldRevision: FinalWorldRevision,
            Effects: effects,
            Resources: resources,
            Error: error,
            RetryRecommended: RetryRecommended,
            PlayerActionRequired: PlayerActionRequired,
            SkillId: "purchase-items",
            Details: details!
        );
    }
}
