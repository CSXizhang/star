using System.Reflection;
using System.Reflection.Emit;
using HarmonyLib;
using StardewAI.Companion.Mod.Adapters;
using StardewValley;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class NativeMilkInventoryScopeTests
{
    [Theory]
    [InlineData(-5, true, true)]
    [InlineData(-5, false, false)]
    [InlineData(-6, true, false)]
    [InlineData(0, true, false)]
    public void GroundScope_RejectsNonEggsAndNonCollectableObjects(int category, bool canGrab, bool expected)
    {
        var obj = new StardewValley.Object { Category = category, CanBeGrabbed = canGrab };
        var scope = typeof(NormalNativeActionAdapter).Assembly.GetType(
            "StardewAI.Companion.Mod.Adapters.NativeAnimalHarvestInventoryScope", throwOnError: true)!;
        bool allowed = (bool)scope.GetMethod("IsGroundEgg", BindingFlags.NonPublic | BindingFlags.Static)!
            .Invoke(null, new object[] { obj })!;
        Assert.Equal(expected, allowed);
    }

    [Theory]
    [InlineData(typeof(StardewValley.Tools.MilkPail), true)]
    [InlineData(typeof(StardewValley.Tools.Shears), true)]
    [InlineData(typeof(StardewValley.Tools.Axe), false)]
    [InlineData(typeof(StardewValley.Tools.WateringCan), false)]
    public void HarvestScope_OnlyAcceptsTheTwoNativeAnimalTools(Type toolType, bool expected)
    {
        var scope = typeof(NormalNativeActionAdapter).Assembly.GetType(
            "StardewAI.Companion.Mod.Adapters.NativeAnimalHarvestInventoryScope", throwOnError: true)!;
        var tool = Activator.CreateInstance(toolType)!;
        bool allowed = (bool)scope.GetMethod("IsSupportedTool", BindingFlags.NonPublic | BindingFlags.Static)!
            .Invoke(null, new[] { tool })!;
        Assert.Equal(expected, allowed);
    }

    [Fact]
    public void InventoryGateRewrite_BindsToInstalledGameMethodWithoutRunningGame()
    {
        var scope = typeof(NormalNativeActionAdapter).Assembly.GetType(
            "StardewAI.Companion.Mod.Adapters.NativeAnimalHarvestInventoryScope", throwOnError: true)!;
        var method = AccessTools.Method(typeof(Farmer), nameof(Farmer.addItemToInventoryBool),
            new[] { typeof(Item), typeof(bool) });
        var harmony = new Harmony("StardewAI.Tests.NativeMilkBinding");
        try
        {
            harmony.Patch(method, transpiler: new HarmonyMethod(scope, "Transpile"));
            Assert.Contains(harmony.Id, Harmony.GetPatchInfo(method).Owners);
        }
        finally
        {
            harmony.Unpatch(method, HarmonyPatchType.All, harmony.Id);
        }
    }

    private static IEnumerable<CodeInstruction> Rewrite(List<CodeInstruction> input)
    {
        var scope = typeof(NormalNativeActionAdapter).Assembly.GetType(
            "StardewAI.Companion.Mod.Adapters.NativeAnimalHarvestInventoryScope", throwOnError: true)!;
        return (IEnumerable<CodeInstruction>)scope.GetMethod("Transpile", BindingFlags.NonPublic | BindingFlags.Static)!
            .Invoke(null, new object[] { input })!;
    }

    [Fact]
    public void InventoryGateRewrite_PreservesNativeBodyAndBranchLabels()
    {
        var gate = AccessTools.PropertyGetter(typeof(Farmer), nameof(Farmer.IsLocalPlayer));
        var generator = new DynamicMethod("labels", typeof(void), Type.EmptyTypes).GetILGenerator();
        var label = generator.DefineLabel();
        var call = new CodeInstruction(OpCodes.Call, gate);
        call.labels.Add(label);
        var code = new List<CodeInstruction> {
            new(OpCodes.Ldarg_0), call, new(OpCodes.Brfalse_S, label), new(OpCodes.Ret)
        };
        var output = Rewrite(code).ToList();
        Assert.Equal(4, output.Count);
        Assert.Same(code[0], output[0]);
        Assert.Same(code[2], output[2]);
        Assert.Same(code[3], output[3]);
        Assert.Equal(label, Assert.Single(output[1].labels));
        Assert.Equal("IsNativeOrScopedReceiver", ((MethodInfo)output[1].operand).Name);
    }

    [Theory]
    [InlineData(0)]
    [InlineData(2)]
    public void InventoryGateRewrite_RejectsUnknownNativeShape(int gateCount)
    {
        var gate = AccessTools.PropertyGetter(typeof(Farmer), nameof(Farmer.IsLocalPlayer));
        var code = Enumerable.Range(0, gateCount).Select(_ => new CodeInstruction(OpCodes.Call, gate)).ToList();
        var error = Assert.Throws<TargetInvocationException>(() => Rewrite(code));
        Assert.IsType<InvalidOperationException>(error.InnerException);
    }
}
