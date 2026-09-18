using System.Reflection;
using Microsoft.Xna.Framework;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class NativeAnimalDoorRulesTests
{
    [Theory]
    [InlineData(0, 0, 0, 0, 0, 0, false)]
    [InlineData(640, 640, 64, 64, 10, 10, true)]
    [InlineData(640, 640, 128, 64, 11, 10, true)]
    [InlineData(640, 640, 128, 64, 12, 10, false)]
    [InlineData(640, 640, 64, 64, 10, 11, false)]
    public void NativeDoorBounds_RejectNonDoorTilesAndRespectMultiTileDoors(
        int x, int y, int width, int height, int tileX, int tileY, bool expected)
    {
        var method = typeof(NormalNativeActionAdapter).GetMethod("IsNativeAnimalDoorTile",
            BindingFlags.Static | BindingFlags.NonPublic)!;
        bool result = (bool)method.Invoke(null, new object[] {
            new Rectangle(x, y, width, height), new TileCoordinate(tileX, tileY) })!;
        Assert.Equal(expected, result);
    }
}
