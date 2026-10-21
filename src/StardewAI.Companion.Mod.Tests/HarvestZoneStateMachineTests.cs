using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class HarvestZoneStateMachineTests
{
    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void CancellationPreservesEffectsAndReportsExplicitReason(bool afterFirstHarvest)
    {
        var actor = new MechanicsActor("test-companion", 270, 270, 40, 40,
            new AuthoritativePose("Farm", new(10, 10), FacingDirection.Down));
        var observer = new SimulatedWorldObserver();
        var first = new TileCoordinate(10, 12);
        var second = new TileCoordinate(11, 12);
        foreach (var tile in new[] { first, second })
            observer.SetDirt(tile, TileDirtState.DryDirt(hasCrop: true) with { IsHarvestable = true, CropId = "24" });
        var adapter = new TestHarvestAdapter(observer);
        var machine = new HarvestZoneStateMachine(actor, observer, new SameMapNavigator(observer), adapter);
        Assert.True(machine.Start(new HarvestZoneRequest("cancel", "cancel-task", "Farm", new[] { first, second }, 50, 20, 60), out _));
        if (afterFirstHarvest)
        {
            for (var tick = 0; tick < 500 && adapter.Calls == 0; tick++)
                machine.StepTicks(1);
            Assert.Equal(1, adapter.Calls);
        }
        machine.RequestCancel("User cancelled via MCP");
        machine.StepTicks(1);

        var result = machine.FinalResult!;
        Assert.Equal(afterFirstHarvest ? ExecutionState.PartiallySucceeded : ExecutionState.Cancelled, result.FinalState);
        Assert.Equal(afterFirstHarvest ? 1 : 0, result.HarvestedTiles.Count);
        Assert.Equal("CANCELLED", result.ErrorCode);
        Assert.Equal("CANCELLED", result.ToTransportPayload().Error!.Code);
        machine.StepTicks(100);
        Assert.Same(result, machine.FinalResult);
        Assert.Equal(afterFirstHarvest ? 1 : 0, adapter.Calls);
        Assert.True(observer.GetDirtState("Farm", second).IsHarvestable);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void PausedClockDoesNotSpendWorkBudgetAndResumeDoesNotResetActiveTime(bool exhaustActiveBudget)
    {
        var actor = new MechanicsActor("test-companion", 270, 270, 40, 40,
            new AuthoritativePose("Farm", new(10, 10), FacingDirection.Down));
        var observer = new SimulatedWorldObserver();
        var tile = new TileCoordinate(10, 12);
        observer.SetDirt(tile, TileDirtState.DryDirt(hasCrop: true) with { IsHarvestable = true, CropId = "24" });
        var adapter = new TestHarvestAdapter(observer);
        var machine = new HarvestZoneStateMachine(actor, observer, new SameMapNavigator(observer), adapter);
        Assert.True(machine.Start(new HarvestZoneRequest("clock-pause", "clock-task", "Farm", new[] { tile }, 50, 20, 60), out _));
        machine.StepTicks(2);
        observer.TimeOfDay = 610;
        machine.RequestPause();
        machine.StepTicks(1);
        Assert.True(machine.IsPaused);
        observer.TimeOfDay = 800;
        machine.StepTicks(5);
        Assert.Equal(0, adapter.Calls);
        machine.Resume();
        observer.TimeOfDay = 810;
        machine.RequestPause();
        machine.StepTicks(1);
        Assert.True(machine.IsPaused);
        observer.TimeOfDay = 1000;
        machine.Resume();
        observer.TimeOfDay = exhaustActiveBudget ? 1050 : 1010;
        machine.Resume();
        machine.StepTicks(100);

        var result = machine.FinalResult!;
        Assert.Equal(exhaustActiveBudget ? ExecutionState.Failed : ExecutionState.Succeeded, result.FinalState);
        Assert.Equal(exhaustActiveBudget ? "BUDGET_EXHAUSTED" : null, result.ErrorCode);
        Assert.Equal(exhaustActiveBudget ? 0 : 1, adapter.Calls);
        Assert.Equal(exhaustActiveBudget ? 0 : 1, result.HarvestedTiles.Count);
        Assert.Equal(exhaustActiveBudget ? 290 : 250, result.GameMinutesElapsed);
    }

    private sealed class TestHarvestAdapter : IHarvestAdapter
    {
        private readonly SimulatedWorldObserver _observer;
        public int Calls { get; private set; }

        public TestHarvestAdapter(SimulatedWorldObserver observer) => _observer = observer;

        public HarvestTileResult HarvestTile(IFarmerActor actor, string locationName, TileCoordinate targetTile)
        {
            Calls++;
            _observer.SetDirt(targetTile, _observer.GetDirtState(locationName, targetTile) with { IsHarvestable = false, HasCrop = false });
            return HarvestTileResult.Succeeded("24", "(O)24", "Parsnip", 1, 0);
        }
    }
}
