using Microsoft.Xna.Framework;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>UI-viewport geometry, shared by rendering and hit testing.</summary>
public sealed class CompanionDashboardLayout
{
    public Rectangle Bounds { get; }
    public Rectangle Body { get; }
    public Rectangle Footer { get; }
    public Rectangle Close { get; }
    public Rectangle[] Tabs { get; }
    public int FieldColumns => Body.Width >= 560 ? 2 : 1;

    public CompanionDashboardLayout(int viewportWidth, int viewportHeight)
    {
        int width = Math.Min(840, Math.Max(240, viewportWidth - 48));
        int height = Math.Min(660, Math.Max(300, viewportHeight - 48));
        width = Math.Min(width, viewportWidth);
        height = Math.Min(height, viewportHeight);
        Bounds = new((viewportWidth - width) / 2, (viewportHeight - height) / 2, width, height);
        Body = new(Bounds.X + 28, Bounds.Y + 148, width - 76, height - 252);
        Footer = new(Bounds.X + 28, Bounds.Bottom - 88, width - 56, 68);
        Close = new(Bounds.Right - 60, Bounds.Y + 20, 40, 40);
        int tabWidth = Math.Min(152, (width - 72) / 3);
        Tabs = Enumerable.Range(0, 3).Select(i => new Rectangle(Bounds.X + 28 + i * (tabWidth + 8), Bounds.Y + 84, tabWidth, 44)).ToArray();
    }
}
