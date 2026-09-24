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

    /// <summary>
    /// When true (default), each Continue round really swings and deducts stamina,
    /// like a genuine chop batch that did not finish. When false, Continue rounds
    /// only wait (e.g. for the native tree-fall animation) and consume nothing.
    /// </summary>
    public bool ContinueConsumesStamina { get; set; } = true;

    public NativeActionStepResult Execute(
        IFarmerActor actor,
        NativeActionRequest request,
        NativeActionTarget target)
    {
        CallCount++;
        Calls.Add((request.Kind, target));

        // Mirror the real adapter: every round physically deducts stamina first
        // and then reports the measured delta for that round, so a multi-round
        // action's true total cost is observable on the actor and comparable
        // with what the state machine aggregates.
        if (ContinueCallsBeforeSuccess > 0)
        {
            ContinueCallsBeforeSuccess--;
            float staminaBefore = actor.Stamina;
            if (ContinueConsumesStamina && actor.Stamina > 0f)
                actor.Stamina = Math.Max(0f, actor.Stamina - StaminaCostPerAction);
            return NativeActionStepResult.Continue("chopping", staminaCost: staminaBefore - actor.Stamina);
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
            float staminaBeforeFailure = actor.Stamina;
            if (actor.Stamina > 0f)
                actor.Stamina = Math.Max(0f, actor.Stamina - StaminaCostPerAction);
            return NativeActionStepResult.Failed(
                FailureMessage,
                playerActionRequired: SimulatePlayerActionRequired,
                staminaCost: staminaBeforeFailure - actor.Stamina);
        }

        float staminaBeforeSuccess = actor.Stamina;
        if (actor.Stamina > 0f)
            actor.Stamina = Math.Max(0f, actor.Stamina - StaminaCostPerAction);

        return NativeActionStepResult.Succeeded(
            request.Kind.ToString().ToLowerInvariant(),
            staminaCost: staminaBeforeSuccess - actor.Stamina,
            itemId: request.ItemId,
            itemCount: target.TargetId is null ? 1 : 0);
    }
}
