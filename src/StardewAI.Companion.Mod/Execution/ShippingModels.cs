using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

public sealed record ShippingRequest(
    string CommandId,
    string TaskId,
    string LocationId,
    IReadOnlyList<ShippingItemRequest> Items,
    float MaxStamina = 50f,
    int MaxWater = 0,
    int MaxGameMinutes = 60,
    string CancelPolicy = "safe-point",
    string? IdempotencyKey = null,
    long ExpectedWorldRevision = 1
);

public sealed record ShippedItemInfo(
    string ItemId,
    int Count,
    int EstimatedUnitValue,
    int EstimatedTotalValue,
    int RemainingBackpackCount
);

public sealed record SkippedShippingItemInfo(
    string ItemId,
    int RequestedCount,
    string Reason
);

public sealed record FailedShippingItemInfo(
    string ItemId,
    int RequestedCount,
    string Reason
);

public sealed class ShippingResult
{
    public string CommandId { get; }
    public string TaskId { get; }
    public ExecutionState FinalState { get; }
    public IReadOnlyList<ShippedItemInfo> ShippedItems { get; }
    public IReadOnlyList<SkippedShippingItemInfo> SkippedItems { get; }
    public IReadOnlyList<FailedShippingItemInfo> FailedItems { get; }
    public int EstimatedTotalValue { get; }
    public int ShippingBinTotalCount { get; }
    public IReadOnlyDictionary<string, int> RemainingBackpackCounts { get; }
    public float StaminaUsed { get; }
    public int WaterUsed { get; }
    public int GameMinutesElapsed { get; }
    public long FinalWorldRevision { get; }
    public string? ErrorMessage { get; }
    public string? ErrorCode { get; }
    public bool RetryRecommended { get; }
    public bool PlayerActionRequired { get; }

    public ShippingResult(
        string commandId,
        string taskId,
        ExecutionState finalState,
        IReadOnlyList<ShippedItemInfo> shippedItems,
        IReadOnlyList<SkippedShippingItemInfo> skippedItems,
        IReadOnlyList<FailedShippingItemInfo> failedItems,
        int estimatedTotalValue,
        int shippingBinTotalCount,
        IReadOnlyDictionary<string, int> remainingBackpackCounts,
        float staminaUsed,
        int waterUsed,
        int gameMinutesElapsed,
        long finalWorldRevision,
        string? errorMessage = null,
        string? errorCode = null,
        bool retryRecommended = false,
        bool playerActionRequired = false)
    {
        CommandId = commandId;
        TaskId = taskId;
        FinalState = finalState;
        ShippedItems = shippedItems;
        SkippedItems = skippedItems;
        FailedItems = failedItems;
        EstimatedTotalValue = estimatedTotalValue;
        ShippingBinTotalCount = shippingBinTotalCount;
        RemainingBackpackCounts = remainingBackpackCounts;
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
        foreach (var item in ShippedItems)
        {
            effects.Add(new Dictionary<string, object>
            {
                ["state"] = "shipped",
                ["itemId"] = item.ItemId,
                ["count"] = item.Count,
                ["estimatedUnitValue"] = item.EstimatedUnitValue,
                ["estimatedTotalValue"] = item.EstimatedTotalValue,
                ["remainingBackpackCount"] = item.RemainingBackpackCount
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

        var shippedItemList = ShippedItems.Select(s => new Dictionary<string, object>
        {
            ["itemId"] = s.ItemId,
            ["count"] = s.Count,
            ["estimatedUnitValue"] = s.EstimatedUnitValue,
            ["estimatedTotalValue"] = s.EstimatedTotalValue,
            ["remainingBackpackCount"] = s.RemainingBackpackCount
        }).ToList();

        var details = new Dictionary<string, object>
        {
            ["shippedItems"] = shippedItemList,
            ["estimatedTotalValue"] = EstimatedTotalValue,
            ["estimatedValue"] = EstimatedTotalValue,
            ["shippingBinTotalCount"] = ShippingBinTotalCount,
            ["remainingBackpackCounts"] = RemainingBackpackCounts.ToDictionary(k => k.Key, v => (object)v.Value),
            ["note"] = "Estimated value only; funds will be settled overnight by the game engine."
        };

        int completedCount = ShippedItems.Sum(s => s.Count);

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
            SkillId: "ship-items",
            Details: details
        );
    }
}
