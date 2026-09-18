using StardewAI.Companion.Mod.Menus;
using Xunit;

public class ChatScrollMetricsTests
{
    [Theory]
    [InlineData(3000, 180, 2820)] // One long message must scroll through all its pixels.
    [InlineData(80, 180, 0)]
    [InlineData(180, 180, 0)]
    public void OverflowIsPixelBased(int content, int viewport, int expected) =>
        Assert.Equal(expected, ChatScrollMetrics.Maximum(content, viewport));
    [Fact]
    public void NewMessageKeepsOldViewportAnchor() =>
        Assert.Equal(600, ChatScrollMetrics.PreserveAnchor(400, 1000, 1200, 200));
    [Fact]
    public void FollowingNewestStaysAtBottom() =>
        Assert.Equal(0, ChatScrollMetrics.PreserveAnchor(0, 1000, 1200, 200));
    [Fact]
    public void ReflowClampsToAvailableContent() =>
        Assert.Equal(0, ChatScrollMetrics.PreserveAnchor(400, 1000, 100, 200));
    [Fact]
    public void SingleLongMessageHasDraggableThumb() =>
        Assert.InRange(ChatScrollMetrics.ThumbHeight(180, 180, 3000),24,179);
}
