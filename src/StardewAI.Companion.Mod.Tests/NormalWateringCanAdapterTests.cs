using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class NormalWateringCanAdapterTests
{
    [Fact]
    public void EmptyCan_PrecheckFails_WithoutCallingTool()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var monitor = new TestMonitor();
        var adapter = new NormalWateringCanAdapter(observer, monitor);

        // Actor with 0 water
        var actor = new MechanicsActor("test-actor", water: 0);
        var targetTile = new TileCoordinate(64, 16);
        actor.UpdatePose("Farm", new TileCoordinate(64, 15), FacingDirection.Down);

        observer.SetDirt(targetTile, TileDirtState.DryDirt());

        var result = adapter.WaterTile(actor, "Farm", targetTile);

        Assert.False(result.Success);
        Assert.True(result.PreconditionFailed);
        Assert.Contains("watering can is empty", result.ErrorMessage);
    }

    [Fact]
    public void LowStamina_PrecheckFails_WithoutCallingTool()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var monitor = new TestMonitor();
        var adapter = new NormalWateringCanAdapter(observer, monitor);

        // Actor with 1.0 stamina (< 2.0 required)
        var actor = new MechanicsActor("test-actor", stamina: 1.0f);
        var targetTile = new TileCoordinate(64, 16);
        actor.UpdatePose("Farm", new TileCoordinate(64, 15), FacingDirection.Down);

        observer.SetDirt(targetTile, TileDirtState.DryDirt());

        var result = adapter.WaterTile(actor, "Farm", targetTile);

        Assert.False(result.Success);
        Assert.True(result.PreconditionFailed);
        Assert.Contains("stamina is exhausted", result.ErrorMessage);
    }

    [Fact]
    public void NonAdjacentTile_PrecheckFails()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var monitor = new TestMonitor();
        var adapter = new NormalWateringCanAdapter(observer, monitor);

        var actor = new MechanicsActor("test-actor", stamina: 100f, water: 40);
        var targetTile = new TileCoordinate(64, 20); // 5 tiles away
        actor.UpdatePose("Farm", new TileCoordinate(64, 15), FacingDirection.Down);

        observer.SetDirt(targetTile, TileDirtState.DryDirt());

        var result = adapter.WaterTile(actor, "Farm", targetTile);

        Assert.False(result.Success);
        Assert.True(result.PreconditionFailed);
        Assert.Contains("not adjacent", result.ErrorMessage);
    }

    [Fact]
    public void AlreadyWateredTile_PrecheckFails()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var monitor = new TestMonitor();
        var adapter = new NormalWateringCanAdapter(observer, monitor);

        var actor = new MechanicsActor("test-actor", stamina: 100f, water: 40);
        var targetTile = new TileCoordinate(64, 16);
        actor.UpdatePose("Farm", new TileCoordinate(64, 15), FacingDirection.Down);

        observer.SetDirt(targetTile, TileDirtState.WateredDirt());

        var result = adapter.WaterTile(actor, "Farm", targetTile);

        Assert.False(result.Success);
        Assert.True(result.PreconditionFailed);
        Assert.Contains("already watered", result.ErrorMessage);
    }

    [Fact]
    public void NonMainThread_FailsImmediately()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm", IsMainThread = false };
        var monitor = new TestMonitor();
        var adapter = new NormalWateringCanAdapter(observer, monitor);

        var actor = new MechanicsActor("test-actor", stamina: 100f, water: 40);
        var targetTile = new TileCoordinate(64, 16);
        actor.UpdatePose("Farm", new TileCoordinate(64, 15), FacingDirection.Down);

        var result = adapter.WaterTile(actor, "Farm", targetTile);

        Assert.False(result.Success);
        Assert.Contains("main thread", result.ErrorMessage);
    }

    [Fact]
    public void UnknownMap_FailsImmediately()
    {
        // The player's active map no longer gates watering; only an unloadable
        // target map fails. The companion may water the Farm while the player
        // is elsewhere (e.g. FarmHouse the morning after a pass-out).
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Town" };
        var monitor = new TestMonitor();
        var adapter = new NormalWateringCanAdapter(observer, monitor);

        var actor = new MechanicsActor("test-actor", stamina: 100f, water: 40);
        var targetTile = new TileCoordinate(64, 16);
        actor.UpdatePose("Farm", new TileCoordinate(64, 15), FacingDirection.Down);

        var result = adapter.WaterTile(actor, "Nowhere", targetTile);

        Assert.False(result.Success);
        Assert.Contains("not loaded or does not exist", result.ErrorMessage);
    }
}
