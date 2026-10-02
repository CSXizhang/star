using System.Reflection;
using System.Runtime.CompilerServices;
using Netcode;
using StardewAI.Companion.Mod.Adapters;
using StardewValley;
using StardewValley.TerrainFeatures;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class NativeDeadCropScytheTests
{
    [Fact]
    public void DeadCropGate_ProtectsLivingCropAndEmptySoil()
    {
        var gate = typeof(NormalNativeActionAdapter).GetMethod("IsDeadCrop", BindingFlags.NonPublic | BindingFlags.Static)!;
        // Only the gate's native net fields are needed. Game constructors load
        // graphics/singletons; this does not pretend to exercise tool execution.
        var crop = (Crop)RuntimeHelpers.GetUninitializedObject(typeof(Crop));
        typeof(Crop).GetField("dead")!.SetValue(crop, new NetBool());
        typeof(Crop).GetField("<NetFields>k__BackingField", BindingFlags.NonPublic | BindingFlags.Instance)!
            .SetValue(crop, new NetFields("test-crop").SetOwner(crop));
        var dirt = (HoeDirt)RuntimeHelpers.GetUninitializedObject(typeof(HoeDirt));
        typeof(HoeDirt).GetField("netCrop", BindingFlags.NonPublic | BindingFlags.Instance)!
            .SetValue(dirt, new NetRef<Crop>());
        Assert.False((bool)gate.Invoke(null, new object?[] { null })!);
        Assert.False((bool)gate.Invoke(null, new object[] { dirt })!);
        dirt.crop = crop;
        Assert.False((bool)gate.Invoke(null, new object[] { dirt })!);
        Assert.Same(crop, dirt.crop);
        crop.dead.Value = true;
        Assert.True((bool)gate.Invoke(null, new object[] { dirt })!);
        Assert.Same(crop, dirt.crop);
    }
}
