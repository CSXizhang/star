using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;
using StardewValley.Tools;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class NormalHoeAdapterTests
{
    [Fact]
    public void HumanIndoors_CompanionFarm_ReachesCropProtection()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "FarmHouse" };
        var actor = new MechanicsActor("test-actor", stamina: 100f) { Hoe = new Hoe() };
        actor.UpdatePose("Farm", new TileCoordinate(10, 10), FacingDirection.Down);
        var target = new TileCoordinate(10, 11);
        observer.SetDirt(target, TileDirtState.DryDirt(hasCrop: true, cropId: "24"));

        var result = new NormalHoeAdapter(observer, new TestMonitor()).HoeTile(actor, "Farm", target);

        Assert.True(result.PreconditionFailed);
        Assert.Equal("has-crop", result.SkipReason);
        Assert.True(observer.GetDirtState("Farm", target).HasCrop);
        Assert.Equal(100f, actor.Stamina);
    }

    [Fact]
    public void CompanionElsewhere_RejectsBeforeToolOrWorldAccess()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var actor = new MechanicsActor("test-actor", stamina: 100f) { Hoe = new Hoe() };
        actor.UpdatePose("FarmHouse", new TileCoordinate(10, 10), FacingDirection.Down);

        var result = new NormalHoeAdapter(observer, new TestMonitor()).HoeTile(actor, "Farm", new TileCoordinate(10, 11));

        Assert.False(result.Success);
        Assert.Contains("companion map 'FarmHouse'", result.ErrorMessage);
        Assert.Equal(100f, actor.Stamina);
    }
}
