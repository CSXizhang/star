using System.Reflection;
using StardewAI.Companion.Mod.Observation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class MachineObservationStateTests
{
    [Fact]
    public void CollectedMachineIsIdleEvenWhenPreviousInputIsRetained()
    {
        var machine = new StardewValley.Object();
        var previousInput = new StardewValley.Object();
        machine.lastInputItem.Value = previousInput;
        machine.lastOutputRuleId.Value = "Default_Egg";
        machine.heldObject.Value = new StardewValley.Object();
        machine.MinutesUntilReady = 60;
        Assert.True(HasInput(machine));

        machine.MinutesUntilReady = 0;
        machine.readyForHarvest.Value = true;
        Assert.True(HasInput(machine));

        // Native collection clears the current output/readiness but keeps history.
        machine.heldObject.Value = null;
        machine.readyForHarvest.Value = false;
        Assert.False(HasInput(machine));
        Assert.Same(previousInput, machine.lastInputItem.Value);
        Assert.Equal("Default_Egg", machine.lastOutputRuleId.Value);
    }

    [Theory]
    [InlineData(60, false, false, true)]
    [InlineData(0, false, true, true)]
    [InlineData(0, true, false, true)]
    [InlineData(0, true, true, true)]
    [InlineData(0, false, false, false)]
    [InlineData(-1, false, false, false)]
    public void CurrentMachineSignalsProtectProcessingReadyAndAnomalousOutputs(
        int minutes, bool ready, bool holdsOutput, bool expected)
    {
        var machine = new StardewValley.Object();
        machine.MinutesUntilReady = minutes;
        machine.readyForHarvest.Value = ready;
        if (holdsOutput) machine.heldObject.Value = new StardewValley.Object();

        Assert.Equal(expected, HasInput(machine));
    }

    private static bool HasInput(StardewValley.Object machine) =>
        (bool)typeof(GameWorldObserver).GetMethod("HasActiveMachineInput",
            BindingFlags.NonPublic | BindingFlags.Static)!.Invoke(null, new object[] { machine })!;
}
