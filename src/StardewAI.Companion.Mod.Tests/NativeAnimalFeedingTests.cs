using System.Reflection;
using System.Runtime.CompilerServices;
using Netcode;
using StardewAI.Companion.Mod.Adapters;
using StardewValley;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class NativeAnimalFeedingTests
{
    [Theory]
    [InlineData(10, 12, 12, 10)]
    [InlineData(0, 12, 12, 0)]
    [InlineData(10, 8, 12, 8)]
    [InlineData(10, 12, 6, 6)]
    [InlineData(10, 0, 12, 0)]
    [InlineData(10, 12, 0, 0)]
    public void FeedDemandUsesResidentsOutsideAndRespectsRealCapacity(
        int residentCount, int animalLimit, int troughTiles, int expected)
    {
        // Only native residency/capacity fields are needed. Leave the interior
        // animal collection unset: no animal has to be physically indoors.
        var house = (AnimalHouse)RuntimeHelpers.GetUninitializedObject(typeof(AnimalHouse));
        var residents = new NetLongList();
        for (int i = 0; i < residentCount; i++) residents.Add(i + 1);
        typeof(AnimalHouse).GetField("animalsThatLiveHere")!.SetValue(house, residents);
        typeof(AnimalHouse).GetField("animalLimit")!.SetValue(house, new NetInt(animalLimit));

        Assert.Equal(expected, FeedTarget(house, troughTiles));
        Assert.Equal(residentCount, residents.Count);
        if (residentCount > 0)
        {
            residents.Add(1);
            Assert.Equal(expected, FeedTarget(house, troughTiles));
        }
    }

    private static int FeedTarget(AnimalHouse house, int troughTiles) =>
        (int)typeof(NormalNativeActionAdapter).GetMethod("GetAnimalFeedTargetCount",
            BindingFlags.NonPublic | BindingFlags.Static)!.Invoke(null, new object[] { house, troughTiles })!;

    [Fact]
    public void FriendlyFeedAliasResolvesToItsUniqueRealInterior()
    {
        var house = House("Coop", "Coop-unique-one");
        var candidates = new (AnimalHouse House, string? BuildingType)[] { (house, "Deluxe Coop") };
        Assert.Same(house, Resolve("Deluxe Coop", candidates, out bool ambiguous));
        Assert.False(ambiguous);
        Assert.Same(house, Resolve("Coop", candidates, out ambiguous));
        Assert.False(ambiguous);
        Assert.Same(house, Resolve("coop-UNIQUE-one", candidates, out ambiguous));
        Assert.False(ambiguous);
        Assert.Equal("Coop-unique-one", house.NameOrUniqueName);
        Assert.True(IsCurrentHouse(house, house));
    }

    [Fact]
    public void DuplicateFriendlyNamesRequireStableInteriorIdentity()
    {
        var first = House("Coop", "Coop-unique-one");
        var second = House("Coop", "Coop-unique-two");
        var candidates = new (AnimalHouse House, string? BuildingType)[] { (first, "Deluxe Coop"), (second, "Deluxe Coop") };
        Assert.Null(Resolve("Deluxe Coop", candidates, out bool ambiguous));
        Assert.True(ambiguous);
        Assert.Null(Resolve("Coop", candidates, out ambiguous));
        Assert.True(ambiguous);
        Assert.Same(second, Resolve("Coop-unique-two", candidates, out ambiguous));
        Assert.False(ambiguous);
        Assert.False(IsCurrentHouse(second, first));
        Assert.False(IsCurrentHouse(second, null));
    }

    [Fact]
    public void UnknownFeedTargetNeverFallsBackToAnotherHouse()
    {
        var house = House("Coop", "Coop-unique-one");
        Assert.Null(Resolve("missing-coop", new (AnimalHouse House, string? BuildingType)[] { (house, "Deluxe Coop") }, out bool ambiguous));
        Assert.False(ambiguous);
        Assert.False(IsCurrentHouse(house, new GameLocation()));
    }

    [Fact]
    public void StableInteriorIdentityWinsOverAnotherBuildingsAlias()
    {
        var first = House("Coop", "Coop-unique-one");
        var second = House("Coop", "Coop-unique-two");
        var candidates = new (AnimalHouse House, string? BuildingType)[] {
            (first, "Deluxe Coop"), (first, null), (second, "Coop-unique-one") };
        Assert.Same(first, Resolve("Coop-unique-one", candidates, out bool ambiguous));
        Assert.False(ambiguous);
        Assert.Same(first, Resolve("Deluxe Coop", candidates, out ambiguous));
        Assert.False(ambiguous);
    }

    private static AnimalHouse House(string name, string identity)
    {
        var house = new AnimalHouse();
        house.name.Value = name;
        house.uniqueName.Value = identity;
        return house;
    }

    private static AnimalHouse? Resolve(string name, IEnumerable<(AnimalHouse House, string? BuildingType)> houses,
        out bool ambiguous)
    {
        object?[] arguments = { name, houses, false };
        var result = (AnimalHouse?)typeof(NormalNativeActionAdapter).GetMethod("SelectFeedAnimalHouse",
            BindingFlags.NonPublic | BindingFlags.Static)!.Invoke(null, arguments);
        ambiguous = (bool)arguments[2]!;
        return result;
    }

    private static bool IsCurrentHouse(AnimalHouse house, GameLocation? current) =>
        (bool)typeof(NormalNativeActionAdapter).GetMethod("IsCurrentFeedAnimalHouse",
            BindingFlags.NonPublic | BindingFlags.Static)!.Invoke(null, new object?[] { house, current })!;
}
