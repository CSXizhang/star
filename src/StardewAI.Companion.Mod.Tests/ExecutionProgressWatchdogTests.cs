using Microsoft.Xna.Framework;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class ExecutionProgressWatchdogTests
{
    [Fact]
    public void MovingWorkCanExceedTheOldTotalDurationThenStillDetectAStall()
    {
        var actor = new MechanicsActor("progress");
        var watchdog = new ExecutionProgressWatchdog();
        for (int tick = 0; tick <= 10000; tick++)
        {
            actor.PixelPosition = new Vector2(tick, 0);
            Assert.False(watchdog.HasTimedOut(tick, 3600, actor, 0));
        }
        Assert.False(watchdog.HasTimedOut(13600, 3600, actor, 0));
        Assert.True(watchdog.HasTimedOut(13601, 3600, actor, 0));
        watchdog.Reset();
        Assert.False(watchdog.HasTimedOut(0, 3600, actor, 0));
    }

    [Fact]
    public void CompletedTargetsAndNativeResourceChangesAreProgressWithoutMovement()
    {
        var actor = new MechanicsActor("stationary-work", stamina: 270f, water: 40);
        var watchdog = new ExecutionProgressWatchdog();
        Assert.False(watchdog.HasTimedOut(0, 3600, actor, 0));
        Assert.False(watchdog.HasTimedOut(3500, 3600, actor, 1));
        actor.WaterLeft--;
        Assert.False(watchdog.HasTimedOut(7000, 3600, actor, 1));
        actor.Stamina--;
        Assert.False(watchdog.HasTimedOut(10500, 3600, actor, 1));
        Assert.True(watchdog.HasTimedOut(14101, 3600, actor, 1));
    }
}
