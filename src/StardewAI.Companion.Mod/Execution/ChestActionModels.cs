using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

public enum ChestActionKind
{
    Deposit,
    Organize,
    Withdraw
}

public sealed record WithdrawItemSpec(
    string ItemId,
    int Count
);

public sealed record ChestActionRequest(
    string CommandId,
    string TaskId,
    ChestActionKind Kind,
    string LocationId,
    TileCoordinate ChestTile,
    IReadOnlyList<string>? ItemIds = null,
    IReadOnlyList<WithdrawItemSpec>? WithdrawItems = null,
    float MaxStamina = 270f,
    int MaxWater = 40,
    int MaxGameMinutes = 60,
    string CancelPolicy = "safe-point",
    string? IdempotencyKey = null,
    long ExpectedWorldRevision = 1
)
{
    public string SkillId => Kind switch
    {
        ChestActionKind.Deposit => "deposit-chest",
        ChestActionKind.Organize => "organize-chest",
        ChestActionKind.Withdraw => "withdraw-chest",
        _ => "unknown"
    };
}

public sealed record DepositedItemInfo(
    int SlotIndex,
    string ItemId,
    string ItemName,
    int Quality,
    int Stack
);

public sealed record WithdrawnItemInfo(
    string ItemId,
    string ItemName,
    int Quality,
    int Stack
);

public sealed record SkippedItemInfo(string ItemId, string Reason);
public sealed record FailedItemInfo(string ItemId, string Reason);

public sealed class ChestActionResult
{
    public string CommandId { get; }
    public string TaskId { get; }
    public ChestActionKind Kind { get; }
    public ExecutionState FinalState { get; }
    public TileCoordinate ChestTile { get; }
    public IReadOnlyList<DepositedItemInfo> DepositedItems { get; }
    public IReadOnlyList<WithdrawnItemInfo> WithdrawnItems { get; }
    public IReadOnlyList<SkippedItemInfo> SkippedItems { get; }
    public IReadOnlyList<FailedItemInfo> FailedItems { get; }
    public IReadOnlyList<ChestMergeInfo> Merges { get; }
    public float StaminaUsed { get; }
    public int WaterUsed { get; }
    public int GameMinutesElapsed { get; }
    public long FinalWorldRevision { get; }
    public bool ChestFull { get; }
    public string? ErrorMessage { get; }
    public string? ErrorCode { get; }
    public bool RetryRecommended { get; }
    public bool PlayerActionRequired { get; }

    public string SkillId => Kind switch
    {
        ChestActionKind.Deposit => "deposit-chest",
        ChestActionKind.Organize => "organize-chest",
        ChestActionKind.Withdraw => "withdraw-chest",
        _ => "unknown"
    };

    public ChestActionResult(
        string commandId,
        string taskId,
        ChestActionKind kind,
        ExecutionState finalState,
        TileCoordinate chestTile,
        IReadOnlyList<DepositedItemInfo> depositedItems,
        IReadOnlyList<SkippedItemInfo> skippedItems,
        IReadOnlyList<FailedItemInfo> failedItems,
        IReadOnlyList<ChestMergeInfo> merges,
        float staminaUsed,
        int waterUsed,
        int gameMinutesElapsed,
        long finalWorldRevision,
        bool chestFull = false,
        string? errorMessage = null,
        string? errorCode = null,
        bool retryRecommended = false,
        bool playerActionRequired = false,
        IReadOnlyList<WithdrawnItemInfo>? withdrawnItems = null)
    {
        CommandId = commandId;
        TaskId = taskId;
        Kind = kind;
        FinalState = finalState;
        ChestTile = chestTile;
        DepositedItems = depositedItems;
        WithdrawnItems = withdrawnItems ?? Array.Empty<WithdrawnItemInfo>();
        SkippedItems = skippedItems;
        FailedItems = failedItems;
        Merges = merges;
        StaminaUsed = staminaUsed;
        WaterUsed = waterUsed;
        GameMinutesElapsed = gameMinutesElapsed;
        FinalWorldRevision = finalWorldRevision;
        ChestFull = chestFull;
        ErrorMessage = errorMessage;
        ErrorCode = errorCode;
        RetryRecommended = retryRecommended;
        PlayerActionRequired = playerActionRequired;
    }

    /// <summary>
    /// Converts this result into the canonical protocol SkillResultPayload DTO.
    /// </summary>
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

        var chestTileDict = new Dictionary<string, object> { ["x"] = ChestTile.X, ["y"] = ChestTile.Y };
        var effects = new List<Dictionary<string, object>>();

        if (Kind == ChestActionKind.Deposit)
        {
            foreach (var deposited in DepositedItems)
            {
                effects.Add(new Dictionary<string, object>
                {
                    ["state"] = "deposited",
                    ["itemId"] = deposited.ItemId,
                    ["itemName"] = deposited.ItemName,
                    ["stack"] = deposited.Stack,
                    ["quality"] = deposited.Quality,
                    ["chestTile"] = chestTileDict
                });
            }
            foreach (var skipped in SkippedItems)
            {
                effects.Add(new Dictionary<string, object>
                {
                    ["state"] = "skipped",
                    ["itemId"] = skipped.ItemId,
                    ["reason"] = skipped.Reason
                });
            }
        }
        else if (Kind == ChestActionKind.Withdraw)
        {
            foreach (var withdrawn in WithdrawnItems)
            {
                effects.Add(new Dictionary<string, object>
                {
                    ["state"] = "withdrawn",
                    ["itemId"] = withdrawn.ItemId,
                    ["itemName"] = withdrawn.ItemName,
                    ["stack"] = withdrawn.Stack,
                    ["quality"] = withdrawn.Quality,
                    ["chestTile"] = chestTileDict
                });
            }
            foreach (var skipped in SkippedItems)
            {
                effects.Add(new Dictionary<string, object>
                {
                    ["state"] = "skipped",
                    ["itemId"] = skipped.ItemId,
                    ["reason"] = skipped.Reason
                });
            }
        }
        else
        {
            foreach (var merge in Merges)
            {
                effects.Add(new Dictionary<string, object>
                {
                    ["state"] = "merged",
                    ["itemId"] = merge.ItemId,
                    ["itemName"] = merge.ItemName,
                    ["quality"] = merge.Quality,
                    ["mergedStack"] = merge.MergedStack,
                    ["chestTile"] = chestTileDict
                });
            }
        }

        int completedCount = Kind switch
        {
            ChestActionKind.Deposit => DepositedItems.Count,
            ChestActionKind.Organize => Merges.Count,
            ChestActionKind.Withdraw => WithdrawnItems.Count,
            _ => 0
        };

        SkillResultResources? resources = new(StaminaUsed, WaterUsed, GameMinutesElapsed);
        SkillResultError? error = ErrorMessage is not null
            ? new(ErrorCode ?? "EXECUTION_ERROR", ErrorMessage, null, RetryRecommended)
            : null;
        Dictionary<string, object>? details = ChestFull
            ? new Dictionary<string, object> { ["chestFull"] = true }
            : null;

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
            SkillId: SkillId,
            Details: details
        );
    }
}
