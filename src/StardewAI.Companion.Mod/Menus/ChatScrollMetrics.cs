namespace StardewAI.Companion.Mod.Menus;

/// <summary>Pixel scrolling independent of message count and graphics resources.</summary>
public static class ChatScrollMetrics
{
    public static int Maximum(int contentHeight, int viewportHeight) => Math.Max(0, contentHeight - viewportHeight);
    public static int PreserveAnchor(int offset, int previousHeight, int contentHeight, int viewportHeight) =>
        offset == 0 ? 0 : Math.Clamp(offset + contentHeight - previousHeight, 0, Maximum(contentHeight, viewportHeight));
    public static int ThumbHeight(int track, int viewport, int content) =>
        Math.Min(track, Math.Max(24, track * viewport / Math.Max(1, content)));
}
