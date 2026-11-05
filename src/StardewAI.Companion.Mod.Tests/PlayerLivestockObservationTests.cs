using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;
using StardewAI.Companion.Mod.PlayerMode;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class PlayerLivestockObservationTests
{
    private static AnimalScanInfo Animal(string name, string location, int x) => new(
        name, "Chicken", "Chicken", 10, 150, 200, 300, true, "(O)176", "drop", null,
        "Coop", location, new(x, 2), false, false, AnimalId: name);

    [Fact]
    public void ExteriorKeepsRealBuildingAndOutdoorAnimalsButDoesNotInventEmptyInterior()
    {
        var hidden = Animal("inside", "Coop1", 3);
        var outdoor = Animal("outside", "Farm", 20);
        var building = new AnimalBuildingScanInfo("Coop", "Coop1", "Farm", new(4, 4), new(5, 6),
            true, true, 1, 4, 3, 4, 100, new[] { hidden }, "coop", 2, new[] { "inside", "outside" });
        var scan = new LivestockScanInfo(new[] { building }, new[] { outdoor }, false, false);
        var result = PlayerKnownObserver.VisibleLivestock(scan, "Farm", l => l == "Farm", (_, _) => true, 12, 1);
        var observed = Assert.Single(result.Buildings);
        Assert.Equal("coop", observed.BuildingId);
        Assert.True(observed.AnimalDoorOpen);
        Assert.Empty(observed.Animals);
        Assert.Null(observed.HayCount);
        Assert.Null(observed.AnimalCount);
        Assert.Null(observed.ResidentCount);
        Assert.Null(observed.ResidentAnimalIds);
        Assert.Equal("outside", Assert.Single(result.RoamingAnimals).Name);
        Assert.True(result.AnimalsTruncated);
    }

    [Fact]
    public void CurrentInteriorReturnsVisibleAnimalsWithoutLeakingOtherHousesOrSilos()
    {
        AnimalBuildingScanInfo House(string id) => new("Coop", id, "Farm", new(4, 4), new(5, 6),
            true, true, 2, 4, 3, 4, 100, new[] { Animal(id + "visible", id, 3), Animal(id + "offscreen", id, 40) });
        var scan = new LivestockScanInfo(new[] { House("Hidden"), House("Current") }, Array.Empty<AnimalScanInfo>(), false, false);
        var result = PlayerKnownObserver.VisibleLivestock(scan, "Current", l => l == "Current", (_, t) => t.X < 10, 1, 1);
        var observed = Assert.Single(result.Buildings);
        Assert.Equal("Currentvisible", Assert.Single(observed.Animals).Name);
        Assert.Equal(3, observed.HayCount);
        Assert.Null(observed.SiloHayCount);
        Assert.Null(observed.AnimalCount);
        Assert.True(result.AnimalsTruncated);
        Assert.False(result.BuildingsTruncated);
    }
}
