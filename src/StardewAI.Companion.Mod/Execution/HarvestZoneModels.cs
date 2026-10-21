using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

public sealed record HarvestZoneRequest(
    string CommandId,
    string TaskId,
    string LocationId,
    IReadOnlyList<TileCoordinate> TargetTiles,
    float MaxStamina,
    int MaxWater,
    int MaxGameMinutes,
    string CancelPolicy = "safe-point",
    string? IdempotencyKey = null,
    long ExpectedWorldRevision = 1
);

public sealed record HarvestedTileInfo(
    TileCoordinate Tile,
    string? CropId,
    string? ItemId,
    string? ItemName,
    int Stack,
    int Quality
)
{
    public string HarvestMethod { get; init; } = "hand";
}

public sealed class HarvestZoneResult
{
    public string CommandId { get; }
    public string TaskId { get; }
    public ExecutionState FinalState { get; }
    public IReadOnlyList<HarvestedTileInfo> HarvestedTiles { get; }
    public IReadOnlyList<SkippedTileInfo> SkippedTiles { get; }
    public IReadOnlyList<FailedTileInfo> FailedTiles { get; }
    public float StaminaUsed { get; }
    public int WaterUsed { get; }
    public int GameMinutesElapsed { get; }
    public long FinalWorldRevision { get; }
    public bool InventoryFull { get; }
    public string? ErrorMessage { get; }
    public string? ErrorCode { get; }
    public bool RetryRecommended { get; }
    public bool PlayerActionRequired { get; }
    public IReadOnlyList<Dictionary<string, object>>? InventoryDelta { get; }
    public bool NativeScytheUsed { get; }

    public HarvestZoneResult(
        string commandId,
        string taskId,
        ExecutionState finalState,
        IReadOnlyList<HarvestedTileInfo> harvestedTiles,
        IReadOnlyList<SkippedTileInfo> skippedTiles,
        IReadOnlyList<FailedTileInfo> failedTiles,
        float staminaUsed,
        int waterUsed,
        int gameMinutesElapsed,
        long finalWorldRevision,
        bool inventoryFull = false,
        string? errorMessage = null,
        string? errorCode = null,
        bool retryRecommended = false,
        bool playerActionRequired = false,
        IReadOnlyList<Dictionary<string, object>>? inventoryDelta = null,
        bool nativeScytheUsed = false)
    {
        CommandId = commandId;
        TaskId = taskId;
        FinalState = finalState;
        HarvestedTiles = harvestedTiles;
        SkippedTiles = skippedTiles;
        FailedTiles = failedTiles;
        StaminaUsed = staminaUsed;
        WaterUsed = waterUsed;
        GameMinutesElapsed = gameMinutesElapsed;
        FinalWorldRevision = finalWorldRevision;
        InventoryFull = inventoryFull;
        ErrorMessage = errorMessage;
        ErrorCode = errorCode;
        RetryRecommended = retryRecommended;
        PlayerActionRequired = playerActionRequired;
        InventoryDelta = inventoryDelta;
        NativeScytheUsed = nativeScytheUsed;
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

        var effects = new List<Dictionary<string, object>>();
        foreach (var harvested in HarvestedTiles)
        {
            effects.Add(new Dictionary<string, object>
            {
                ["tile"] = new Dictionary<string, object> { ["x"] = harvested.Tile.X, ["y"] = harvested.Tile.Y },
                ["state"] = "harvested",
                ["cropId"] = harvested.CropId ?? "",
                ["itemId"] = harvested.ItemId ?? "",
                ["itemName"] = harvested.ItemName ?? "",
                ["stack"] = harvested.Stack,
                ["quality"] = harvested.Quality,
                ["harvestMethod"] = harvested.HarvestMethod
            });
        }
        foreach (var skipped in SkippedTiles)
        {
            effects.Add(new Dictionary<string, object>
            {
                ["tile"] = new Dictionary<string, object> { ["x"] = skipped.Tile.X, ["y"] = skipped.Tile.Y },
                ["state"] = "skipped",
                ["reason"] = skipped.Reason
            });
        }
        foreach (var failed in FailedTiles)
        {
            effects.Add(new Dictionary<string, object>
            {
                ["tile"] = new Dictionary<string, object> { ["x"] = failed.Tile.X, ["y"] = failed.Tile.Y },
                ["state"] = "failed",
                ["reason"] = failed.Reason
            });
        }

        SkillResultResources? resources = new(StaminaUsed, WaterUsed, GameMinutesElapsed);
        SkillResultError? error = ErrorMessage is not null
            ? new(ErrorCode ?? "EXECUTION_ERROR", ErrorMessage, null, RetryRecommended)
            : null;
        Dictionary<string, object>? details = InventoryFull || InventoryDelta is not null
            ? new Dictionary<string, object>() : null;
        if (InventoryFull) details!["inventoryFull"] = true;
        if (InventoryDelta is not null)
        {
            details!["inventoryDelta"] = InventoryDelta;
            details["inventoryDeltaObserved"] = true;
            details["inventoryDeltaEntryCount"] = InventoryDelta.Count;
            details["inventoryDeltaTiming"] = "command-completion; later native proximity collection belongs to subsequent snapshots";
            details["inventoryDeltaScope"] = "bound-native-player-command-interval";
            details["nativeScytheUsed"] = NativeScytheUsed;
            if (NativeScytheUsed)
                details["quantityNote"] = "Scythe tile effects confirm harvested crops, not backpack quantity. inventoryDelta is the measured whole-job backpack change; native drops may still be on the ground.";
        }

        return new SkillResultPayload(
            CommandId: CommandId,
            TaskId: TaskId,
            TerminalState: terminalState,
            CompletedCount: HarvestedTiles.Count,
            SkippedCount: SkippedTiles.Count,
            FailedCount: FailedTiles.Count,
            FinalWorldRevision: FinalWorldRevision,
            Effects: effects,
            Resources: resources,
            Error: error,
            RetryRecommended: RetryRecommended,
            PlayerActionRequired: PlayerActionRequired,
            SkillId: "harvest-zone",
            Details: details
        );
    }
}
