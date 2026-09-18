using System.Runtime.CompilerServices;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>Unscaled SpriteFont text clipped without changing the caller's batch state.</summary>
public static class ClippedSpriteText
{
    private static readonly ConditionalWeakTable<SpriteFont, Dictionary<char, SpriteFont.Glyph>> Glyphs = new();

    public static void Draw(SpriteBatch batch, SpriteFont font, string text, Vector2 position, Color color, Rectangle viewport)
    {
        var glyphs = Glyphs.GetValue(font, f => f.GetGlyphs());
        // Match the local MonoGame DrawString(font, string, position, color) overload.
        position = new Vector2((int)position.X, (int)position.Y);
        var offset = Vector2.Zero;
        bool first = true;
        foreach (char character in text)
        {
            if (character == '\r') continue;
            if (character == '\n')
            {
                offset.X = 0;
                offset.Y += font.LineSpacing;
                first = true;
                continue;
            }
            if (!glyphs.TryGetValue(character, out var glyph))
            {
                if (font.DefaultCharacter is not char fallback || !glyphs.TryGetValue(fallback, out glyph))
                    throw new ArgumentException("Text contains a character unavailable in the current SpriteFont.", nameof(text));
            }
            offset.X = first ? Math.Max(glyph.LeftSideBearing, 0) : offset.X + font.Spacing + glyph.LeftSideBearing;
            first = false;
            var destination = position + offset + new Vector2(glyph.Cropping.X, glyph.Cropping.Y);
            var source = glyph.BoundsInTexture;
            if (Clip(ref destination, ref source, viewport))
                batch.Draw(font.Texture, destination, source, color);
            offset.X += glyph.Width + glyph.RightSideBearing;
        }
    }

    public static bool Clip(ref Vector2 position, ref Rectangle source, Rectangle viewport)
    {
        int left = Math.Max(0, (int)Math.Ceiling(viewport.Left - position.X));
        int top = Math.Max(0, (int)Math.Ceiling(viewport.Top - position.Y));
        int right = Math.Min(source.Width, (int)Math.Floor(viewport.Right - position.X));
        int bottom = Math.Min(source.Height, (int)Math.Floor(viewport.Bottom - position.Y));
        if (right <= left || bottom <= top) return false;
        position += new Vector2(left, top);
        source = new Rectangle(source.X + left, source.Y + top, right - left, bottom - top);
        return true;
    }
}
