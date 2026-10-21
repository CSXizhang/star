using System;
using Microsoft.Xna.Framework.Graphics;
using StardewModdingAPI;
using StardewValley;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>
/// The companion's whole UI is Chinese, but the save may run under the default
/// (English) content language: then Game1.smallFont/dialogueFont are the Latin
/// SpriteFonts with no CJK glyphs, and SpriteText uses the Latin bitmap font, so
/// every Chinese string renders as blanks. While a save is loaded we therefore
/// switch to the game's own native Chinese assets: the zh-CN SpriteFonts shipped
/// in Content/Fonts and the zh language code that makes SpriteText use the native
/// Chinese bitmap font (and per-character wrapping). Originals are restored on
/// return to title so nothing outside the loaded session is affected.
/// </summary>
public static class ChineseUiLocale
{
    private static SpriteFont? _previousSmallFont;
    private static SpriteFont? _previousDialogueFont;
    private static LocalizedContentManager.LanguageCode? _previousLanguage;
    private static bool _applied;

    public static void Apply(Action<string, LogLevel> log)
    {
        if (_applied) return;
        var previousLanguage = LocalizedContentManager.CurrentLanguageCode;
        SpriteFont? chineseSmall = null;
        SpriteFont? chineseDialogue = null;
        try
        {
            chineseSmall = Game1.content.Load<SpriteFont>("Fonts\\SmallFont.zh-CN");
            chineseDialogue = Game1.content.Load<SpriteFont>("Fonts\\SpriteFont1.zh-CN");
        }
        catch (Exception ex)
        {
            log($"Chinese UI fonts unavailable ({ex.Message}); keeping game fonts.", LogLevel.Warn);
        }
        _previousSmallFont = Game1.smallFont;
        _previousDialogueFont = Game1.dialogueFont;
        _previousLanguage = previousLanguage;
        if (chineseSmall != null) Game1.smallFont = chineseSmall;
        if (chineseDialogue != null) Game1.dialogueFont = chineseDialogue;
        LocalizedContentManager.CurrentLanguageCode = LocalizedContentManager.LanguageCode.zh;
        _applied = true;
        string coverage = chineseSmall == null
            ? "zh-CN SpriteFonts missing"
            : $"smallFont glyphs={chineseSmall.Characters.Count} has中={chineseSmall.Characters.Contains('中')} hasA={chineseSmall.Characters.Contains('A')}";
        log($"Chinese UI locale active (was {previousLanguage}); {coverage}.", LogLevel.Info);
    }

    public static void Restore()
    {
        if (!_applied) return;
        _applied = false;
        if (_previousSmallFont != null) Game1.smallFont = _previousSmallFont;
        if (_previousDialogueFont != null) Game1.dialogueFont = _previousDialogueFont;
        if (_previousLanguage is { } language) LocalizedContentManager.CurrentLanguageCode = language;
        _previousSmallFont = null;
        _previousDialogueFont = null;
        _previousLanguage = null;
    }
}
