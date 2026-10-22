using StardewAI.Companion.Mod.Domain;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class AdjacentRestorePlacementTests
{
    [Fact]
    public void OnlyFourAdjacentCellsAreExaminedAndLegalCellIsChosen()
    {
        var visited = new List<TileCoordinate>();
        var origin = new TileCoordinate(6, 7);
        var result = AdjacentRestorePlacement.Find(origin, true, true, tile =>
        {
            visited.Add(tile);
            return tile == new TileCoordinate(7, 7);
        });
        Assert.Equal(new TileCoordinate(7, 7), result);
        Assert.Equal(new[] { new TileCoordinate(6, 8), new TileCoordinate(7, 7) }, visited);
        Assert.Null(AdjacentRestorePlacement.Find(origin, true, true, _ => false));
    }

    [Theory]
    [InlineData(false, true)]
    [InlineData(true, false)]
    public void UnknownOrHardBlockerCannotRelocate(bool ordinaryCollectible, bool otherwiseLegal)
    {
        Assert.Null(AdjacentRestorePlacement.Find(new TileCoordinate(6, 7), ordinaryCollectible,
            otherwiseLegal, _ => throw new Exception("Must not search for hard/unknown blockers")));
    }
}
