using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>
/// Initial companion setup wizard (single-screen).
/// Shown on first interaction or when the player reopens it from the life menu.
/// Allows setting companion name, play-style, personality, care frequency, and
/// reviewing the daily spend limit.
///
/// Layout inherits the same conventions as <see cref="CompanionCommandMenu"/>:
/// <see cref="IClickableMenu"/>, game-native TextBox, self-drawn buttons,
/// Esc preserves draft, receiveKeyPress / receiveLeftClick / receiveScrollWheelAction.
/// </summary>
public sealed class CompanionSetupMenu : IClickableMenu
{
    public const string MenuTitle = "伙伴初次设置";

    // -----------------------------------------------------------------------
    // Play-style options
    // -----------------------------------------------------------------------

    private static readonly (string Key, string Label, string Tooltip)[] PlayStyles = new[]
    {
        ("earn",      "优先赚钱",            "收获出货、按需补种，遵守每日购买上限"),
        ("workhorse", "任劳任怨",            "浇水除草收获、喂动物、收机器成品等日常杂务"),
        ("community", "社区献祭（规划中）",  "规划中能力，本版本不会自动完成"),
        ("decor",     "农场装修（规划中）",  "规划中能力，本版本不会自动完成"),
    };

    private static readonly (string Key, string Label)[] Personalities = new[]
    {
        ("gentle",   "温柔"),
        ("lively",   "活泼"),
        ("calm",     "沉稳"),
        ("tsundere", "嘴硬心软"),
    };

    private static readonly (string Key, string Label)[] CareFrequencies = new[]
    {
        ("quiet",    "安静"),
        ("moderate", "适中"),
        ("chatty",   "健谈"),
    };

    // -----------------------------------------------------------------------
    // Callbacks
    // -----------------------------------------------------------------------

    private readonly Action<string, string, string, string> _onSave;       // name, playStyle, personality, careFreq
    private readonly Action<string, string, string, string> _onStart;      // same, but also triggers §1.8
    private readonly Action _onSkip;
    private readonly Action? _onClose;

    // -----------------------------------------------------------------------
    // Draft state
    // -----------------------------------------------------------------------

    private TextBox _nameBox = null!;
    private string _playStyle;
    private string _personality;
    private string _careFrequency;
    private readonly int _dailySpendLimit;
    private readonly string _currentWorkMode;
    private readonly LifeMenuUiState? _liveState;
    private bool _draftTouched;   // user edited the draft before/while the profile reply was pending
    private bool _formSynced;     // one-time refresh of the draft once the real profile arrives

    /// <summary>
    /// Whether the real profile has been loaded from <c>life.profile.state</c>. Until then
    /// the form shows defaults and saving must stay disabled to avoid overwriting the
    /// stored profile with placeholder values.
    /// </summary>
    private bool ProfileLoaded => _liveState?.HasProfileState ?? true;

    // -----------------------------------------------------------------------
    // Layout rectangles
    // -----------------------------------------------------------------------

    private Rectangle _nameBoxRect;
    private Rectangle _saveButtonRect;
    private Rectangle _startButtonRect;
    private Rectangle _skipButtonRect;
    private Rectangle _closeButtonRect;

    private readonly Rectangle[] _playStyleRects = new Rectangle[4];
    private readonly Rectangle[] _personalityRects = new Rectangle[4];
    private readonly Rectangle[] _careFreqRects = new Rectangle[3];

    private int _layoutWidth;
    private int _layoutHeight;

    /// <summary>
    /// Creates the setup menu pre-populated with the player's current profile values.
    /// </summary>
    /// <param name="liveState">
    /// Live UI state used to gate saving until the first <c>life.profile.state</c> reply
    /// arrives, and to refresh the draft with the real values once it does. Until then the
    /// form only shows defaults, so save/start must stay disabled ("加载中") to avoid
    /// overwriting the stored profile with placeholder values.
    /// </param>
    public CompanionSetupMenu(
        string companionName,
        string playStyle,
        string personality,
        string careFrequency,
        int dailySpendLimit,
        string currentWorkMode,
        LifeMenuUiState? liveState,
        Action<string, string, string, string> onSave,
        Action<string, string, string, string> onStart,
        Action onSkip,
        Action? onClose = null)
        : base(
            Math.Max(0, (Game1.uiViewport.Width - 600) / 2),
            Math.Max(0, (Game1.uiViewport.Height - 520) / 2),
            Math.Min(600, Math.Max(480, Game1.uiViewport.Width - 32)),
            Math.Min(520, Math.Max(440, Game1.uiViewport.Height - 32)))
    {
        _playStyle = playStyle;
        _personality = personality;
        _careFrequency = careFrequency;
        _dailySpendLimit = dailySpendLimit;
        _currentWorkMode = currentWorkMode;
        _liveState = liveState;
        _onSave = onSave ?? throw new ArgumentNullException(nameof(onSave));
        _onStart = onStart ?? throw new ArgumentNullException(nameof(onStart));
        _onSkip = onSkip ?? throw new ArgumentNullException(nameof(onSkip));
        _onClose = onClose;

        BuildLayout(companionName);
    }

    private void BuildLayout(string initialName)
    {
        _layoutWidth = Game1.uiViewport.Width;
        _layoutHeight = Game1.uiViewport.Height;

        int w = Math.Min(600, Math.Max(480, Game1.uiViewport.Width - 32));
        int h = Math.Min(520, Math.Max(440, Game1.uiViewport.Height - 32));
        width = w;
        height = h;
        xPositionOnScreen = Math.Max(0, (Game1.uiViewport.Width - w) / 2);
        yPositionOnScreen = Math.Max(0, (Game1.uiViewport.Height - h) / 2);

        Texture2D? tbTex = null;
        try { tbTex = Game1.content.Load<Texture2D>(@"LooseSprites\textBox"); } catch { }

        if (_nameBox is null)
        {
            _nameBox = new TextBox(tbTex, null, Game1.smallFont, Game1.textColor)
            {
                limitWidth = false,
                textLimit = 12
            };
        }
        _nameBox.Text = initialName;
        _nameBox.X = xPositionOnScreen + 140;
        _nameBox.Y = yPositionOnScreen + 72;
        _nameBox.Width = 200;
        _nameBox.Height = 44;
        _nameBoxRect = new Rectangle(_nameBox.X, _nameBox.Y, _nameBox.Width, _nameBox.Height);

        // Play-style buttons (2 columns × 2 rows)
        int psY = yPositionOnScreen + 154;
        int psW = (w - 64) / 2;
        for (int i = 0; i < 4; i++)
            _playStyleRects[i] = new Rectangle(xPositionOnScreen + 24 + (i % 2) * (psW + 8), psY + (i / 2) * 44, psW, 36);

        // Personality buttons (4 in one row)
        int perY = yPositionOnScreen + 264;
        int perW = (w - 64) / 4;
        for (int i = 0; i < 4; i++)
            _personalityRects[i] = new Rectangle(xPositionOnScreen + 24 + i * (perW + 4), perY, perW, 36);

        // Care frequency buttons (3 in one row)
        int cfY = yPositionOnScreen + 330;
        int cfW = (w - 64) / 3;
        for (int i = 0; i < 3; i++)
            _careFreqRects[i] = new Rectangle(xPositionOnScreen + 24 + i * (cfW + 8), cfY, cfW, 36);

        // Action buttons
        int btnY = yPositionOnScreen + h - 60;
        _startButtonRect = new Rectangle(xPositionOnScreen + 24, btnY, 180, 44);
        _saveButtonRect = new Rectangle(xPositionOnScreen + 216, btnY, 130, 44);
        _skipButtonRect = new Rectangle(xPositionOnScreen + 358, btnY, 100, 44);
        _closeButtonRect = new Rectangle(xPositionOnScreen + w - 40, yPositionOnScreen + 12, 28, 28);
    }

    public override void gameWindowSizeChanged(Rectangle oldBounds, Rectangle newBounds)
    {
        base.gameWindowSizeChanged(oldBounds, newBounds);
        BuildLayout(_nameBox?.Text ?? "阿星");
    }

    public override void receiveKeyPress(Keys key)
    {
        if (key == Keys.Escape)
        {
            exitThisMenu(playSound: false);
            return;
        }
        base.receiveKeyPress(key);
    }

    public override void receiveLeftClick(int x, int y, bool playSound = true)
    {
        base.receiveLeftClick(x, y, playSound);

        // Close button
        if (_closeButtonRect.Contains(x, y))
        {
            exitThisMenu(playSound: false);
            return;
        }

        // Name box
        if (_nameBoxRect.Contains(x, y))
        {
            _draftTouched = true;
            _nameBox.SelectMe();
            if (Game1.keyboardDispatcher != null)
                Game1.keyboardDispatcher.Subscriber = _nameBox;
            return;
        }

        // Play-style selection
        for (int i = 0; i < PlayStyles.Length; i++)
        {
            if (_playStyleRects[i].Contains(x, y))
            {
                _draftTouched = true;
                _playStyle = PlayStyles[i].Key;
                return;
            }
        }

        // Personality selection
        for (int i = 0; i < Personalities.Length; i++)
        {
            if (_personalityRects[i].Contains(x, y))
            {
                _draftTouched = true;
                _personality = Personalities[i].Key;
                return;
            }
        }

        // Care frequency selection
        for (int i = 0; i < CareFrequencies.Length; i++)
        {
            if (_careFreqRects[i].Contains(x, y))
            {
                _draftTouched = true;
                _careFrequency = CareFrequencies[i].Key;
                return;
            }
        }

        // While the real profile has not arrived, saving would write defaults over the
        // stored settings — the start/save buttons stay disabled ("加载中").
        if (!ProfileLoaded) return;

        // Start together (only for earn/workhorse)
        if (_startButtonRect.Contains(x, y))
        {
            bool canStart = _playStyle is "earn" or "workhorse";
            if (!canStart)
            {
                Game1.addHUDMessage(new HUDMessage("献祭/装修为规划中能力，本版本不会自动完成。请选择赚钱或任劳任怨后再开始。", HUDMessage.error_type));
                return;
            }
            string name = GetValidName();
            _onStart(name, _playStyle, _personality, _careFrequency);
            exitThisMenu(playSound: false);
            return;
        }

        // Save only
        if (_saveButtonRect.Contains(x, y))
        {
            string name = GetValidName();
            _onSave(name, _playStyle, _personality, _careFrequency);
            exitThisMenu(playSound: false);
            return;
        }

        // Skip
        if (_skipButtonRect.Contains(x, y))
        {
            _onSkip();
            exitThisMenu(playSound: false);
            return;
        }
    }

    public override void receiveScrollWheelAction(int direction)
    {
        base.receiveScrollWheelAction(direction);
    }

    public override void draw(SpriteBatch b)
    {
        if (_layoutWidth != Game1.uiViewport.Width || _layoutHeight != Game1.uiViewport.Height)
            BuildLayout(_nameBox?.Text ?? "阿星");

        // One-time refresh: once the real profile.state arrives, adopt its values —
        // unless the player already edited the draft while waiting.
        if (ProfileLoaded && !_formSynced && !_draftTouched && _liveState != null)
        {
            _formSynced = true;
            _playStyle = _liveState.PlayStyle;
            _personality = _liveState.Personality;
            _careFrequency = _liveState.CareFrequency;
            _nameBox!.Text = _liveState.CompanionName;
        }

        // Background tint
        b.Draw(Game1.fadeToBlackRect, Game1.graphics.GraphicsDevice.Viewport.Bounds, Color.Black * 0.45f);
        drawTextureBox(b, xPositionOnScreen, yPositionOnScreen, width, height, Color.White);

        // Title
        b.DrawString(Game1.dialogueFont, MenuTitle,
            new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 12), Game1.textColor);
        b.DrawString(Game1.smallFont, "X",
            new Vector2(_closeButtonRect.X + 8, _closeButtonRect.Y + 2), Color.Red);

        // Name field
        b.DrawString(Game1.smallFont, "伙伴名字：",
            new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 80), Game1.textColor);
        _nameBox!.Draw(b);
        b.DrawString(Game1.smallFont, "（1–12字符）",
            new Vector2(_nameBox.X + _nameBox.Width + 8, yPositionOnScreen + 80), Color.DimGray);

        // Play-style heading
        b.DrawString(Game1.smallFont, "玩法偏好：",
            new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 120), Game1.textColor);
        for (int i = 0; i < PlayStyles.Length; i++)
        {
            bool selected = PlayStyles[i].Key == _playStyle;
            Color btnColor = selected ? Color.ForestGreen : Color.SlateGray;
            DrawButton(b, _playStyleRects[i], PlayStyles[i].Label, btnColor,
                _playStyleRects[i].Contains(Game1.getOldMouseX(), Game1.getOldMouseY()));
        }

        // Personality heading
        b.DrawString(Game1.smallFont, "伙伴性格：",
            new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 236), Game1.textColor);
        for (int i = 0; i < Personalities.Length; i++)
        {
            bool selected = Personalities[i].Key == _personality;
            Color btnColor = selected ? Color.SteelBlue : Color.SlateGray;
            DrawButton(b, _personalityRects[i], Personalities[i].Label, btnColor,
                _personalityRects[i].Contains(Game1.getOldMouseX(), Game1.getOldMouseY()));
        }

        // Care frequency heading
        b.DrawString(Game1.smallFont, "日常关怀频率：",
            new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 302), Game1.textColor);
        for (int i = 0; i < CareFrequencies.Length; i++)
        {
            bool selected = CareFrequencies[i].Key == _careFrequency;
            Color btnColor = selected ? Color.DarkOrchid : Color.SlateGray;
            DrawButton(b, _careFreqRects[i], CareFrequencies[i].Label, btnColor,
                _careFreqRects[i].Contains(Game1.getOldMouseX(), Game1.getOldMouseY()));
        }

        // Daily spend limit (read-only display)
        int limitY = yPositionOnScreen + 378;
        string limitLabel = _dailySpendLimit > 0
            ? $"每日购买上限：{_dailySpendLimit} 金（可在 F8 设置中修改）"
            : "每日购买上限：未设置（可在 F8 设置中修改）";
        b.DrawString(Game1.smallFont, limitLabel,
            new Vector2(xPositionOnScreen + 24, limitY), Color.DimGray);

        // Work-mode note
        string modeNote = _currentWorkMode == "free"
            ? "当前：自由模式已开启"
            : "当前：指令模式";
        b.DrawString(Game1.smallFont, modeNote,
            new Vector2(xPositionOnScreen + 24, limitY + 28), Color.DimGray);

        // Loading gate: profile.state has not arrived yet — saving would overwrite
        // the stored settings with the default form values.
        if (!ProfileLoaded)
        {
            b.DrawString(Game1.smallFont, "正在加载现有伙伴设置… 到达前请勿保存。",
                new Vector2(xPositionOnScreen + 24, limitY + 56), Color.DarkOrange);
        }

        // Action buttons
        bool canStart = _playStyle is "earn" or "workhorse";
        int mx = Game1.getOldMouseX(), my = Game1.getOldMouseY();

        DrawButton(b, _startButtonRect, ProfileLoaded ? "开始一起生活" : "加载中…",
            ProfileLoaded && canStart ? Color.ForestGreen : Color.Gray,
            ProfileLoaded && _startButtonRect.Contains(mx, my));

        DrawButton(b, _saveButtonRect, ProfileLoaded ? "仅保存" : "加载中…",
            ProfileLoaded ? Color.SteelBlue : Color.Gray,
            ProfileLoaded && _saveButtonRect.Contains(mx, my));

        DrawButton(b, _skipButtonRect, "跳过",
            Color.SlateGray, _skipButtonRect.Contains(mx, my));

        // Tooltip for planning styles
        if (_playStyle is "community" or "decor")
        {
            int ttY = yPositionOnScreen + height - 104;
            b.DrawString(Game1.smallFont, "提示：献祭/装修为规划中能力，本版本不会自动完成；",
                new Vector2(xPositionOnScreen + 24, ttY), Color.DarkOrange);
            b.DrawString(Game1.smallFont, "选后仍可保存偏好，但「开始一起生活」需选其他模式。",
                new Vector2(xPositionOnScreen + 24, ttY + 22), Color.DarkOrange);
        }

        drawMouse(b);
    }

    private string GetValidName()
    {
        string raw = _nameBox.Text.Trim();
        return string.IsNullOrEmpty(raw) ? "阿星" : raw[..Math.Min(raw.Length, 12)];
    }

    protected override void cleanupBeforeExit()
    {
        base.cleanupBeforeExit();
        if (Game1.keyboardDispatcher?.Subscriber == _nameBox)
            Game1.keyboardDispatcher.Subscriber = null;
        _nameBox.Selected = false;
        _onClose?.Invoke();
    }

    private static void DrawButton(SpriteBatch b, Rectangle rect, string text, Color baseColor, bool hovered)
    {
        Color bg = hovered ? Color.Lerp(baseColor, Color.White, 0.25f) : baseColor;
        b.Draw(Game1.fadeToBlackRect, rect, bg * 0.85f);
        Vector2 sz = Game1.smallFont.MeasureString(text);
        b.DrawString(Game1.smallFont, text,
            new Vector2(rect.X + (rect.Width - sz.X) / 2f, rect.Y + (rect.Height - sz.Y) / 2f),
            Color.White);
    }
}
