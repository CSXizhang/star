using Microsoft.Xna.Framework;
using StardewAI.Companion.Mod.Menus;
using Xunit;

public class CompanionCommandMenuPauseStateTests
{
    [Fact]
    public void WhenAutonomyPausedIsTrue_BadgeIsVisible_TextIsCorrect_AndStatusColorIsDarkGoldenrod()
    {
        CompanionCommandMenu.AutonomyPaused = true;
        try
        {
            Assert.True(CompanionCommandMenu.IsPausedBadgeVisible);
            Assert.Equal("已暂停", CompanionCommandMenu.PausedBadgeText);
            Assert.Equal("已暂停", CompanionCommandMenu.PausedText);

            // Status color is DarkGoldenrod regardless of processing or state text
            Assert.Equal(Color.DarkGoldenrod, CompanionCommandMenu.GetStatusColor(isProcessing: false, currentStatusText: "待命", isPaused: true));
            Assert.Equal(Color.DarkGoldenrod, CompanionCommandMenu.GetStatusColor(isProcessing: true, currentStatusText: "正在处理...", isPaused: true));
            Assert.Equal(Color.DarkGoldenrod, CompanionCommandMenu.GetStatusColor(isProcessing: false, currentStatusText: "失败", isPaused: true));
        }
        finally
        {
            CompanionCommandMenu.AutonomyPaused = false;
        }
    }

    [Fact]
    public void WhenAutonomyPausedIsFalse_BadgeIsHidden_AndStatusColorFollowsProcessingOrState()
    {
        CompanionCommandMenu.AutonomyPaused = false;

        Assert.False(CompanionCommandMenu.IsPausedBadgeVisible);
        Assert.Null(CompanionCommandMenu.PausedBadgeText);

        Assert.Equal(Color.DarkOrange, CompanionCommandMenu.GetStatusColor(isProcessing: true, currentStatusText: "正在处理...", isPaused: false));
        Assert.Equal(Color.Red, CompanionCommandMenu.GetStatusColor(isProcessing: false, currentStatusText: "失败", isPaused: false));
        Assert.Equal(Color.DarkGreen, CompanionCommandMenu.GetStatusColor(isProcessing: false, currentStatusText: "就绪", isPaused: false));
        Assert.Equal(Color.DarkGreen, CompanionCommandMenu.GetStatusColor(isProcessing: false, currentStatusText: "待命", isPaused: false));
    }

    [Theory]
    [InlineData(480, 100, 50)]
    [InlineData(600, 100, 50)]
    [InlineData(720, 100, 50)]
    public void PausedBadgeRect_DimensionsAndSeparationFromDetailButton(int width, int x, int y)
    {
        var badgeRect = CompanionCommandMenu.GetPausedBadgeRect(x, y, width);
        Assert.Equal(y + 50, badgeRect.Y);
        Assert.Equal(28, badgeRect.Height);
        Assert.Equal(84, badgeRect.Width);

        // Detail button is positioned at x + width - 140, width 116.
        int detailButtonX = x + width - 140;
        // Verify badge sits to the left of detail button with exact 8px spacing
        Assert.Equal(detailButtonX, badgeRect.Right + 8);

        // Verify status text maximum width leaves at least 8px spacing before badge
        int maxStatusWidth = CompanionCommandMenu.GetStatusMaxTextWidth(width, isPaused: true);
        int statusTextStartX = x + 24;
        Assert.True(statusTextStartX + maxStatusWidth <= badgeRect.X);

        // Unpaused width should be wider
        int maxUnpausedWidth = CompanionCommandMenu.GetStatusMaxTextWidth(width, isPaused: false);
        Assert.True(maxUnpausedWidth > maxStatusWidth);
    }

    [Fact]
    public void ModeAndPauseStatesRemainDecoupledAndReadable()
    {
        CompanionCommandMenu.AutonomyMode = "command";
        CompanionCommandMenu.ModeChangePending = false;
        CompanionCommandMenu.AutonomyPaused = false;

        Assert.Equal("开启自由模式", CompanionCommandMenu.ModeButtonText);
        Assert.False(CompanionCommandMenu.IsPausedBadgeVisible);

        // Pausing autonomy preserves mode button text
        CompanionCommandMenu.AutonomyPaused = true;
        Assert.Equal("开启自由模式", CompanionCommandMenu.ModeButtonText);
        Assert.True(CompanionCommandMenu.IsPausedBadgeVisible);

        // Switching to free mode preserves pause state
        CompanionCommandMenu.AutonomyMode = "free";
        Assert.Equal("退出自由模式", CompanionCommandMenu.ModeButtonText);
        Assert.True(CompanionCommandMenu.IsPausedBadgeVisible);

        // Unpausing autonomy preserves mode
        CompanionCommandMenu.AutonomyPaused = false;
        Assert.Equal("退出自由模式", CompanionCommandMenu.ModeButtonText);
        Assert.False(CompanionCommandMenu.IsPausedBadgeVisible);

        // Cleanup
        CompanionCommandMenu.AutonomyMode = "command";
        CompanionCommandMenu.AutonomyPaused = false;
    }
}
