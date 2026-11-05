using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class WaitStateMachineTests
{
    private static SkillExecutePayload Request(int minutes = 40) => new("wait-cmd", "wait-task", "wait", "1.0", 1,
        new WaterZoneParameters("Farm", null) { WaitMinutes = minutes }, new(0, 0, 0), "safe-point", "model-selected");

    [Fact]
    public void WaitingNeedsNativeClockProgressAndNeverMovesOrSpendsResources()
    {
        var actor = new MechanicsActor();
        var observer = new SimulatedWorldObserver { TimeOfDay = 820 };
        var pose = actor.Pose;
        var machine = new WaitStateMachine(actor, observer, () => null);
        SkillResultPayload? result = null;
        machine.Completed += value => result = value;
        Assert.True(machine.Start(Request(), out _));
        for (int tick = 0; tick < 1000; tick++) machine.Update(null, tick);
        Assert.True(machine.IsExecuting);
        Assert.Null(result);
        observer.TimeOfDay = 850;
        machine.Update(null, 1001);
        Assert.True(machine.IsExecuting);
        observer.TimeOfDay = 900;
        machine.Update(null, 1002);
        Assert.Equal("succeeded", result!.TerminalState);
        Assert.Equal(40, result.Resources!.GameMinutesElapsed);
        Assert.Equal(pose, actor.Pose);
        Assert.Equal(270, actor.Stamina);
        Assert.Equal(40, actor.WaterLeft);
        Assert.Null(actor.ActiveTaskId);
        Assert.False(machine.IsExecuting);
    }

    [Fact]
    public void PausedTimeIsExcludedAndCancellationDoesNotResumeWaiting()
    {
        var observer = new SimulatedWorldObserver { TimeOfDay = 820 };
        var machine = new WaitStateMachine(new MechanicsActor(), observer, () => null);
        var results = new List<SkillResultPayload>();
        machine.Completed += results.Add;
        Assert.True(machine.Start(Request(), out _));
        observer.TimeOfDay = 830; machine.Update(null, 1);
        machine.RequestPause();
        observer.TimeOfDay = 1000; machine.Update(null, 2);
        Assert.Empty(results);
        machine.Resume();
        observer.TimeOfDay = 1010; machine.Update(null, 3);
        machine.RequestCancel("F9 human takeover"); machine.Update(null, 4);
        observer.TimeOfDay = 1200; machine.Update(null, 5);
        Assert.Single(results);
        Assert.Equal("cancelled", results[0].TerminalState);
        Assert.Equal(20, results[0].Resources!.GameMinutesElapsed);
        Assert.False(machine.IsExecuting);
    }

    [Fact]
    public void ChangedWorldOrNativeOwnershipReturnsControlInsteadOfContinuingAnOldWait()
    {
        var actor = new MechanicsActor();
        var observer = new SimulatedWorldObserver { TimeOfDay = 2500 };
        string? blocked = null;
        var machine = new WaitStateMachine(actor, observer, () => blocked);
        var results = new List<SkillResultPayload>();
        machine.Completed += results.Add;
        Assert.True(machine.Start(Request(120), out _));
        observer.DayOfMonth = 2; observer.TimeOfDay = 600; machine.Update(null, 1);
        Assert.Equal("WORLD_CHANGED", results.Single().Error!.Code);
        Assert.True(machine.Start(Request(), out _));
        blocked = "Native question requires an answer"; machine.Update(null, 2);
        Assert.Equal("WAIT_INTERRUPTED", results.Last().Error!.Code);
        Assert.Null(actor.ActiveTaskId);
        Assert.False(machine.Start(Request(), out var rejected));
        Assert.Equal("WAIT_UNAVAILABLE", rejected!.Error!.Code);
    }
}
