using Microsoft.Xna.Framework;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class FarmRegionPlannerTests
{
    [Fact]
    public void PathDistanceAccountsForBarrierAndUnreachableGround()
    {
        var ground = new HashSet<TileCoordinate>();
        for (int x = 0; x < 5; x++) for (int y = 0; y < 5; y++)
            if (x != 2 || y == 4) ground.Add(new(x, y));
        ground.Add(new(9, 9));
        var paths = FarmRegionPlanner.Distances(ground, new[] { new TileCoordinate(0, 0) });
        Assert.Equal(12, paths[new TileCoordinate(4, 0)]);
        Assert.False(paths.ContainsKey(new TileCoordinate(9, 9)));
    }

    [Fact]
    public void PrioritizesExistingConnectedSoilAndHonorsSavedBounds()
    {
        var cells = new Dictionary<TileCoordinate, string>();
        for (int x = 1; x < 29; x++) for (int y = 1; y < 12; y++)
            cells[new(x, y)] = x >= 18 && x < 26 && y >= 3 && y < 8 ? "deadCrop" : "tillable";
        var paths = FarmRegionPlanner.Distances(cells.Keys.ToHashSet(), new[] { new TileCoordinate(1, 1) });
        var choices = FarmRegionPlanner.Candidates(cells, new(), paths, null);
        Assert.InRange(choices.Count, 1, 3);
        Assert.True(choices[0].Contains(new Point(18, 3)));
        var saved = new Rectangle(18, 3, 8, 5);
        Assert.Equal(new[] { saved }, FarmRegionPlanner.Candidates(cells, new(), paths, saved));
    }
}
