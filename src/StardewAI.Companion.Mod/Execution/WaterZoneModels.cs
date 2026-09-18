using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

public enum ExecutionState
{
    Created,
    Validating,
    Preparing,
    Navigating,
    Facing,
    Watering,
    /// <summary>Generic physical action in progress (harvest / chest interaction).</summary>
    Acting,
    Verifying,
    Paused,
    Cancelling,
    Cancelled,
    Succeeded,
    PartiallySucceeded,
    Failed,
    Rejected
}

public sealed record WaterZoneRequest(
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

public sealed record SkippedTileInfo(TileCoordinate Tile, string Reason);
public sealed record FailedTileInfo(TileCoordinate Tile, string Reason);

public sealed class WaterZoneResult
{
    public string CommandId { get; }
    public string TaskId { get; }
    public ExecutionState FinalState { get; }
    public IReadOnlyList<TileCoordinate> WateredTiles { get; }
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

    public WaterZoneResult(
        string commandId,
        string taskId,
        ExecutionState finalState,
        IReadOnlyList<TileCoordinate> wateredTiles,
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
        WateredTiles = wateredTiles;
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

        var effects = WateredTiles.Select(tile => new Dictionary<string, object>
        {
            ["tile"] = new Dictionary<string, object> { ["x"] = tile.X, ["y"] = tile.Y },
            ["state"] = "watered"
        }).ToList();

        SkillResultResources? resources = new(StaminaUsed, WaterUsed, GameMinutesElapsed);
        SkillResultError? error = ErrorMessage is not null
            ? new(ErrorCode ?? "EXECUTION_ERROR", ErrorMessage, null, RetryRecommended)
            : null;

        return new SkillResultPayload(
            CommandId: CommandId,
            TaskId: TaskId,
            TerminalState: terminalState,
            CompletedCount: WateredTiles.Count,
            SkippedCount: SkippedTiles.Count,
            FailedCount: FailedTiles.Count,
            FinalWorldRevision: FinalWorldRevision,
            Effects: effects,
            Resources: resources,
            Error: error,
            RetryRecommended: RetryRecommended,
            PlayerActionRequired: PlayerActionRequired
        );
    }
}
