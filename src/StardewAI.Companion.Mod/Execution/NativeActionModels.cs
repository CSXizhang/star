using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>
/// Explicit, allow-listed agricultural/husbandry actions. Each value maps to one
/// skill id and one typed native implementation; there is no generic reflection entry.
/// </summary>
public enum NativeActionKind
{
    RefillWateringCan,
    ApplyFertilizer,
    ClearDebris,
    PickupItems,
    InsertMachine,
    CollectMachine,
    PetAnimal,
    FeedAnimals,
    ToggleAnimalDoor,
    CollectAnimalProduce
}

/// <summary>
/// Minimal structured progress for one running native action, shared with the chat
/// bridge / F8 surface (Q5): action, native phase, completed/total from the real
/// target loop, and a machine-readable reason when waiting or terminal. Completed
/// and total are never invented — <c>total</c> is 0 when the request carries no
/// countable targets, and the UI then shows no percentage.
/// </summary>
public sealed record NativeActionProgress(
    string Action,
    string Phase,
    int Completed,
    int Total,
    string? ReasonCode = null
);

/// <summary>
/// One explicit target: a map tile, plus an optional native identity
/// (machine tile is the tile itself; animal name / building indoors name for husbandry).
/// </summary>
public sealed record NativeActionTarget(
    TileCoordinate Tile,
    string? TargetId = null
);

public sealed record NativeActionRequest(
    string CommandId,
    string TaskId,
    string LocationId,
    NativeActionKind Kind,
    string SkillId,
    IReadOnlyList<NativeActionTarget> Targets,
    string? ItemId = null,
    int ItemCount = 1,
    float MaxStamina = 100f,
    int MaxWater = 0,
    int MaxGameMinutes = 60,
    string CancelPolicy = "safe-point",
    string? IdempotencyKey = null,
    long ExpectedWorldRevision = 1
);

public sealed record NativeActionEffect(
    string Target,
    string State,
    string? Reason = null,
    string? ItemId = null,
    int Count = 0,
    TileCoordinate? Tile = null
);

public sealed class NativeActionResult
{
    public string CommandId { get; }
    public string TaskId { get; }
    public string SkillId { get; }
    public ExecutionState FinalState { get; }
    public IReadOnlyList<NativeActionEffect> Effects { get; }
    public IReadOnlyList<NativeActionEffect> Skipped { get; }
    public IReadOnlyList<NativeActionEffect> Failed { get; }
    public float StaminaUsed { get; }
    public int WaterUsed { get; }
    public int GameMinutesElapsed { get; }
    public long FinalWorldRevision { get; }
    public string? ErrorMessage { get; }
    public string? ErrorCode { get; }
    public bool RetryRecommended { get; }
    public bool PlayerActionRequired { get; }

    /// <summary>Final structured progress (Q5): action/phase/completed/total/reasonCode.</summary>
    public NativeActionProgress? Progress { get; }

    public NativeActionResult(
        string commandId,
        string taskId,
        string skillId,
        ExecutionState finalState,
        IReadOnlyList<NativeActionEffect> effects,
        IReadOnlyList<NativeActionEffect> skipped,
        IReadOnlyList<NativeActionEffect> failed,
        float staminaUsed,
        int waterUsed,
        int gameMinutesElapsed,
        long finalWorldRevision,
        string? errorMessage = null,
        string? errorCode = null,
        bool retryRecommended = false,
        bool playerActionRequired = false,
        NativeActionProgress? progress = null)
    {
        CommandId = commandId;
        TaskId = taskId;
        SkillId = skillId;
        FinalState = finalState;
        Effects = effects;
        Skipped = skipped;
        Failed = failed;
        StaminaUsed = staminaUsed;
        WaterUsed = waterUsed;
        GameMinutesElapsed = gameMinutesElapsed;
        FinalWorldRevision = finalWorldRevision;
        ErrorMessage = errorMessage;
        ErrorCode = errorCode;
        RetryRecommended = retryRecommended;
        PlayerActionRequired = playerActionRequired;
        Progress = progress;
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
        foreach (var effect in Effects.Concat(Skipped).Concat(Failed))
        {
            var entry = new Dictionary<string, object>
            {
                ["state"] = effect.State
            };
            if (effect.Tile is not null)
                entry["tile"] = new Dictionary<string, object> { ["x"] = effect.Tile.Value.X, ["y"] = effect.Tile.Value.Y };
            if (!string.IsNullOrEmpty(effect.Target))
                entry["target"] = effect.Target;
            if (!string.IsNullOrEmpty(effect.Reason))
                entry["reason"] = effect.Reason!;
            if (!string.IsNullOrEmpty(effect.ItemId))
                entry["itemId"] = effect.ItemId!;
            if (effect.Count > 0)
                entry["stack"] = effect.Count;
            effects.Add(entry);
        }

        var details = new Dictionary<string, object>
        {
            ["completed"] = Effects.Select(Describe).ToList(),
            ["skipped"] = Skipped.Select(Describe).ToList(),
            ["failed"] = Failed.Select(Describe).ToList()
        };

        // Structured progress: emitted only when the state machine really tracked a
        // target loop. No data means no percentage is shown upstream.
        if (Progress is not null)
        {
            details["action"] = Progress.Action;
            details["phase"] = Progress.Phase;
            details["completedCount"] = Progress.Completed;
            details["total"] = Progress.Total;
            if (!string.IsNullOrEmpty(Progress.ReasonCode))
                details["reasonCode"] = Progress.ReasonCode!;
        }

        SkillResultError? error = ErrorMessage is not null
            ? new(ErrorCode ?? "EXECUTION_ERROR", ErrorMessage, null, RetryRecommended)
            : null;

        return new SkillResultPayload(
            CommandId: CommandId,
            TaskId: TaskId,
            TerminalState: terminalState,
            CompletedCount: Effects.Count,
            SkippedCount: Skipped.Count,
            FailedCount: Failed.Count,
            FinalWorldRevision: FinalWorldRevision,
            Effects: effects,
            Resources: new SkillResultResources(StaminaUsed, WaterUsed, GameMinutesElapsed),
            Error: error,
            RetryRecommended: RetryRecommended,
            PlayerActionRequired: PlayerActionRequired,
            SkillId: SkillId,
            Details: details
        );
    }

    private static Dictionary<string, object> Describe(NativeActionEffect effect)
    {
        var entry = new Dictionary<string, object>
        {
            ["state"] = effect.State,
            ["target"] = effect.Target
        };
        if (effect.Tile is not null)
        {
            entry["x"] = effect.Tile.Value.X;
            entry["y"] = effect.Tile.Value.Y;
        }
        if (!string.IsNullOrEmpty(effect.Reason))
            entry["reason"] = effect.Reason!;
        if (!string.IsNullOrEmpty(effect.ItemId))
            entry["itemId"] = effect.ItemId!;
        if (effect.Count > 0)
            entry["count"] = effect.Count;
        return entry;
    }
}
