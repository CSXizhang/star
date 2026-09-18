using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

public sealed record NavigationRequest(
    string CommandId,
    string TaskId,
    string LocationId,
    TileCoordinate TargetTile,
    int MaxGameMinutes = 120,
    float MaxStamina = 50f,
    int MaxWater = 0,
    string CancelPolicy = "safe-point"
);

public sealed record NavigationHopRecord(
    string Location,
    int PathLength,
    MapEdge? ExitWarp
);

public sealed record NavigationResult(
    string CommandId,
    string TaskId,
    ExecutionState FinalState,
    string TargetLocation,
    TileCoordinate TargetTile,
    string FinalLocation,
    TileCoordinate FinalTile,
    IReadOnlyList<string> VisitedLocations,
    IReadOnlyList<NavigationHopRecord> HopRecords,
    float StaminaUsed,
    int WaterUsed,
    int GameMinutesElapsed,
    long FinalWorldRevision,
    string? ErrorMessage = null,
    string? ErrorCode = null,
    Dictionary<string, object>? LockDetails = null,
    bool TargetAdjusted = false,
    TileCoordinate? RequestedTile = null
)
{
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
        foreach (var hop in HopRecords)
        {
            var eff = new Dictionary<string, object>
            {
                ["location"] = hop.Location,
                ["pathLength"] = hop.PathLength,
            };
            if (hop.ExitWarp != null)
            {
                eff["exitWarp"] = new Dictionary<string, object>
                {
                    ["sourceTile"] = new Dictionary<string, object> { ["x"] = hop.ExitWarp.SourceTile.X, ["y"] = hop.ExitWarp.SourceTile.Y },
                    ["targetLocation"] = hop.ExitWarp.TargetLocation,
                    ["targetTile"] = new Dictionary<string, object> { ["x"] = hop.ExitWarp.TargetTile.X, ["y"] = hop.ExitWarp.TargetTile.Y },
                    ["kind"] = hop.ExitWarp.EdgeKind.ToString()
                };
            }
            effects.Add(eff);
        }

        var reqTile = RequestedTile ?? TargetTile;
        var details = new Dictionary<string, object>
        {
            ["visitedLocations"] = VisitedLocations.ToList(),
            ["finalLocation"] = FinalLocation,
            ["finalTile"] = new Dictionary<string, object> { ["x"] = FinalTile.X, ["y"] = FinalTile.Y },
            ["targetAdjusted"] = TargetAdjusted,
            ["requestedTile"] = new Dictionary<string, object> { ["x"] = reqTile.X, ["y"] = reqTile.Y },
            ["hopCount"] = HopRecords.Count,
            ["route"] = HopRecords.Select(h => new Dictionary<string, object>
            {
                ["location"] = h.Location,
                ["pathLength"] = h.PathLength,
                ["targetLocation"] = h.ExitWarp?.TargetLocation ?? FinalLocation
            }).ToList()
        };

        if (LockDetails != null)
        {
            details["lockDetails"] = LockDetails;
        }

        SkillResultResources? resources = new(StaminaUsed, WaterUsed, GameMinutesElapsed);
        SkillResultError? error = ErrorMessage is not null
            ? new(
                ErrorCode ?? "EXECUTION_ERROR",
                ErrorMessage,
                LockDetails != null ? System.Text.Json.JsonSerializer.Serialize(LockDetails) : null,
                Retryable: false)
            : null;

        return new SkillResultPayload(
            CommandId: CommandId,
            TaskId: TaskId,
            TerminalState: terminalState,
            CompletedCount: FinalState == ExecutionState.Succeeded ? 1 : 0,
            SkippedCount: 0,
            FailedCount: FinalState == ExecutionState.Failed ? 1 : 0,
            FinalWorldRevision: FinalWorldRevision,
            Effects: effects,
            Resources: resources,
            Error: error,
            RetryRecommended: false,
            PlayerActionRequired: false,
            SkillId: "navigate-to",
            Details: details,
            TargetAdjusted: TargetAdjusted,
            RequestedTile: new TileCoord(reqTile.X, reqTile.Y)
        );
    }
}
