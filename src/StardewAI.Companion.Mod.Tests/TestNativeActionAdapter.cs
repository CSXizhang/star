using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>
/// In-memory test double for INativeActionAdapter: records every target the state
/// machine actually acted on and can simulate native preconditions/failures.
/// </summary>
public sealed class TestNativeActionAdapter : INativeActionAdapter
{
    public int CallCount { get; private set; }
    public List<(NativeActionKind Kind, NativeActionTarget Target)> Calls { get; } = new();
    public float StaminaCostPerAction { get; set; } = 1.0f;
    public bool SimulatePrecondition { get; set; }
    public string PreconditionSkipReason { get; set; } = "precondition";
    public bool SimulateFailure { get; set; }
    public string FailureMessage { get; set; } = "native failure";
    public bool SimulatePlayerActionRequired { get; set; }

    /// <summary>Return Continue() for the first N calls, simulating a multi-tick action.</summary>
    public int ContinueCallsBeforeSuccess { get; set; }

    public NativeActionStepResult Execute(
        IFarmerActor actor,
        NativeActionRequest request,
        NativeActionTarget target)
    {
        CallCount++;
        Calls.Add((request.Kind, target));

        if (ContinueCallsBeforeSuccess > 0)
        {
            ContinueCallsBeforeSuccess--;
            return NativeActionStepResult.Continue("chopping");
        }

        if (SimulatePrecondition)
        {
            return NativeActionStepResult.Precondition(
                $"simulated precondition on {target.Tile}",
                PreconditionSkipReason,
                playerActionRequired: SimulatePlayerActionRequired);
        }

        if (SimulateFailure)
        {
            return NativeActionStepResult.Failed(FailureMessage, playerActionRequired: SimulatePlayerActionRequired);
        }

        if (actor.Stamina > 0f)
            actor.Stamina = Math.Max(0f, actor.Stamina - StaminaCostPerAction);

        return NativeActionStepResult.Succeeded(
            request.Kind.ToString().ToLowerInvariant(),
            staminaCost: StaminaCostPerAction,
            itemId: request.ItemId,
            itemCount: target.TargetId is null ? 1 : 0);
    }
}
