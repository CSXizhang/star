using System.Reflection;
using System.Runtime.Serialization;
using HarmonyLib;
using StardewValley;
using StardewAI.Companion.Mod.Domain;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class NativeCompanionAnimationTests
{
    [Theory]
    [InlineData(FacingDirection.Down, 32)]
    [InlineData(FacingDirection.Right, 40)]
    [InlineData(FacingDirection.Up, 48)]
    [InlineData(FacingDirection.Left, 56)]
    public void RunningUsesNativeRunningCycle(FacingDirection facing, int index) =>
        Assert.Equal(index, NativeCompanionAnimation.MovementIndex(facing, true));

    private sealed class HeadlessSprite : FarmerSprite
    {
        public override void UpdateSourceRect() { }
    }
    private sealed class HeadlessFarmer : Farmer
    {
        public override int FacingDirection { get; set; }
    }

    [Fact]
    public void RepeatingSameFacingDoesNotResetNativeAnimationTimer()
    {
        var farmer = (HeadlessFarmer)FormatterServices.GetUninitializedObject(typeof(HeadlessFarmer));
        var sprite = (HeadlessSprite)FormatterServices.GetUninitializedObject(typeof(HeadlessSprite));
        AccessTools.Field(typeof(AnimatedSprite), "currentAnimation").SetValue(sprite,
            new List<FarmerSprite.AnimationFrame>());
        AccessTools.Field(typeof(Character), "sprite").SetValue(farmer, new Netcode.NetRef<AnimatedSprite>(sprite));
        farmer.FacingDirection = 1;
        var actor = new FarmerMechanicsActor("same-facing");
        AccessTools.Field(typeof(FarmerMechanicsActor), "_gameFarmer").SetValue(actor, farmer);
        sprite.setCurrentFrame(8);
        sprite.timer = 150;
        for (int i = 0; i < 30; i++) actor.Face(FacingDirection.Right);
        Assert.Equal(150, sprite.timer);
        Assert.Equal(4, sprite.CurrentAnimation.Count);
    }

    [Fact]
    public void NativeWalkingCycleHasDistinctFramesAndStopRemainsNeutral()
    {
        var sprite = (HeadlessSprite)FormatterServices.GetUninitializedObject(typeof(HeadlessSprite));
        AccessTools.Field(typeof(AnimatedSprite), "currentAnimation").SetValue(sprite,
            new List<FarmerSprite.AnimationFrame>());
        var frames = new HashSet<int>();
        sprite.setCurrentFrame(0);
        for (int i = 0; i < 30; i++)
        {
            // Exercise the native frame advance without its separate audio hook,
            // which requires a running game sound engine.
            AccessTools.Method(typeof(FarmerSprite), "currentAnimationTick").Invoke(sprite, null);
            frames.Add(sprite.CurrentFrame);
        }
        Assert.True(frames.Count >= 3);
        NativeCompanionAnimation.Stand(sprite, FacingDirection.Down);
        Assert.Equal(0, sprite.CurrentFrame);
    }

    [Theory]
    [InlineData(FacingDirection.Down, 0)]
    [InlineData(FacingDirection.Right, 6)]
    [InlineData(FacingDirection.Up, 12)]
    [InlineData(FacingDirection.Left, 6)]
    public void StopUsesNativeNeutralPoseRatherThanRaisedFoot(FacingDirection facing, int frame)
    {
        var sprite = (HeadlessSprite)FormatterServices.GetUninitializedObject(typeof(HeadlessSprite));
        AccessTools.Field(typeof(AnimatedSprite), "currentAnimation").SetValue(sprite,
            new List<FarmerSprite.AnimationFrame>());
        sprite.getAnimationFromIndex(NativeCompanionAnimation.WalkingIndex(facing), sprite, 100, 1, false, false);
        Assert.NotEqual(frame, sprite.CurrentAnimation[0].frame);
        sprite.PauseForSingleAnimation = true;
        NativeCompanionAnimation.Stand(sprite, facing);
        Assert.Equal(frame, sprite.CurrentFrame);
        Assert.False(sprite.PauseForSingleAnimation);
        Assert.Equal(facing == FacingDirection.Left, sprite.CurrentAnimationFrame.flip);
    }

    [Theory]
    [InlineData(FacingDirection.Up, 16)]
    [InlineData(FacingDirection.Right, 8)]
    [InlineData(FacingDirection.Down, 0)]
    [InlineData(FacingDirection.Left, 24)]
    public void WalkingUsesTheNativeDirectionalAnimationIndexes(FacingDirection facing, int index) =>
        Assert.Equal(index, NativeCompanionAnimation.WalkingIndex(facing));

    [Fact]
    public void ToolTickCallsFarmerSpecificSingleAnimationEntry()
    {
        var instructions = PatchProcessor.GetOriginalInstructions(
            AccessTools.Method(typeof(FarmerMechanicsActor), nameof(FarmerMechanicsActor.UpdateToolAnimation)));
        Assert.Contains(instructions, i => i.Calls(AccessTools.Method(typeof(FarmerSprite), nameof(FarmerSprite.checkForSingleAnimation))));
        Assert.DoesNotContain(instructions, i => i.operand is MethodInfo method &&
            method.DeclaringType == typeof(AnimatedSprite) && method.Name == nameof(AnimatedSprite.animateOnce));
    }

    [Fact]
    public void NativeWateringCompletionWithUsingToolStillSetDoesNotWaitForever()
    {
        // Reproduce the real native final frame after doneWithAnimation clears
        // PauseForSingleAnimation. The detached canMoveNow callback has not
        // cleared UsingTool, so the native isOnToolAnimation query remains true.
        var farmer = (Farmer)FormatterServices.GetUninitializedObject(typeof(Farmer));
        var sprite = (FarmerSprite)FormatterServices.GetUninitializedObject(typeof(FarmerSprite));
        AccessTools.Field(typeof(Farmer), "usingTool").SetValue(farmer, new Netcode.NetBool(true));
        AccessTools.Field(typeof(Character), "sprite").SetValue(farmer, new Netcode.NetRef<AnimatedSprite>(sprite));
        AccessTools.Field(typeof(FarmerSprite), "owner").SetValue(sprite, farmer);
        AccessTools.Field(typeof(FarmerSprite), "currentSingleAnimation").SetValue(sprite, 180);
        AccessTools.Field(typeof(AnimatedSprite), "currentAnimation").SetValue(sprite,
            new List<FarmerSprite.AnimationFrame> { new(62, 0), new(62, 75), new(63, 100), new(46, 500) });
        sprite.currentFrame = 46;
        sprite.currentAnimationIndex = 3;
        sprite.interval = 125;
        var actor = new FarmerMechanicsActor("native-final-frame");
        AccessTools.Field(typeof(FarmerMechanicsActor), "_gameFarmer").SetValue(actor, farmer);
        AccessTools.Field(typeof(FarmerMechanicsActor), "_isUsingTool").SetValue(actor, true);
        Assert.False(sprite.PauseForSingleAnimation);
        Assert.True(sprite.isOnToolAnimation());
        Assert.NotNull(sprite.CurrentAnimation);
        Assert.Equal(ToolAnimationPhase.Completed, actor.UpdateToolAnimation(null, 1));
    }

    [Fact]
    public void FoodAnimationStartsNativeFramesWithoutRunningConsumption()
    {
        var instructions = PatchProcessor.GetOriginalInstructions(
            AccessTools.Method(typeof(FarmerMechanicsActor), nameof(FarmerMechanicsActor.BeginEating)));
        Assert.Contains(instructions, i => i.Calls(AccessTools.Method(typeof(FarmerSprite), nameof(FarmerSprite.animateOnce),
            new[] { typeof(int), typeof(float), typeof(int) })));
        Assert.DoesNotContain(instructions, i => i.Calls(AccessTools.Method(typeof(Farmer), nameof(Farmer.doneEating))));
    }
}
