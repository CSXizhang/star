using System;
using Microsoft.Xna.Framework;
using StardewAI.Companion.Mod.Observation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class NativeMapImageRegionTests
{
    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void LocalCaptureRestoresNativePropertyEvenWhenCaptureFails(bool existing)
    {
        var map = new xTile.Map();
        map.AddLayer(new xTile.Layers.Layer("Back", map, new xTile.Dimensions.Size(80, 65), new xTile.Dimensions.Size(64, 64)));
        if (existing) map.Properties["ScreenshotRegion"] = "1 2 70 60";
        Assert.Throws<InvalidOperationException>(() => NativeMapImageRegion.Capture<int>(map, new Rectangle(10, 20, 12, 8), area =>
        {
            Assert.Equal("10 20 21 27", map.Properties["ScreenshotRegion"].ToString());
            Assert.Equal(new Rectangle(10, 20, 12, 8), area);
            throw new InvalidOperationException("native capture failure");
        }));
        Assert.Equal(existing, map.Properties.ContainsKey("ScreenshotRegion"));
        if (existing) Assert.Equal("1 2 70 60", map.Properties["ScreenshotRegion"].ToString());
        var whole = NativeMapImageRegion.Capture(map, null, area => area);
        Assert.Equal(existing ? new Rectangle(1, 2, 70, 59) : new Rectangle(0, 0, 80, 65), whole);
        Assert.Throws<ArgumentOutOfRangeException>(() => NativeMapImageRegion.Capture(map, new Rectangle(79, 64, 2, 2), _ => false));
        Assert.Equal(existing, map.Properties.ContainsKey("ScreenshotRegion"));
    }
}
