using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;

namespace StardewAI.Companion.Mod.Menus;

public sealed record ChatMessage(string Speaker, string Text, Color Color, string? TokenInfo = null);

/// <summary>
/// In-game natural language companion chat window (F8).
///
/// Layout constraints:
/// * a fixed two-line progress header: the current action/progress plus an
///   optional, individually toggleable technical tool name / wait reason;
/// * an independent chat area that wraps by real pixel height, is clipped to its
///   viewport, scrolls with the mouse wheel and a scrollbar, and does not force
///   the view to the bottom while the player reads history (new messages raise an
///   unread hint instead);
/// * window resize re-layouts every region instead of overflowing.
/// </summary>
public sealed class CompanionCommandMenu : IClickableMenu
{
    public const string MenuTitle = "星露谷 AI 伙伴对话 (F8)";

    public static readonly List<ChatMessage> ChatHistory = new();
    public static bool IsProcessing { get; set; }
    public static string? CurrentRequestId { get; set; }
    public static string? CurrentRequestSaveId { get; set; }
    public static string CurrentStatusText { get; set; } = "就绪";

    /// <summary>Current action / progress line shown to the player (F8 line 1).</summary>
    public static string? CurrentActionText { get; set; }

    /// <summary>Optional technical tool name (F8 line 2, hidden by default).</summary>
    public static string? CurrentToolName { get; set; }

    /// <summary>Why the persisted plan worker is waiting (F8 line 2 fallback).</summary>
    public static string? PlanWaitReason { get; set; }

    /// <summary>Waiting conditions reported by the plan/overview layer.</summary>
    public static IReadOnlyList<string> WaitingConditions { get; set; } = Array.Empty<string>();

    /// <summary>
    /// Structured progress from the native execution state machine (Q5):
    /// `{action, phase, completed, total, reasonCode}`. Shown on line 2; a missing or
    /// zero total is printed without any fabricated percentage.
    /// </summary>
    public static string? StructuredProgressText { get; set; }

    public static bool ShowTechnicalDetail { get; set; }
    public static bool ModeChangePending { get; set; }
    public static string ModeButtonText => ModeChangePending ? "等待模式确认" : AutonomyMode == "free" ? "退出自由模式" : "开启自由模式";
    public static string AutonomyMode { get; set; } = "command";
    public static bool AutonomyPaused { get; set; }
    public const string PausedText = "已暂停";
    public static bool IsPausedBadgeVisible => AutonomyPaused;
    public static string? PausedBadgeText => AutonomyPaused ? PausedText : null;

    public static Rectangle GetPausedBadgeRect(int xPositionOnScreen, int yPositionOnScreen, int width) =>
        new(xPositionOnScreen + width - 232, yPositionOnScreen + 50, 84, 28);

    public static int GetStatusMaxTextWidth(int width, bool isPaused) => isPaused ? width - 264 : width - 178;

    public static Color GetStatusColor(bool isProcessing, string? currentStatusText, bool isPaused)
    {
        if (isPaused)
            return Color.DarkGoldenrod;
        if (isProcessing)
            return Color.DarkOrange;
        if (currentStatusText == "失败")
            return Color.Red;
        return Color.DarkGreen;
    }

    public static int DailySpendLimit { get; set; }
    public static string BoxPreference { get; set; } = "none";
    public static IReadOnlyList<string> AvailableChestOptions { get; set; } = new[] { "none" };
    public static string? LastTokenInfo { get; set; }

    /// <summary>Starts a new request and resets the visible progress state.</summary>
    public static void BeginRequest(string requestId, string? saveId, string statusText)
    {
        CurrentRequestId = requestId;
        CurrentRequestSaveId = saveId;
        CurrentStatusText = statusText;
        CurrentActionText = null;
        CurrentToolName = null;
        PlanWaitReason = null;
        WaitingConditions = Array.Empty<string>();
        StructuredProgressText = null;
        LastTokenInfo = null;
        IsProcessing = true;
    }

    /// <summary>True when a reply belongs to the request/save currently on screen.</summary>
    public static bool IsCurrentReply(string? requestId, string? saveId)
    {
        if (CurrentRequestId is null || !string.Equals(CurrentRequestId, requestId, StringComparison.Ordinal))
            return false;
        if (!string.IsNullOrEmpty(saveId) && !string.IsNullOrEmpty(CurrentRequestSaveId) &&
            !string.Equals(saveId, CurrentRequestSaveId, StringComparison.Ordinal))
            return false;
        return true;
    }

    private readonly Action<string> _onSubmitText;
    private readonly Action? _onPause;
    private readonly Action? _onResume;
    private readonly Action? _onCancel;
    private readonly Action? _onModeToggle;
    private readonly Action? _onSettings;
    private bool _settingsOpen;

    private TextBox _textBox = null!;
    private Rectangle _sendButtonRect;
    private Rectangle _pauseButtonRect;
    private Rectangle _resumeButtonRect;
    private Rectangle _cancelButtonRect;
    private Rectangle _closeButtonRect;
    private Rectangle _detailButtonRect;
    private Rectangle _chatViewport;
    private Rectangle _scrollTrackRect;
    private Rectangle _scrollThumbRect;

    // Chat scrolling state. 0 = newest message visible at the bottom.
    private int _scrollBack;
    private int _unreadWhileScrolled;
    private int _lastSeenCount;
    private bool _draggingThumb;

    private int _lastContentHeight;
    private int _layoutViewportWidth;
    private int _layoutViewportHeight;
    private const int ChatLineSpacing = 4;

    public CompanionCommandMenu(Action<string> onSubmitText)
        : this(onSubmitText, null, null, null, null, null)
    {
    }

    public CompanionCommandMenu(
        Action<string> onSubmitText,
        Action? onPause = null,
        Action? onResume = null,
        Action? onCancel = null,
        Action? onModeToggle = null,
        Action? onSettings = null)
        : base(
            Math.Max(0, (Game1.uiViewport.Width - 720) / 2),
            Math.Max(0, (Game1.uiViewport.Height - 480) / 2),
            Math.Min(720, Math.Max(480, Game1.uiViewport.Width - 32)),
            Math.Min(480, Math.Max(360, Game1.uiViewport.Height - 32)))
    {
        _onSubmitText = onSubmitText ?? throw new ArgumentNullException(nameof(onSubmitText));
        _onPause = onPause;
        _onResume = onResume;
        _onCancel = onCancel;
        _onModeToggle = onModeToggle;
        _onSettings = onSettings;

        _lastSeenCount = ChatHistory.Count;
        BuildLayout();

        if (ChatHistory.Count == 0)
        {
            ChatHistory.Add(new ChatMessage("系统", "欢迎使用星露谷 AI 伙伴！输入中文自然语言（如：把箱子里的胡萝卜种子种下并浇水），按回车或点击[发送]。", Color.DimGray));
            _lastSeenCount = ChatHistory.Count;
        }
    }

    /// <summary>Rebuilds every rectangle from the current window size.</summary>
    private void BuildLayout()
    {
        _layoutViewportWidth = Game1.uiViewport.Width;
        _layoutViewportHeight = Game1.uiViewport.Height;
        int minWidth = Math.Min(720, Math.Max(480, Game1.uiViewport.Width - 32));
        int minHeight = Math.Min(480, Math.Max(360, Game1.uiViewport.Height - 32));
        width = minWidth;
        height = minHeight;
        xPositionOnScreen = Math.Max(0, (Game1.uiViewport.Width - width) / 2);
        yPositionOnScreen = Math.Max(0, (Game1.uiViewport.Height - height) / 2);

        Texture2D? textBoxTexture = null;
        try
        {
            textBoxTexture = Game1.content.Load<Texture2D>(@"LooseSprites\textBox");
        }
        catch
        {
            // Fallback gracefully to default box rendering if texture load fails
        }

        int inputY = yPositionOnScreen + height - 64;
        if (_textBox is null)
        {
            _textBox = new TextBox(textBoxTexture, null, Game1.smallFont, Game1.textColor)
            {
                limitWidth = false,
                textLimit = 200,
            };
            _textBox.OnEnterPressed += _ => Submit();
        }

        _textBox.X = xPositionOnScreen + 24;
        _textBox.Y = inputY;
        _textBox.Width = Math.Max(200, width - 300);
        _textBox.Height = 44;

        _sendButtonRect = new Rectangle(xPositionOnScreen + width - 258, inputY, 68, 44);
        _pauseButtonRect = new Rectangle(xPositionOnScreen + width - 182, inputY, 52, 44);
        _resumeButtonRect = new Rectangle(xPositionOnScreen + width - 124, inputY, 52, 44);
        _cancelButtonRect = new Rectangle(xPositionOnScreen + width - 66, inputY, 52, 44);
        _closeButtonRect = new Rectangle(xPositionOnScreen + width - 40, yPositionOnScreen + 12, 28, 28);
        _detailButtonRect = new Rectangle(xPositionOnScreen + width - 140, yPositionOnScreen + 50, 116, 28);

        int chatTop = yPositionOnScreen + 168;
        int chatBottom = yPositionOnScreen + height - 76;
        _chatViewport = new Rectangle(xPositionOnScreen + 24, chatTop, width - 64, Math.Max(40, chatBottom - chatTop));

        int trackX = _chatViewport.X + _chatViewport.Width + 6;
        _scrollTrackRect = new Rectangle(trackX, _chatViewport.Y, 12, _chatViewport.Height);
        _scrollThumbRect = _scrollTrackRect;
    }

    public override void gameWindowSizeChanged(Rectangle oldBounds, Rectangle newBounds)
    {
        base.gameWindowSizeChanged(oldBounds, newBounds);
        BuildLayout();
    }

    public override void receiveKeyPress(Keys key)
    {
        if (key == Keys.Escape)
        {
            exitThisMenu(playSound: false);
            return;
        }

        if (key == Keys.Enter)
        {
            Submit();
            return;
        }

        base.receiveKeyPress(key);
    }

    public override void receiveScrollWheelAction(int direction)
    {
        // direction > 0 scrolls up into history, < 0 returns toward the newest message.
        _scrollBack = Math.Clamp(_scrollBack + (direction > 0 ? 64 : -64), 0, MaxScrollBack());
        if (_scrollBack == 0)
            _unreadWhileScrolled = 0;
        base.receiveScrollWheelAction(direction);
    }

    public override void receiveLeftClick(int x, int y, bool playSound = true)
    {
        base.receiveLeftClick(x, y, playSound);

        if (_closeButtonRect.Contains(x, y))
        {
            exitThisMenu(playSound: false);
            return;
        }

        if (_detailButtonRect.Contains(x, y))
        {
            ShowTechnicalDetail = !ShowTechnicalDetail;
            return;
        }

        if (_scrollThumbRect.Contains(x, y) && _scrollTrackRect.Height > _scrollThumbRect.Height)
        {
            _draggingThumb = true;
            return;
        }

        if (_scrollTrackRect.Contains(x, y))
        {
            // Page toward the clicked position on the track.
            double ratio = (double)(y - _scrollTrackRect.Y) / Math.Max(1, _scrollTrackRect.Height);
            _scrollBack = Math.Clamp((int)Math.Round((1 - ratio) * MaxScrollBack()), 0, MaxScrollBack());
            if (_scrollBack == 0)
                _unreadWhileScrolled = 0;
            return;
        }

        if (new Rectangle(xPositionOnScreen + 24, yPositionOnScreen + 118, 150, 32).Contains(x, y))
        {
            _onModeToggle?.Invoke();
            return;
        }
        if (new Rectangle(xPositionOnScreen + 182, yPositionOnScreen + 118, 86, 32).Contains(x, y))
        {
            _settingsOpen = !_settingsOpen;
            _textBox.Text = _settingsOpen ? DailySpendLimit.ToString() : string.Empty;
            _textBox.SelectMe();
            if (Game1.keyboardDispatcher != null) Game1.keyboardDispatcher.Subscriber = _textBox;
            return;
        }
        if (_settingsOpen && new Rectangle(xPositionOnScreen + 280, yPositionOnScreen + 118, 250, 32).Contains(x, y))
        {
            IReadOnlyList<string> options = AvailableChestOptions.Count == 0 ? new[] { "any" } : AvailableChestOptions;
            int index = 0;
            for (int i = 0; i < options.Count; i++)
                if (options[i] == BoxPreference) { index = i; break; }
            BoxPreference = options[(index + 1) % options.Count];
            return;
        }
        if (_settingsOpen && new Rectangle(xPositionOnScreen + 538, yPositionOnScreen + 118, 110, 32).Contains(x, y))
        {
            if (int.TryParse(_textBox.Text, out int limit) && limit >= 0)
            {
                DailySpendLimit = limit;
                _onSettings?.Invoke();
            }
            return;
        }

        if (AutonomyPaused && GetPausedBadgeRect(xPositionOnScreen, yPositionOnScreen, width).Contains(x, y))
        {
            _onResume?.Invoke();
            return;
        }

        if (_sendButtonRect.Contains(x, y))
        {
            Submit();
            return;
        }

        if (_pauseButtonRect.Contains(x, y))
        {
            _onPause?.Invoke();
            return;
        }

        if (_resumeButtonRect.Contains(x, y))
        {
            _onResume?.Invoke();
            return;
        }

        if (_cancelButtonRect.Contains(x, y))
        {
            _onCancel?.Invoke();
            return;
        }

        if (_textBox.X <= x && x <= _textBox.X + _textBox.Width &&
            _textBox.Y <= y && y <= _textBox.Y + _textBox.Height)
        {
            _textBox.SelectMe();
            if (Game1.keyboardDispatcher != null)
            {
                Game1.keyboardDispatcher.Subscriber = _textBox;
            }
        }
    }

    public override void leftClickHeld(int x, int y)
    {
        base.leftClickHeld(x, y);

        if (!_draggingThumb || _scrollTrackRect.Height <= _scrollThumbRect.Height)
            return;

        int maxScroll = MaxScrollBack();
        int travel = _scrollTrackRect.Height - _scrollThumbRect.Height;
        double ratio = (double)(y - _scrollTrackRect.Y - _scrollThumbRect.Height / 2) / Math.Max(1, travel);
        _scrollBack = Math.Clamp((int)Math.Round((1 - Math.Clamp(ratio, 0, 1)) * maxScroll), 0, maxScroll);
        if (_scrollBack == 0)
            _unreadWhileScrolled = 0;
    }

    public override void releaseLeftClick(int x, int y)
    {
        base.releaseLeftClick(x, y);
        _draggingThumb = false;
    }

    private int MaxScrollBack() => ChatScrollMetrics.Maximum(BuildRenderedMessages().Sum(m => m.Height), _chatViewport.Height);

    private void TrackIncomingMessages()
    {
        int count = ChatHistory.Count;
        int contentHeight = BuildRenderedMessages().Sum(m => m.Height);
        if (_scrollBack > 0 && _lastContentHeight > 0)
        {
            // Bottom-relative pixel offset: new content/reflow keeps the top anchor.
            _scrollBack = ChatScrollMetrics.PreserveAnchor(_scrollBack, _lastContentHeight, contentHeight, _chatViewport.Height);
            _unreadWhileScrolled += Math.Max(0, count - _lastSeenCount);
        }
        _lastContentHeight = contentHeight;
        _lastSeenCount = count;
    }

    private sealed record RenderedMessage(string Speaker, string Wrapped, string? TokenInfo, Color Color, int Height);

    private List<RenderedMessage> BuildRenderedMessages()
    {
        int contentWidth = Math.Max(120, _chatViewport.Width - 24);
        var rendered = new List<RenderedMessage>();

        int start = 0;
        for (int i = start; i < ChatHistory.Count; i++)
        {
            var msg = ChatHistory[i];
            string wrapped = Game1.parseText($"[{msg.Speaker}]: {msg.Text ?? string.Empty}", Game1.smallFont, contentWidth);
            string? tokens = string.IsNullOrEmpty(msg.TokenInfo) ? null : Game1.parseText(msg.TokenInfo, Game1.smallFont, contentWidth);
            int height = (int)Game1.smallFont.MeasureString(wrapped).Y + ChatLineSpacing;
            if (!string.IsNullOrEmpty(msg.TokenInfo))
                height += (int)Game1.smallFont.MeasureString(tokens).Y + ChatLineSpacing;
            rendered.Add(new RenderedMessage(msg.Speaker, wrapped, tokens, msg.Color, height));
        }

        return rendered;
    }

    public override void draw(SpriteBatch b)
    {
        if (_layoutViewportWidth != Game1.uiViewport.Width || _layoutViewportHeight != Game1.uiViewport.Height)
            BuildLayout();
        TrackIncomingMessages();

        // 1. Semi-transparent dark background tint
        b.Draw(Game1.fadeToBlackRect, Game1.graphics.GraphicsDevice.Viewport.Bounds, Color.Black * 0.45f);

        // 2. Menu background texture box
        drawTextureBox(b, xPositionOnScreen, yPositionOnScreen, width, height, Color.White);

        // 3. Instruction title & Close [X]
        b.DrawString(Game1.dialogueFont, MenuTitle, new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 12), Game1.textColor);
        b.DrawString(Game1.smallFont, "X", new Vector2(_closeButtonRect.X + 8, _closeButtonRect.Y + 2), Color.Red);

        // 4. Fixed status + two-line progress header.
        Color statusColor = GetStatusColor(IsProcessing, CurrentStatusText, AutonomyPaused);
        string statusPrefix = IsProcessing ? "● [处理中] " : "● [状态] ";
        int maxStatusWidth = GetStatusMaxTextWidth(width, AutonomyPaused);
        b.DrawString(Game1.smallFont, ClipToWidth(statusPrefix + CurrentStatusText, maxStatusWidth), new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 54), statusColor);
        if (AutonomyPaused)
        {
            Rectangle pausedRect = GetPausedBadgeRect(xPositionOnScreen, yPositionOnScreen, width);
            DrawButton(b, pausedRect, PausedText, Color.DarkGoldenrod, pausedRect.Contains(Game1.getOldMouseX(), Game1.getOldMouseY()));
        }
        DrawButton(b, _detailButtonRect, ShowTechnicalDetail ? "隐藏详情" : "显示详情", Color.SteelBlue, _detailButtonRect.Contains(Game1.getOldMouseX(), Game1.getOldMouseY()));

        string actionLine = !string.IsNullOrEmpty(CurrentActionText)
            ? "当前：" + CurrentActionText
            : "当前：" + (IsProcessing ? CurrentStatusText : "待命");
        if (_unreadWhileScrolled > 0)
            actionLine += $"（还有 {_unreadWhileScrolled} 条新消息，滚轮向下查看）";
        b.DrawString(Game1.smallFont, ClipToWidth(actionLine, width - 48), new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 78), Game1.textColor);

        if (ShowTechnicalDetail || !string.IsNullOrEmpty(StructuredProgressText) || !string.IsNullOrEmpty(PlanWaitReason))
        {
            string detail = ShowTechnicalDetail && !string.IsNullOrEmpty(CurrentToolName) ? "工具：" + CurrentToolName : "";
            if (!string.IsNullOrEmpty(StructuredProgressText))
                detail += "　进度：" + StructuredProgressText;
            if (!string.IsNullOrEmpty(PlanWaitReason))
                detail += "　等待：" + PlanWaitReason;
            if (WaitingConditions.Count > 0)
                detail += "　条件：" + string.Join("、", WaitingConditions.Take(3));
            detail = ClipToWidth(detail, width - 48);
            b.DrawString(Game1.smallFont, detail, new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 100), Color.DimGray);
        }

        DrawButton(b, new Rectangle(xPositionOnScreen + 24, yPositionOnScreen + 118, 150, 32),
            ModeButtonText, AutonomyMode == "free" ? Color.SeaGreen : Color.SlateGray, false);
        DrawButton(b, new Rectangle(xPositionOnScreen + 182, yPositionOnScreen + 118, 86, 32), _settingsOpen ? "收起" : "设置", Color.SteelBlue, false);
        if (_settingsOpen)
        {
            b.DrawString(Game1.smallFont, "每日购买上限", new Vector2(xPositionOnScreen + 280, yPositionOnScreen + 98), Game1.textColor);
            DrawButton(b, new Rectangle(xPositionOnScreen + 280, yPositionOnScreen + 118, 250, 32), "箱子：" + BoxPreference + "（点击切换）", Color.SaddleBrown, false);
            DrawButton(b, new Rectangle(xPositionOnScreen + 538, yPositionOnScreen + 118, 110, 32), "保存设置", Color.ForestGreen, false);
        }

        if (!string.IsNullOrEmpty(LastTokenInfo))
        {
            int maxTokenWidth = AutonomyPaused ? width - 240 - 300 : width - 320;
            if (maxTokenWidth > 20)
                b.DrawString(Game1.smallFont, ClipToWidth(LastTokenInfo, maxTokenWidth), new Vector2(xPositionOnScreen + 300, yPositionOnScreen + 54), Color.Teal);
        }

        // 5. Divider line
        b.Draw(Game1.fadeToBlackRect, new Rectangle(xPositionOnScreen + 24, yPositionOnScreen + 156, width - 48, 2), Color.Gray * 0.5f);

        // 6. Independent, clipped, scrollable chat area.
        DrawChatArea(b);

        // 7. Input Box
        _textBox.Draw(b);

        // 8. Buttons
        int mouseX = Game1.getOldMouseX();
        int mouseY = Game1.getOldMouseY();

        DrawButton(b, _sendButtonRect, "发送", Color.ForestGreen, _sendButtonRect.Contains(mouseX, mouseY));
        DrawButton(b, _pauseButtonRect, "暂停", Color.DarkGoldenrod, _pauseButtonRect.Contains(mouseX, mouseY));
        DrawButton(b, _resumeButtonRect, "继续", Color.SeaGreen, _resumeButtonRect.Contains(mouseX, mouseY));
        DrawButton(b, _cancelButtonRect, "取消", Color.Firebrick, _cancelButtonRect.Contains(mouseX, mouseY));

        // 9. Mouse cursor
        drawMouse(b);
    }

    private void DrawChatArea(SpriteBatch b)
    {
        var rendered = BuildRenderedMessages();
        int totalHeight = rendered.Sum(m => m.Height);
        int maxScroll = ChatScrollMetrics.Maximum(totalHeight, _chatViewport.Height);
        _scrollBack = Math.Clamp(_scrollBack, 0, maxScroll);
        float y = _chatViewport.Y + Math.Min(0, _chatViewport.Height - totalHeight) + _scrollBack;
        // The game owns this batch and scales uiScreen during composition.
        // Clip glyph sources in UI coordinates without changing any batch/device state.
        foreach (var msg in rendered)
        {
            if (y + msg.Height >= _chatViewport.Top && y < _chatViewport.Bottom)
            {
                ClippedSpriteText.Draw(b, Game1.smallFont, msg.Wrapped,
                    new Vector2(_chatViewport.X + 4, y), msg.Color, _chatViewport);
                if (!string.IsNullOrEmpty(msg.TokenInfo))
                    ClippedSpriteText.Draw(b, Game1.smallFont, msg.TokenInfo,
                        new Vector2(_chatViewport.X + 4, y + Game1.smallFont.MeasureString(msg.Wrapped).Y + ChatLineSpacing),
                        Color.DimGray, _chatViewport);
            }
            y += msg.Height;
        }
        if (maxScroll <= 0) { _scrollThumbRect = Rectangle.Empty; return; }
        b.Draw(Game1.fadeToBlackRect, _scrollTrackRect, Color.Black * 0.25f);
        int thumbHeight = ChatScrollMetrics.ThumbHeight(_scrollTrackRect.Height, _chatViewport.Height, totalHeight);
        int thumbY = _scrollTrackRect.Y + (int)((_scrollTrackRect.Height - thumbHeight) * (1.0 - (double)_scrollBack / maxScroll));
        _scrollThumbRect = new Rectangle(_scrollTrackRect.X, thumbY, _scrollTrackRect.Width, thumbHeight);
        b.Draw(Game1.fadeToBlackRect, _scrollThumbRect, Color.LightGray);
    }

    private static string ClipToWidth(string text, int maxWidth)
    {
        if (string.IsNullOrEmpty(text) || maxWidth <= 0)
            return text ?? string.Empty;
        if (Game1.smallFont.MeasureString(text).X <= maxWidth)
            return text;

        for (int length = text.Length - 1; length > 1; length--)
        {
            string candidate = text[..length] + "…";
            if (Game1.smallFont.MeasureString(candidate).X <= maxWidth)
                return candidate;
        }
        return text[..1];
    }

    private static void DrawButton(SpriteBatch b, Rectangle rect, string text, Color baseColor, bool isHovered)
    {
        Color bg = isHovered ? Color.Lerp(baseColor, Color.White, 0.25f) : baseColor;
        b.Draw(Game1.fadeToBlackRect, rect, bg * 0.85f);
        Vector2 textSize = Game1.smallFont.MeasureString(text);
        Vector2 textPos = new(
            rect.X + (rect.Width - textSize.X) / 2f,
            rect.Y + (rect.Height - textSize.Y) / 2f
        );
        b.DrawString(Game1.smallFont, text, textPos, Color.White);
    }

    protected override void cleanupBeforeExit()
    {
        base.cleanupBeforeExit();

        if (Game1.keyboardDispatcher?.Subscriber == _textBox)
        {
            Game1.keyboardDispatcher.Subscriber = null;
        }
        _textBox.Selected = false;
    }

    private void Submit()
    {
        string rawText = _textBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(rawText))
            return;

        if (IsProcessing)
        {
            Game1.addHUDMessage(new HUDMessage("当前任务正在处理中，请稍候或点击[取消]。", HUDMessage.error_type));
            return;
        }

        _textBox.Text = "";
        ChatHistory.Add(new ChatMessage("你", rawText, Color.DarkBlue));
        _scrollBack = 0;
        _unreadWhileScrolled = 0;
        IsProcessing = true;
        CurrentStatusText = "正在处理...";
        CurrentActionText = null;
        CurrentToolName = null;
        LastTokenInfo = null;

        _onSubmitText(rawText);
        exitThisMenu(playSound: false);
    }
}
