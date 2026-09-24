using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Result of one atomic native agricultural/husbandry action.
/// </summary>
public sealed class NativeActionStepResult
{
    public bool Success { get; }
    public bool PreconditionFailed { get; }

    /// <summary>
    /// True when the action is legitimately unfinished and needs more tick windows
    /// (e.g. chop-tree waiting out the native fall animation). The state machine
    /// stays on the same target; the round's real resource deltas (e.g. stamina
    /// spent on swings that happened before the wait) are still accumulated.
    /// </summary>
    public bool InProgress { get; }
    public string? ErrorMessage { get; }

    /// <summary>
    /// Machine-readable skip reason when the action could not be applied to this
    /// target but the overall request can continue: "already-watered", "not-refillable",
    /// "no-produce", "missing-tool", "already-petted", "wrong-map", ...
    /// </summary>
    public string? SkipReason { get; }

    /// <summary>Effect state recorded on success, e.g. "refilled", "fertilized".</summary>
    public string State { get; }

    /// <summary>
    /// Real stamina delta observed during this round. Success, failure and
    /// in-progress rounds may all spend stamina; the state machine sums every
    /// round's value so the final report equals the actor's actual consumption.
    /// </summary>
    public float StaminaCost { get; }

    /// <summary>Real water delta observed during this round; accumulated like <see cref="StaminaCost"/>.</summary>
    public int WaterUsed { get; }
    public int WaterGained { get; }
    public string? ItemId { get; }
    public int ItemCount { get; }
    public bool PlayerActionRequired { get; }

    private NativeActionStepResult(
        bool success,
        bool preconditionFailed,
        string? errorMessage,
        string? skipReason,
        string state,
        float staminaCost,
        int waterUsed,
        int waterGained,
        string? itemId,
        int itemCount,
        bool playerActionRequired,
        bool inProgress = false)
    {
        Success = success;
        PreconditionFailed = preconditionFailed;
        ErrorMessage = errorMessage;
        SkipReason = skipReason;
        State = state;
        StaminaCost = staminaCost;
        WaterUsed = waterUsed;
        WaterGained = waterGained;
        ItemId = itemId;
        ItemCount = itemCount;
        PlayerActionRequired = playerActionRequired;
        InProgress = inProgress;
    }

    public static NativeActionStepResult Succeeded(
        string state,
        float staminaCost = 0f,
        int waterUsed = 0,
        int waterGained = 0,
        string? itemId = null,
        int itemCount = 0,
        bool playerActionRequired = false) =>
        new(true, false, null, null, state, staminaCost, waterUsed, waterGained, itemId, itemCount, playerActionRequired);

    public static NativeActionStepResult Precondition(
        string reason,
        string? skipReason = null,
        bool playerActionRequired = false) =>
        new(false, true, reason, skipReason ?? reason, "skipped", 0f, 0, 0, null, 0, playerActionRequired);

    public static NativeActionStepResult Failed(
        string reason,
        bool playerActionRequired = false,
        float staminaCost = 0f,
        int waterUsed = 0) =>
        new(false, false, reason, null, "failed", staminaCost, waterUsed, 0, null, 0, playerActionRequired);

    public static NativeActionStepResult Continue(
        string state,
        float staminaCost = 0f,
        int waterUsed = 0) =>
        new(false, false, null, null, state, staminaCost, waterUsed, 0, null, 0, false, inProgress: true);
}

/// <summary>
/// Adapter contract for explicit native agricultural/husbandry actions. Every
/// implementation must execute through real game APIs on the main thread, must
/// never mutate <c>Game1.player</c> references or the human player's resources,
/// and must return an actionable error instead of faking success.
/// </summary>
public interface INativeActionAdapter
{
    NativeActionStepResult Execute(
        IFarmerActor actor,
        NativeActionRequest request,
        NativeActionTarget target);
}
