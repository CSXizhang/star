using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class AdjacentTargetWalkTests
{
    [Fact]
    public void PlayerEnteringPlannedRouteCausesReplanWithoutWalkingThroughPlayer()
    {
        var start = new TileCoordinate(10, 10);
        var target = new TileCoordinate(10, 15);
        var actor = new MechanicsActor(initialPose: new AuthoritativePose("Farm", start, FacingDirection.Down));
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var next = navigator.FindPathToInteract(actor, "Farm", target).Steps.First(step => step.Tile != start).Tile;
        var walk = new AdjacentTargetWalk(actor, observer, navigator);
        Assert.True(walk.Plan("Farm", target));
        observer.PlayerTile = next;

        var state = AdjacentWalkState.Walking;
        for (var tick = 0; tick < 1000 && state == AdjacentWalkState.Walking; tick++)
        {
            state = walk.Tick("Farm", target);
            Assert.NotEqual(next, actor.Tile);
        }
        Assert.Equal(AdjacentWalkState.Facing, state);
        Assert.True(actor.Tile.IsAdjacentTo(target));
    }

    [Fact]
    public void BlockedReplanDoesNotMoveActorOrPretendTargetWasReached()
    {
        var start = new TileCoordinate(10, 10);
        var target = new TileCoordinate(10, 15);
        var actor = new MechanicsActor(initialPose: new AuthoritativePose("Farm", start, FacingDirection.Down));
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var next = navigator.FindPathToInteract(actor, "Farm", target).Steps.First(step => step.Tile != start).Tile;
        var walk = new AdjacentTargetWalk(actor, observer, navigator);
        Assert.True(walk.Plan("Farm", target));
        observer.PlayerTile = next;
        foreach (var tile in new[] { new TileCoordinate(9, 15), new TileCoordinate(11, 15),
                                     new TileCoordinate(10, 14), new TileCoordinate(10, 16) })
            observer.SetPassable(tile, false);
        var position = actor.PixelPosition;
        var state = AdjacentWalkState.Walking;
        for (var tick = 0; tick < 5 && state == AdjacentWalkState.Walking; tick++)
            state = walk.Tick("Farm", target);
        Assert.Equal(AdjacentWalkState.Unreachable, state);
        Assert.Equal(position, actor.PixelPosition);
    }
}
