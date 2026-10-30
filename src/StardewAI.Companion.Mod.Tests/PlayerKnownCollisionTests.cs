using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;
using StardewAI.Companion.Mod.PlayerMode;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class PlayerKnownCollisionTests
{
    [Fact]
    public void WalkingQueryReachesTheBoundActorInsteadOfInterfaceDefault()
    {
        var failure = new InvalidOperationException("actor binding unavailable");
        var native = new GameWorldObserver(new TestMonitor())
        {
            NativeCollisionActor = () => throw failure
        };
        IWorldObserver observer = new PlayerKnownObserver(native, new PlayerKnowledgeTracker());
        Assert.Same(failure, Assert.Throws<InvalidOperationException>(() =>
            observer.CanWalkBetween("Farm", new TileCoordinate(1, 1), new TileCoordinate(2, 1))));
    }
}
