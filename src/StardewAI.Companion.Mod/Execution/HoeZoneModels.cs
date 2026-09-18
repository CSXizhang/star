using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

public sealed record HoeZoneRequest(
    string CommandId,
    string TaskId,
    string LocationId,
    IReadOnlyList<TileCoordinate> TargetTiles,
    float MaxStamina = 100f,
    int MaxWater = 0,
    int MaxGameMinutes = 60,
    string CancelPolicy = "safe-point",
    string? IdempotencyKey = null,
    long ExpectedWorldRevision = 1
);

public sealed record HoedTileInfo(
    TileCoordinate Tile,
    float StaminaCost
);

public sealed class HoeZoneResult
{
    public string CommandId { get; }
    public string TaskId { get; }
    public ExecutionState FinalState { get; }
    public IReadOnlyList<HoedTileInfo> HoedTiles { get; }
    public IReadOnlyList<SkippedTileInfo> SkippedTiles { get; }
    public IReadOnlyList<FailedTileInfo> FailedTiles { get; }
    public float StaminaUsed { get; }
    public int WaterUsed { get; }
    public int GameMinutesElapsed { get; }
    public long FinalWorldRevision { get; }
    public string? ErrorMessage { get; }
    public string? ErrorCode { get; }
    public bool RetryRecommended { get; }
    public bool PlayerActionRequired { get; }

    public HoeZoneResult(
        string commandId,
        string taskId,
        ExecutionState finalState,
        IReadOnlyList<HoedTileInfo> hoedTiles,
        IReadOnlyList<SkippedTileInfo> skippedTiles,
        IReadOnlyList<FailedTileInfo> failedTiles,
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
        HoedTiles = hoedTiles;
        SkippedTiles = skippedTiles;
        FailedTiles = failedTiles;
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
        foreach (var hoed in HoedTiles)
        {
            effects.Add(new Dictionary<string, object>
            {
                ["tile"] = new Dictionary<string, object> { ["x"] = hoed.Tile.X, ["y"] = hoed.Tile.Y },
                ["state"] = "hoed"
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
            ["hoedTiles"] = HoedTiles.Select(h => new Dictionary<string, object> { ["x"] = h.Tile.X, ["y"] = h.Tile.Y }).ToList(),
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
            CompletedCount: HoedTiles.Count,
            SkippedCount: SkippedTiles.Count,
            FailedCount: FailedTiles.Count,
            FinalWorldRevision: FinalWorldRevision,
            Effects: effects,
            Resources: resources,
            Error: error,
            RetryRecommended: RetryRecommended,
            PlayerActionRequired: PlayerActionRequired,
            SkillId: "hoe-tiles",
            Details: details
        );
    }
}
