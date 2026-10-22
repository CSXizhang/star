using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class NormalCompanionMapTests
{
    [Fact]
    public void HumanIndoors_CompanionFarm_ReachesActionPreconditions()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "FarmHouse" };
        var actor = new MechanicsActor("test-actor", stamina: 100f, water: 40);
        actor.UpdatePose("Farm", new TileCoordinate(10, 10), FacingDirection.Down);
        var tile = new TileCoordinate(10, 11);
        var monitor = new TestMonitor();
        observer.SetDirt(tile, TileDirtState.WateredDirt());

        var harvest = new NormalHarvestAdapter(observer, monitor).HarvestTile(actor, "Farm", tile);
        var plant = new NormalPlantAdapter(observer, monitor).PlantTile(actor, "Farm", tile, "(O)472");
        var water = new NormalWateringCanAdapter(observer, monitor).WaterTile(actor, "Farm", tile);
        var chest = new NormalChestAdapter(observer, monitor).DepositItem(actor, "Farm", new TileCoordinate(20, 20), 0);
        var shipping = new NormalShippingAdapter(observer, monitor).ShipItem(actor, "Farm", "(O)24", 1);

        Assert.Equal("not-ready", harvest.SkipReason);
        Assert.Equal("out-of-seeds", plant.SkipReason);
        Assert.Contains("already watered", water.ErrorMessage);
        Assert.Contains("not adjacent", chest.ErrorMessage);
        Assert.Contains("no GameFarmer", shipping.ErrorMessage);
        Assert.Equal(100f, actor.Stamina);
        Assert.Equal(40, actor.WaterLeft);
        Assert.True(observer.GetDirtState("Farm", tile).IsWatered);
    }

    [Theory]
    [InlineData("harvest")]
    [InlineData("plant")]
    [InlineData("water")]
    [InlineData("deposit")]
    [InlineData("organize")]
    [InlineData("withdraw")]
    [InlineData("ship")]
    public void CompanionElsewhere_RejectsBeforeWorldOrInventoryAccess(string action)
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var actor = new MechanicsActor("test-actor", stamina: 100f, water: 40);
        actor.UpdatePose("FarmHouse", new TileCoordinate(10, 10), FacingDirection.Down);

        Assert.Contains("companion map 'FarmHouse'", ExecutePrecheck(action, observer, actor));
        Assert.Equal(100f, actor.Stamina);
        Assert.Equal(40, actor.WaterLeft);
    }

    [Theory]
    [InlineData("harvest")]
    [InlineData("plant")]
    [InlineData("water")]
    [InlineData("deposit")]
    [InlineData("organize")]
    [InlineData("withdraw")]
    [InlineData("ship")]
    public void UnloadedCompanionMap_RejectsBeforeWorldOrInventoryAccess(string action)
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        observer.KnownLocations.Clear();
        var actor = new MechanicsActor("test-actor", stamina: 100f, water: 40);
        actor.UpdatePose("Farm", new TileCoordinate(10, 10), FacingDirection.Down);

        Assert.Contains("not loaded", ExecutePrecheck(action, observer, actor));
        Assert.Equal(100f, actor.Stamina);
        Assert.Equal(40, actor.WaterLeft);
    }

    private static string? ExecutePrecheck(string action, SimulatedWorldObserver observer, MechanicsActor actor)
    {
        var monitor = new TestMonitor();
        var tile = new TileCoordinate(10, 11);
        return action switch
        {
            "harvest" => new NormalHarvestAdapter(observer, monitor).HarvestTile(actor, "Farm", tile).ErrorMessage,
            "plant" => new NormalPlantAdapter(observer, monitor).PlantTile(actor, "Farm", tile, "(O)472").ErrorMessage,
            "water" => new NormalWateringCanAdapter(observer, monitor).WaterTile(actor, "Farm", tile).ErrorMessage,
            "deposit" => new NormalChestAdapter(observer, monitor).DepositItem(actor, "Farm", tile, 0).ErrorMessage,
            "organize" => new NormalChestAdapter(observer, monitor).OrganizeChest(actor, "Farm", tile).ErrorMessage,
            "withdraw" => new NormalChestAdapter(observer, monitor).WithdrawItem(actor, "Farm", tile, "(O)24", 1).ErrorMessage,
            "ship" => new NormalShippingAdapter(observer, monitor).ShipItem(actor, "Farm", "(O)24", 1).ErrorMessage,
            _ => throw new ArgumentOutOfRangeException(nameof(action))
        };
    }
}
