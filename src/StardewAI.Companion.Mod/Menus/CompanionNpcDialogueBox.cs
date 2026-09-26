using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.BellsAndWhistles;
using StardewValley.Menus;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>Stardew's portrait dialogue, with its text area fitted to the UI viewport.</summary>
public sealed class CompanionNpcDialogueBox : DialogueBox
{
    public CompanionNpcDialogueBox(Dialogue dialogue) : base(dialogue)
    {
        FitToViewport(dialogue.getCurrentDialogue());
    }

    public static string LiteralText(string text)
    {
        // Model speech is content, never Stardew dialogue commands (including item gifts).
        return CompanionDialogueInbox.PlainText(text).Replace('$', '＄').Replace('#', '＃')
            .Replace('%', '％').Replace('@', '＠').Replace('^', '＾').Replace('¦', '｜')
            .Replace('{', '｛').Replace('}', '｝').Replace('[', '［').Replace(']', '］')
            .Replace('*', '＊').Replace("\r", "").Replace('\n', ' ');
    }

    public static int PanelWidth(int viewportWidth) => Math.Min(1200, Math.Max(512, viewportWidth - 64));

    private void FitToViewport(string? fullText = null)
    {
        width = PanelWidth(Game1.uiViewport.Width);
        x = (Game1.uiViewport.Width - width) / 2;
        y = Math.Max(16, Game1.uiViewport.Height - height - 64);
        if (!isQuestion)
        {
            // Native portrait layout reserves 460px for the portrait and 20px padding.
            // Re-page the remaining text after narrowing; never split at sentence boundaries.
            var remaining = fullText ?? string.Concat(characterDialoguesBrokenUp);
            var sections = SpriteText.getStringBrokenIntoSectionsOfHeight(remaining, width - 480, height - 16);
            characterDialoguesBrokenUp.Clear();
            for (int i = sections.Count - 1; i >= 0; i--) characterDialoguesBrokenUp.Push(sections[i]);
        }
    }

    public override void gameWindowSizeChanged(Rectangle oldBounds, Rectangle newBounds)
    {
        base.gameWindowSizeChanged(oldBounds, newBounds);
        FitToViewport();
    }
}
