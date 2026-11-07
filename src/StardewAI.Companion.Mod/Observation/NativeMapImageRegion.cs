using Microsoft.Xna.Framework;

namespace StardewAI.Companion.Mod.Observation;

/// <summary>Scope the native screenshot property and restore it even on failure.</summary>
public static class NativeMapImageRegion
{
    public static T Capture<T>(xTile.Map map, Rectangle? requested, Func<Rectangle, T> capture)
    {
        var layer = map.Layers[0];
        var area = new Rectangle(0, 0, layer.LayerWidth, layer.LayerHeight);
        bool hadRegion = map.Properties.TryGetValue("ScreenshotRegion", out var original);
        if (requested is Rectangle selected)
            area = selected;
        else if (hadRegion)
        {
            var corners = (original?.ToString() ?? throw new InvalidOperationException("Missing native ScreenshotRegion."))
                .Split(' ', StringSplitOptions.RemoveEmptyEntries).Select(int.Parse).ToArray();
            if (corners.Length != 4) throw new InvalidOperationException("Invalid native ScreenshotRegion.");
            area = new Rectangle(corners[0], corners[1], corners[2] - corners[0] + 1, corners[3] - corners[1] + 1);
        }
        if (area.X < 0 || area.Y < 0 || area.Width <= 0 || area.Height <= 0
            || (long)area.X + area.Width > layer.LayerWidth || (long)area.Y + area.Height > layer.LayerHeight)
            throw new ArgumentOutOfRangeException(nameof(requested), "Image region must be inside the current map.");
        try
        {
            if (requested.HasValue)
                map.Properties["ScreenshotRegion"] = $"{area.X} {area.Y} {area.Right - 1} {area.Bottom - 1}";
            return capture(area);
        }
        finally
        {
            if (hadRegion) map.Properties["ScreenshotRegion"] = original;
            else map.Properties.Remove("ScreenshotRegion");
        }
    }
}
