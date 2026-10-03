using System.Reflection;
using HarmonyLib;
using StardewValley;
using StardewAI.Companion.Mod.Domain;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class NativeCompanionAnimationTests
{
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
    public void FoodAnimationStartsNativeFramesWithoutRunningConsumption()
    {
        var instructions = PatchProcessor.GetOriginalInstructions(
            AccessTools.Method(typeof(FarmerMechanicsActor), nameof(FarmerMechanicsActor.BeginEating)));
        Assert.Contains(instructions, i => i.Calls(AccessTools.Method(typeof(FarmerSprite), nameof(FarmerSprite.animateOnce),
            new[] { typeof(int), typeof(float), typeof(int) })));
        Assert.DoesNotContain(instructions, i => i.Calls(AccessTools.Method(typeof(Farmer), nameof(Farmer.doneEating))));
    }
}
