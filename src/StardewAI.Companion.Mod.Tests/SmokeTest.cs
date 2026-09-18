using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>
/// Executable vertical slice tests verifying continuous tick-driven movement,
/// tool swing animation lifecycle, actual independent resource consumption,
/// and post-condition soil verification.
/// </summary>
public class SmokeTest
{
    [Fact]
    public void OneTileVerticalSlice_TickMovement_ToolSwing_ResourceConsumption_And_Verification()
    {
        // 1. Setup World and Actor
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var startTile = new TileCoordinate(10, 10);
        var initialPose = new AuthoritativePose("Farm", startTile, FacingDirection.Down);
        var actor = new MechanicsActor(
            companionId: "companion-alpha",
            stamina: 270.0f,
            maxStamina: 270.0f,
            water: 40,
            maxWater: 40,
            initialPose: initialPose);

        var avatar = new CompanionAvatar(actor);
        var navigator = new SameMapNavigator(observer);
        var adapter = new TestWateringCanAdapter(observer);
        var machine = new WaterZoneStateMachine(actor, observer, navigator, adapter, avatar);

        // 2. Setup dry tilled soil at (10, 12).
        // Actor at (10, 10) must physically walk 1 tile south to (10, 11), face South, and water (10, 12).
        var targetTile = new TileCoordinate(10, 12);
        observer.SetDirt(targetTile, TileDirtState.DryDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-slice-001",
            TaskId: "task-slice-001",
            LocationId: "Farm",
            TargetTiles: new[] { targetTile },
            MaxStamina: 50.0f,
            MaxWater: 10,
            MaxGameMinutes: 60
        );

        // 3. Initiate state machine
        bool started = machine.Start(request, out var earlyResult);
        Assert.True(started);
        Assert.Null(earlyResult);
        Assert.Equal(ExecutionState.Navigating, machine.CurrentState);
        Assert.True(machine.IsExecuting);
        Assert.Equal("task-slice-001", actor.ActiveTaskId);

        // 4. Physical continuous movement over multiple ticks
        // Distance is 64 pixels. At 3.5 px/tick, takes ~19 ticks.
        for (int i = 0; i < 15; i++)
        {
            machine.Update(null, i + 1);
            Assert.Equal(ExecutionState.Navigating, machine.CurrentState);
            // Confirm actor is physically in between (10, 10) and (10, 11)
            Assert.True(actor.PixelPosition.Y > 10 * 64);
            Assert.True(actor.PixelPosition.Y < 11 * 64);
        }

        // Advance until arrival at interaction tile (10, 11)
        int maxTicks = 100;
        int tickCounter = 16;
        while (machine.CurrentState == ExecutionState.Navigating && tickCounter < maxTicks)
        {
            machine.Update(null, tickCounter++);
        }

        // 5. Verify arrival and transition to Facing -> Watering
        Assert.Equal(new TileCoordinate(10, 11), actor.Tile);

        // Advance into tool swing
        while (machine.CurrentState == ExecutionState.Facing && tickCounter < maxTicks)
        {
            machine.Update(null, tickCounter++);
        }

        Assert.Equal(ExecutionState.Watering, machine.CurrentState);
        Assert.Equal(FacingDirection.Down, actor.Facing);
        Assert.Equal("watering", avatar.CurrentAnimation);

        // Advance tool swing animation to the lifecycle effect point
        while (machine.CurrentState == ExecutionState.Watering && actor.AnimationPhase < ToolAnimationPhase.EffectPoint && tickCounter < maxTicks)
        {
            machine.Update(null, tickCounter++);
        }

        // Resources are deducted at the real lifecycle effect point
        Assert.Equal(268.0f, actor.Stamina);
        Assert.Equal(39, actor.WaterLeft);


        // 6. Complete watering animation ticks and verification
        while (machine.IsExecuting && tickCounter < maxTicks)
        {
            machine.Update(null, tickCounter++);
        }

        // 7. Assert terminal execution state
        Assert.False(machine.IsExecuting);
        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);
        Assert.Equal("idle", avatar.CurrentAnimation);
        Assert.Null(actor.ActiveTaskId);

        var finalResult = machine.FinalResult;
        Assert.NotNull(finalResult);
        Assert.Equal(ExecutionState.Succeeded, finalResult.FinalState);
        Assert.Single(finalResult.WateredTiles);
        Assert.Equal(targetTile, finalResult.WateredTiles[0]);
        Assert.Empty(finalResult.SkippedTiles);
        Assert.Empty(finalResult.FailedTiles);
        Assert.Equal(2.0f, finalResult.StaminaUsed);
        Assert.Equal(1, finalResult.WaterUsed);

        // 8. Ground truth verification in the world
        Assert.True(observer.GetDirtState("Farm", targetTile).IsWatered);
    }
}
