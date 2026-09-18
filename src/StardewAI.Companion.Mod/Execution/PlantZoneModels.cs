using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

public sealed record PlantZoneRequest(
    string CommandId,
    string TaskId,
    string LocationId,
    string SeedItemId,
    IReadOnlyList<TileCoordinate> TargetTiles,
    float MaxStamina = 50f,
    int MaxWater = 0,
    int MaxGameMinutes = 60,
    string CancelPolicy = "safe-point",
    string? IdempotencyKey = null,
    long ExpectedWorldRevision = 1
);

public sealed record PlantedTileInfo(
    TileCoordinate Tile,
    string SeedItemId,
    int RemainingStack
);

public sealed class PlantZoneResult
{
    public string CommandId { get; }
    public string TaskId { get; }
    public string SeedItemId { get; }
    public ExecutionState FinalState { get; }
    public IReadOnlyList<PlantedTileInfo> PlantedTiles { get; }
    public IReadOnlyList<SkippedTileInfo> SkippedTiles { get; }
    public IReadOnlyList<FailedTileInfo> FailedTiles { get; }
    public float StaminaUsed { get; }
    public int WaterUsed { get; }
    public int GameMinutesElapsed { get; }
    public long FinalWorldRevision { get; }
    public int RemainingSeedStack { get; }
    public bool OutOfSeeds { get; }
    public string? ErrorMessage { get; }
    public string? ErrorCode { get; }
    public bool RetryRecommended { get; }
    public bool PlayerActionRequired { get; }

    public PlantZoneResult(
        string commandId,
        string taskId,
        string seedItemId,
        ExecutionState finalState,
        IReadOnlyList<PlantedTileInfo> plantedTiles,
        IReadOnlyList<SkippedTileInfo> skippedTiles,
        IReadOnlyList<FailedTileInfo> failedTiles,
        float staminaUsed,
        int waterUsed,
        int gameMinutesElapsed,
        long finalWorldRevision,
        int remainingSeedStack,
        bool outOfSeeds = false,
        string? errorMessage = null,
        string? errorCode = null,
        bool retryRecommended = false,
        bool playerActionRequired = false)
    {
        CommandId = commandId;
        TaskId = taskId;
        SeedItemId = seedItemId;
        FinalState = finalState;
        PlantedTiles = plantedTiles;
        SkippedTiles = skippedTiles;
        FailedTiles = failedTiles;
        StaminaUsed = staminaUsed;
        WaterUsed = waterUsed;
        GameMinutesElapsed = gameMinutesElapsed;
        FinalWorldRevision = finalWorldRevision;
        RemainingSeedStack = remainingSeedStack;
        OutOfSeeds = outOfSeeds;
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
        foreach (var planted in PlantedTiles)
        {
            effects.Add(new Dictionary<string, object>
            {
                ["tile"] = new Dictionary<string, object> { ["x"] = planted.Tile.X, ["y"] = planted.Tile.Y },
                ["state"] = "planted",
                ["itemId"] = planted.SeedItemId,
                ["stack"] = planted.RemainingStack
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

        SkillResultResources? resources = new(StaminaUsed, WaterUsed, GameMinutesElapsed);
        SkillResultError? error = ErrorMessage is not null
            ? new(ErrorCode ?? "EXECUTION_ERROR", ErrorMessage, null, RetryRecommended)
            : null;

        var details = new Dictionary<string, object>
        {
            ["plantedTiles"] = PlantedTiles.Select(p => new Dictionary<string, object> { ["x"] = p.Tile.X, ["y"] = p.Tile.Y }).ToList(),
            ["remainingSeedStack"] = RemainingSeedStack,
            ["seedItemId"] = SeedItemId,
            ["outOfSeeds"] = OutOfSeeds,
            ["skippedTiles"] = SkippedTiles.Select(s => new Dictionary<string, object>
            {
                ["x"] = s.Tile.X,
                ["y"] = s.Tile.Y,
                ["reason"] = s.Reason
            }).ToList()
        };

        return new SkillResultPayload(
            CommandId: CommandId,
            TaskId: TaskId,
            TerminalState: terminalState,
            CompletedCount: PlantedTiles.Count,
            SkippedCount: SkippedTiles.Count,
            FailedCount: FailedTiles.Count,
            FinalWorldRevision: FinalWorldRevision,
            Effects: effects,
            Resources: resources,
            Error: error,
            RetryRecommended: RetryRecommended,
            PlayerActionRequired: PlayerActionRequired,
            SkillId: "plant-seeds",
            Details: details
        );
    }
}
