using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Navigation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class SameMapNavigatorTests
{
    [Fact]
    public void CrossMapMovement_IsStrictlyForbidden()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);
        var pose = new AuthoritativePose("Farm", 10, 10, FacingDirection.Down);

        var result = navigator.FindPathToInteract(pose, "Forest", new TileCoordinate(15, 15));

        Assert.False(result.Success);
        Assert.Contains("Cross-map navigation forbidden", result.ErrorMessage);
    }

    [Fact]
    public void FindPathToInteract_FindsAdjacentPassableTileAndFacing()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);

        // Actor at (10, 10), target at (10, 12)
        // Standing at (10, 11) facing Down (South) allows watering (10, 12)
        var pose = new AuthoritativePose("Farm", 10, 10, FacingDirection.Down);
        var target = new TileCoordinate(10, 12);

        var result = navigator.FindPathToInteract(pose, "Farm", target);

        Assert.True(result.Success);
        Assert.NotEmpty(result.Steps);

        var finalStep = result.Steps[^1];
        Assert.True(finalStep.Tile.IsAdjacentTo(target));
        Assert.Equal(FacingDirection.Down, finalStep.Facing);
    }

    [Fact]
    public void Pathfinding_AvoidsObstacles()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);

        // Block direct path from (5, 5) to (5, 7): place obstacle at (5, 6)
        observer.SetPassable(new TileCoordinate(5, 6), false);

        var pose = new AuthoritativePose("Farm", 5, 5, FacingDirection.Down);
        var destination = new TileCoordinate(5, 7);

        var result = navigator.FindPath("Farm", pose.Tile, destination);

        Assert.True(result.Success);
        Assert.DoesNotContain(result.Steps, s => s.Tile == new TileCoordinate(5, 6));
        Assert.Equal(destination, result.Steps[^1].Tile);
    }

    [Fact]
    public void TargetCompletelyEnclosed_FailsWithoutTeleporting()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);

        var target = new TileCoordinate(20, 20);
        // Block all 4 cardinal neighbors
        observer.SetPassable(new TileCoordinate(20, 19), false);
        observer.SetPassable(new TileCoordinate(21, 20), false);
        observer.SetPassable(new TileCoordinate(20, 21), false);
        observer.SetPassable(new TileCoordinate(19, 20), false);

        var pose = new AuthoritativePose("Farm", 10, 10, FacingDirection.Down);
        var result = navigator.FindPathToInteract(pose, "Farm", target);

        Assert.False(result.Success);
        Assert.Contains("no passable adjacent standing positions", result.ErrorMessage);
    }

    [Fact]
    public void FindNearestPassableReachableTile_FindsClosestPassableNeighbor()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);

        // Center at (43, 57) is impassable
        observer.SetPassable(new TileCoordinate(43, 57), false);
        // North (43, 56) is impassable
        observer.SetPassable(new TileCoordinate(43, 56), false);
        // South (43, 58) is passable
        observer.SetPassable(new TileCoordinate(43, 58), true);

        var start = new TileCoordinate(43, 60);
        var result = navigator.FindNearestPassableReachableTile("Farm", start, new TileCoordinate(43, 57), maxRadius: 3);

        Assert.True(result.HasValue);
        Assert.Equal(new TileCoordinate(43, 58), result.Value.Tile);
        Assert.True(result.Value.Path.Success);
    }

    [Fact]
    public void FindNearestPassableReachableTile_ReturnsNullWhenAllCandidatesBlocked()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);

        var center = new TileCoordinate(43, 57);
        for (int dx = -3; dx <= 3; dx++)
        {
            for (int dy = -3; dy <= 3; dy++)
            {
                observer.SetPassable(new TileCoordinate(center.X + dx, center.Y + dy), false);
            }
        }

        var start = new TileCoordinate(43, 65);
        var result = navigator.FindNearestPassableReachableTile("Farm", start, center, maxRadius: 3);

        Assert.Null(result);
    }

    [Fact]
    public void Planning_TreatsPlayerOccupiedTileAsObstacle_LikeWalking()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);

        // Player stands on the only sane standing tile south of the target.
        observer.PlayerTile = new TileCoordinate(10, 11);

        var pose = new AuthoritativePose("Farm", 10, 10, FacingDirection.Down);
        var result = navigator.FindPathToInteract(pose, "Farm", new TileCoordinate(10, 12));

        Assert.True(result.Success);
        Assert.DoesNotContain(result.Steps, s => s.Tile == new TileCoordinate(10, 11));
    }

    [Fact]
    public void Planning_WhenPlayerBlocksEveryStandingTile_FailsInsteadOfRoutingThroughPlayer()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);

        var target = new TileCoordinate(10, 12);
        observer.PlayerTile = new TileCoordinate(10, 11); // north candidate
        observer.SetPassable(new TileCoordinate(11, 12), false); // east
        observer.SetPassable(new TileCoordinate(10, 13), false); // south
        observer.SetPassable(new TileCoordinate(9, 12), false);  // west

        var pose = new AuthoritativePose("Farm", 10, 10, FacingDirection.Down);
        var result = navigator.FindPathToInteract(pose, "Farm", target);

        Assert.False(result.Success);
        Assert.Contains("no passable adjacent standing positions", result.ErrorMessage);
    }

    [Fact]
    public void FindPath_WithAvoidTiles_NeverRoutesThroughAvoidedTiles()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);

        var avoided = new TileCoordinate(10, 11);
        var result = navigator.FindPath(
            "Farm",
            new TileCoordinate(10, 10),
            new TileCoordinate(10, 14),
            avoidTiles: new[] { avoided });

        Assert.True(result.Success);
        Assert.DoesNotContain(result.Steps, s => s.Tile == avoided);
    }
}
