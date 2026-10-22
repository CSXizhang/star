using Microsoft.Xna.Framework;
using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class CompanionDashboardLayoutTests
{
    [Theory]
    [InlineData(1920, 1080, 2)]
    [InlineData(1280, 720, 2)]
    [InlineData(960, 540, 2)]
    [InlineData(800, 600, 2)]
    [InlineData(640, 480, 1)]
    [InlineData(320, 320, 1)]
    public void WindowAndControlsStayInsideUiViewport(int width, int height, int columns)
    {
        var layout = new CompanionDashboardLayout(width, height);
        Assert.True(new Rectangle(0, 0, width, height).Contains(layout.Bounds));
        Assert.InRange(Math.Abs(layout.Bounds.Center.X - width / 2), 0, 1);
        Assert.InRange(Math.Abs(layout.Bounds.Center.Y - height / 2), 0, 1);
        Assert.Equal(columns, layout.FieldColumns);
        Assert.True(layout.Body.Width > 0 && layout.Body.Height > 0);
        Assert.True(layout.Bounds.Contains(layout.Body));
        Assert.True(layout.Bounds.Contains(layout.Close));
        Assert.True(layout.Bounds.Contains(layout.Footer));
        foreach (var tab in layout.Tabs)
        {
            Assert.True(layout.Bounds.Contains(tab));
            Assert.False(tab.Intersects(layout.Body));
            Assert.False(tab.Intersects(layout.Close));
        }
        Assert.False(layout.Body.Intersects(layout.Footer));
        for (int i = 1; i < layout.Tabs.Length; i++) Assert.False(layout.Tabs[i - 1].Intersects(layout.Tabs[i]));
    }
}
