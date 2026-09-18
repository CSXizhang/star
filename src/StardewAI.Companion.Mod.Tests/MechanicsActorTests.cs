using StardewAI.Companion.Mod.Domain;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class MechanicsActorTests
{
    [Fact]
    public void InitialActor_HasFullResourcesAndDefaultPose()
    {
        var actor = new MechanicsActor();
        Assert.Equal(270f, actor.Stamina);
        Assert.Equal(40, actor.Water);
        Assert.False(actor.IsExhausted);
        Assert.False(actor.IsWateringCanEmpty);
        Assert.Equal("Farm", actor.Pose.LocationName);
        Assert.Equal(FacingDirection.Down, actor.Pose.Facing);
    }

    [Fact]
    public void TryConsumeStamina_DeductsAndDetectsExhaustion()
    {
        var actor = new MechanicsActor("test", stamina: 10f, maxStamina: 10f);

        // Valid consumption
        bool success1 = actor.TryConsumeStamina(4.5f, out string? error1);
        Assert.True(success1);
        Assert.Null(error1);
        Assert.Equal(5.5f, actor.Stamina);
        Assert.False(actor.IsExhausted);

        // Insufficient stamina
        bool success2 = actor.TryConsumeStamina(6.0f, out string? error2);
        Assert.False(success2);
        Assert.NotNull(error2);
        Assert.Equal(5.5f, actor.Stamina);

        // Consume remaining to zero
        bool success3 = actor.TryConsumeStamina(5.5f, out string? error3);
        Assert.True(success3);
        Assert.Null(error3);
        Assert.Equal(0f, actor.Stamina);
        Assert.True(actor.IsExhausted);
    }

    [Fact]
    public void TryConsumeWater_DeductsAndDetectsEmptyCan()
    {
        var actor = new MechanicsActor("test", water: 2, maxWater: 40);

        Assert.True(actor.TryConsumeWater(1, out _));
        Assert.Equal(1, actor.Water);
        Assert.False(actor.IsWateringCanEmpty);

        Assert.True(actor.TryConsumeWater(1, out _));
        Assert.Equal(0, actor.Water);
        Assert.True(actor.IsWateringCanEmpty);

        // Attempting to consume when empty
        Assert.False(actor.TryConsumeWater(1, out string? error));
        Assert.NotNull(error);
        Assert.Equal(0, actor.Water);
    }

    [Fact]
    public void UpdatePose_UpdatesAuthoritatively()
    {
        var actor = new MechanicsActor();
        actor.UpdatePose("Farm", new TileCoordinate(70, 25), FacingDirection.Right);

        Assert.Equal("Farm", actor.Pose.LocationName);
        Assert.Equal(new TileCoordinate(70, 25), actor.Pose.Tile);
        Assert.Equal(FacingDirection.Right, actor.Pose.Facing);
    }

    [Fact]
    public void MechanicsActor_ToolLifecyclePhases_ProgressesDeterministically()
    {
        var actor = new MechanicsActor("test-double");
        actor.Face(FacingDirection.Down);

        Assert.False(actor.IsUsingTool);
        Assert.Equal(ToolAnimationPhase.None, actor.AnimationPhase);

        actor.BeginUsingTool();
        Assert.True(actor.IsUsingTool);
        Assert.Equal(ToolAnimationPhase.Windup, actor.AnimationPhase);
        Assert.Equal(160, actor.CurrentFrame);

        // Windup (ticks 1-3)
        actor.UpdateToolAnimation(null, 1);
        Assert.Equal(ToolAnimationPhase.Windup, actor.AnimationPhase);
        actor.UpdateToolAnimation(null, 2);
        Assert.Equal(ToolAnimationPhase.Windup, actor.AnimationPhase);
        actor.UpdateToolAnimation(null, 3);
        Assert.Equal(ToolAnimationPhase.Windup, actor.AnimationPhase);

        // EffectPoint (tick 4)
        actor.UpdateToolAnimation(null, 4);
        Assert.Equal(ToolAnimationPhase.EffectPoint, actor.AnimationPhase);
        Assert.Equal(161, actor.CurrentFrame);

        // FollowThrough (ticks 5-8)
        for (int i = 5; i <= 8; i++)
        {
            actor.UpdateToolAnimation(null, i);
            Assert.Equal(ToolAnimationPhase.FollowThrough, actor.AnimationPhase);
            Assert.Equal(162, actor.CurrentFrame);
        }

        // Completed (tick > 8)
        actor.UpdateToolAnimation(null, 9);
        Assert.Equal(ToolAnimationPhase.Completed, actor.AnimationPhase);

        actor.EndUsingTool();
        Assert.False(actor.IsUsingTool);
        Assert.Equal(ToolAnimationPhase.None, actor.AnimationPhase);
    }
}
