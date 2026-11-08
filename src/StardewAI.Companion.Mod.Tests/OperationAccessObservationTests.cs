using System.Text.Json;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class OperationAccessObservationTests
{
    private static MachineScanInfo Machine(TileCoordinate tile, string item) =>
        new(tile, item, item, true, false, 0, 0, null, null, 0, 0, null, false, "Farm");

    [Fact]
    public void PlacingNeighborReportsOlderMachineEnclosedAndRemovalRestoresAccessWithoutMovingActor()
    {
        var observer = new SimulatedWorldObserver();
        var actor = new MechanicsActor();
        actor.SetLocation("Farm", new(10, 14));
        var navigator = new SameMapNavigator(observer);
        var old = Machine(new(10, 10), "(BC)21");
        observer.SetMachines(new[] { old });
        foreach (var tile in new[] { new TileCoordinate(10, 10), new(10, 9), new(9, 10), new(11, 10) })
            observer.SetPassable(tile, false);
        JsonElement Observe() => JsonSerializer.SerializeToElement(OperationAccessObserver.Observe(
            observer, navigator, actor, "Farm", new[] { new TileCoordinate(10, 11) }));
        Assert.True(Observe().GetProperty("items")[0].GetProperty("reachable").GetBoolean());
        observer.SetPassable(new(10, 11), false);
        observer.SetMachines(new[] { old, Machine(new(10, 11), "(BC)17") });
        var after = Observe().GetProperty("items");
        Assert.False(after[0].GetProperty("reachable").GetBoolean());
        Assert.Equal(JsonValueKind.Null, after[0].GetProperty("standingTile").ValueKind);
        Assert.True(after[1].GetProperty("reachable").GetBoolean());
        observer.SetPassable(new(10, 11), true);
        observer.SetMachines(new[] { old });
        Assert.True(Observe().GetProperty("items")[0].GetProperty("reachable").GetBoolean());
        Assert.Equal(new TileCoordinate(10, 14), actor.Tile);
    }

    [Fact]
    public void RemoteMapObservationDoesNotDeclareTheMachineUnreachable()
    {
        var observer = new SimulatedWorldObserver();
        observer.SetMachines(new[] { Machine(new(10, 10), "(BC)21") });
        var actor = new MechanicsActor();
        actor.SetLocation("FarmHouse", new(3, 3));
        var result = JsonSerializer.SerializeToElement(OperationAccessObserver.Observe(
            observer, new SameMapNavigator(observer), actor, "Farm"));
        Assert.Equal(JsonValueKind.Null, result.GetProperty("items")[0].GetProperty("reachable").ValueKind);
        Assert.Equal("actor-on-other-map", result.GetProperty("items")[0].GetProperty("observationStatus").GetString());
    }
}
