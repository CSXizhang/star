using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.BellsAndWhistles;
using StardewValley.Menus;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>Stardew's portrait dialogue, with its text area fitted to the UI viewport.</summary>
public sealed class CompanionNpcDialogueBox : DialogueBox
{
    private Action? _onDisplayed;
    public CompanionNpcDialogueBox(Dialogue dialogue, Action? onDisplayed = null) : base(dialogue)
    {
        _onDisplayed = onDisplayed;
        FitToViewport(dialogue.getCurrentDialogue());
    }

    public override void draw(SpriteBatch b)
    {
        base.draw(b);
        if (characterIndexInDialogue <= 0 && getCurrentString().Length > 0) return;
        var displayed = _onDisplayed;
        _onDisplayed = null;
        displayed?.Invoke();
    }

    public override void receiveKeyPress(Keys key)
    {
        if (key == Keys.Enter) { receiveLeftClick(x + width / 2, y + height / 2); return; }
        if (key is Keys.Escape or Keys.F8) { closeDialogue(); return; }
        base.receiveKeyPress(key);
    }

    protected override void cleanupBeforeExit()
    {
        base.cleanupBeforeExit();
        if (!Game1.eventUp)
        {
            Game1.dialogueUp = false;
            if (Game1.player != null && !Game1.player.UsingTool) Game1.player.CanMove = true;
        }
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

    // Embedded conversation owns its lifetime. Advancing the final page must
    // leave the text and reply controls on screen instead of closing the menu.
    public void AdvanceConversationPage()
    {
        if (characterIndexInDialogue < getCurrentString().Length - 1)
            characterIndexInDialogue = getCurrentString().Length - 1;
        else if (characterDialoguesBrokenUp.Count > 1)
        {
            characterDialoguesBrokenUp.Pop();
            characterIndexInDialogue = 0;
        }
    }
    public void PlaceConversation(int top) => y = top;

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
