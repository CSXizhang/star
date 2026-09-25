using System.Text;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;
using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>
/// Life menu — shown when the player interacts with the companion.
/// Provides: chat (随便聊聊), plan (商量计划), request task (帮我做件事),
/// view memories (查看记忆), companion settings (伙伴设置).
///
/// The menu is fully independent from F8/<see cref="CompanionCommandMenu"/> state.
/// "帮我做件事" closes this menu and opens <see cref="CompanionCommandMenu"/> with
/// any prefilled text, reusing the existing dispatch path.
/// </summary>
public sealed class CompanionLifeMenu : IClickableMenu
{
    public const string MenuTitle = "和伙伴聊聊";

    // -----------------------------------------------------------------------
    // Sub-panels
    // -----------------------------------------------------------------------

    private enum Panel { Main, Chat, Memory, Care }

    private Panel _activePanel = Panel.Main;

    // -----------------------------------------------------------------------
    // Callbacks
    // -----------------------------------------------------------------------

    /// <summary>Submits a life-chat message (mode = "chat" or "plan").</summary>
    private readonly Func<string, string, bool> _onSubmitLifeChat;

    /// <summary>Closes this menu and opens CompanionCommandMenu with optional prefill.</summary>
    private readonly Action<string> _onOpenCommandMenu;

    /// <summary>Opens the CompanionSetupMenu.</summary>
    private readonly Action _onOpenSetup;

    /// <summary>Refreshes the confirmed work plan when the plan panel opens.</summary>
    private readonly Action _onRefreshWork;

    /// <summary>Requests a fresh memory list (§1.5 life.memory.list) when the memory panel opens.</summary>
    private readonly Action _onRefreshMemory;

    /// <summary>Sends a memory edit request.</summary>
    private readonly Func<string, string?, string?, string?, bool> _onMemoryEdit;  // op, id, kind, text

    // -----------------------------------------------------------------------
    // Read-only state (mirrored from LifeMenuUiState)
    // -----------------------------------------------------------------------

    private readonly LifeMenuUiState _state;

    // -----------------------------------------------------------------------
    // Chat panel state
    // -----------------------------------------------------------------------

    private string _chatMode = "chat"; // "chat" | "plan"
    private TextBox _chatInput = null!;
    private readonly List<(string Speaker, string Text, Color Color)> _chatHistory = new();
    private int _scrollBack;

    // -----------------------------------------------------------------------
    // Memory panel state
    // -----------------------------------------------------------------------

    private TextBox _memoryAddInput = null!;
    private string _memoryAddKind = "preference"; // "preference" | "agreement"
    private string? _memoryDeletePendingId;   // delete is a two-step, explicitly confirmed action
    private string? _memoryCorrectPendingId;  // correct loads the entry text into the input box
    private string? _memorySelectedId;        // entry shown full-text in the detail strip
    private int _memoryScroll;
    private int _careScroll;

    // -----------------------------------------------------------------------
    // Layout rectangles
    // -----------------------------------------------------------------------

    private Rectangle _closeButtonRect;

    // Main panel buttons
    private Rectangle _chatButtonRect;
    private Rectangle _planButtonRect;
    private Rectangle _taskButtonRect;
    private Rectangle _memoryButtonRect;
    private Rectangle _setupButtonRect;
    private Rectangle _careButtonRect;

    // Chat panel
    private Rectangle _chatModeToggleRect;
    private Rectangle _chatSendRect;
    private Rectangle _chatBackRect;
    private Rectangle _chatViewport;
    private Rectangle _careViewport;

    // Memory panel
    private Rectangle _memBackRect;
    private Rectangle _memAddKindRect;
    private Rectangle _memAddSendRect;
    private Rectangle _memCancelEditRect;
    private Rectangle _memConfirmYesRect;
    private Rectangle _memConfirmNoRect;
    private Rectangle _memViewport;
    private Rectangle _memDetailCorrectRect;
    private Rectangle _memDetailDeleteRect;

    /// <summary>Height of the detail strip that shows the selected entry's full text.</summary>
    private const int MemDetailStripHeight = 96;

    private int _layoutWidth;
    private int _layoutHeight;

    // -----------------------------------------------------------------------

    /// <summary>
    /// Initializes the life menu.
    /// </summary>
    public CompanionLifeMenu(
        LifeMenuUiState state,
        Func<string, string, bool> onSubmitLifeChat,
        Action<string> onOpenCommandMenu,
        Action onOpenSetup,
        Action onRefreshWork,
        Action onRefreshMemory,
        Func<string, string?, string?, string?, bool> onMemoryEdit)
        : base(
            Math.Max(0, (Game1.uiViewport.Width - 640) / 2),
            Math.Max(0, (Game1.uiViewport.Height - 500) / 2),
            Math.Min(640, Math.Max(480, Game1.uiViewport.Width - 32)),
            Math.Min(500, Math.Max(400, Game1.uiViewport.Height - 32)))
    {
        _state = state ?? throw new ArgumentNullException(nameof(state));
        _onSubmitLifeChat = onSubmitLifeChat ?? throw new ArgumentNullException(nameof(onSubmitLifeChat));
        _onOpenCommandMenu = onOpenCommandMenu ?? throw new ArgumentNullException(nameof(onOpenCommandMenu));
        _onOpenSetup = onOpenSetup ?? throw new ArgumentNullException(nameof(onOpenSetup));
        _onRefreshWork = onRefreshWork ?? throw new ArgumentNullException(nameof(onRefreshWork));
        _onRefreshMemory = onRefreshMemory ?? throw new ArgumentNullException(nameof(onRefreshMemory));
        _onMemoryEdit = onMemoryEdit ?? throw new ArgumentNullException(nameof(onMemoryEdit));

        BuildLayout();
    }

    private void BuildLayout()
    {
        _layoutWidth = Game1.uiViewport.Width;
        _layoutHeight = Game1.uiViewport.Height;

        int w = Math.Min(640, Math.Max(480, Game1.uiViewport.Width - 32));
        int h = Math.Min(500, Math.Max(400, Game1.uiViewport.Height - 32));
        width = w;
        height = h;
        xPositionOnScreen = Math.Max(0, (Game1.uiViewport.Width - w) / 2);
        yPositionOnScreen = Math.Max(0, (Game1.uiViewport.Height - h) / 2);

        _closeButtonRect = new Rectangle(xPositionOnScreen + w - 40, yPositionOnScreen + 12, 28, 28);

        // Main panel buttons
        int btnY = yPositionOnScreen + 80;
        int btnH = 44;
        int btnW = w - 48;
        _chatButtonRect = new Rectangle(xPositionOnScreen + 24, btnY, btnW, btnH);
        _planButtonRect = new Rectangle(xPositionOnScreen + 24, btnY + 54, btnW, btnH);
        _taskButtonRect = new Rectangle(xPositionOnScreen + 24, btnY + 108, btnW, btnH);
        _memoryButtonRect = new Rectangle(xPositionOnScreen + 24, btnY + 162, btnW, btnH);
        _setupButtonRect = new Rectangle(xPositionOnScreen + 24, btnY + 216, btnW, btnH);
        _careButtonRect = new Rectangle(xPositionOnScreen + 24, btnY + 270, btnW, btnH);

        // Chat panel
        Texture2D? tbTex = null;
        try { tbTex = Game1.content.Load<Texture2D>(@"LooseSprites\textBox"); } catch { }

        if (_chatInput is null)
        {
            _chatInput = new TextBox(tbTex, null, Game1.smallFont, Game1.textColor)
            { limitWidth = false, textLimit = 500 };
        }
        int chatInputY = yPositionOnScreen + h - 64;
        _chatInput.X = xPositionOnScreen + 24;
        _chatInput.Y = chatInputY;
        _chatInput.Width = w - 220;
        _chatInput.Height = 44;

        _chatModeToggleRect = new Rectangle(xPositionOnScreen + w - 190, chatInputY, 100, 44);
        _chatSendRect = new Rectangle(xPositionOnScreen + w - 84, chatInputY, 60, 44);
        _chatBackRect = new Rectangle(xPositionOnScreen + 24, yPositionOnScreen + 46, 72, 30);
        _chatViewport = new Rectangle(xPositionOnScreen + 24, yPositionOnScreen + 86, w - 48, h - 160);
        _careViewport = new Rectangle(xPositionOnScreen + 24, yPositionOnScreen + 86, w - 48, h - 112);

        // Memory panel
        if (_memoryAddInput is null)
        {
            _memoryAddInput = new TextBox(tbTex, null, Game1.smallFont, Game1.textColor)
            { limitWidth = false, textLimit = 200 };
        }
        int memInputY = yPositionOnScreen + h - 68;
        _memoryAddInput.X = xPositionOnScreen + 24;
        _memoryAddInput.Y = memInputY;
        _memoryAddInput.Width = w - 260;
        _memoryAddInput.Height = 40;

        _memAddKindRect = new Rectangle(xPositionOnScreen + w - 230, memInputY, 110, 40);
        _memAddSendRect = new Rectangle(xPositionOnScreen + w - 114, memInputY, 90, 40);
        _memCancelEditRect = new Rectangle(xPositionOnScreen + w - 230, memInputY - 44, 110, 36);
        _memBackRect = new Rectangle(xPositionOnScreen + 24, yPositionOnScreen + 46, 72, 30);
        _memViewport = new Rectangle(xPositionOnScreen + 24, yPositionOnScreen + 86, w - 48, h - 170);

        // Selected-entry detail strip (bottom of the list viewport)
        _memDetailCorrectRect = new Rectangle(_memViewport.Right - 150, _memViewport.Bottom - MemDetailStripHeight + 8, 66, 34);
        _memDetailDeleteRect = new Rectangle(_memViewport.Right - 78, _memViewport.Bottom - MemDetailStripHeight + 8, 66, 34);

        // Delete-confirmation strip (fixed position, overlays the top of the list)
        int stripY = yPositionOnScreen + 46;
        _memConfirmYesRect = new Rectangle(xPositionOnScreen + w - 170, stripY, 72, 30);
        _memConfirmNoRect = new Rectangle(xPositionOnScreen + w - 92, stripY, 72, 30);
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
            if (_activePanel != Panel.Main)
            {
                if (_activePanel == Panel.Memory && (_memoryDeletePendingId != null || _memoryCorrectPendingId != null))
                {
                    _memoryDeletePendingId = null;
                    _memoryCorrectPendingId = null;
                    return;
                }
                if (_activePanel == Panel.Memory)
                {
                    // Second Esc clears the selection, a third returns to main.
                    if (_memorySelectedId != null)
                    {
                        _memorySelectedId = null;
                        return;
                    }
                }
                _activePanel = Panel.Main;
                return;
            }
            exitThisMenu(playSound: false);
            return;
        }
        if (key == Keys.Enter)
        {
            if (_activePanel == Panel.Chat) SubmitChat();
            return;
        }
        base.receiveKeyPress(key);
    }

    public override void receiveScrollWheelAction(int direction)
    {
        if (_activePanel == Panel.Chat)
            _scrollBack = Math.Clamp(_scrollBack + (direction > 0 ? 48 : -48), 0, MaxChatScroll());
        else if (_activePanel == Panel.Memory)
            _memoryScroll = Math.Clamp(_memoryScroll + (direction > 0 ? 48 : -48), 0, MaxMemScroll());
        else if (_activePanel == Panel.Care)
            _careScroll = Math.Clamp(_careScroll + (direction > 0 ? 48 : -48), 0, MaxCareScroll());
        base.receiveScrollWheelAction(direction);
    }

    public override void receiveLeftClick(int x, int y, bool playSound = true)
    {
        base.receiveLeftClick(x, y, playSound);

        if (_closeButtonRect.Contains(x, y)) { exitThisMenu(playSound: false); return; }

        if (_activePanel == Panel.Main)
        {
            HandleMainClick(x, y);
            return;
        }
        if (_activePanel == Panel.Chat)
        {
            HandleChatClick(x, y);
            return;
        }
        if (_activePanel == Panel.Memory)
        {
            HandleMemoryClick(x, y);
            return;
        }
        if (_activePanel == Panel.Care && _chatBackRect.Contains(x, y))
        {
            _activePanel = Panel.Main;
        }
    }

    // -----------------------------------------------------------------------
    // Main panel
    // -----------------------------------------------------------------------

    private void HandleMainClick(int x, int y)
    {
        if (_chatButtonRect.Contains(x, y))
        {
            _chatMode = "chat";
            _activePanel = Panel.Chat;
            ActivateChatInput();
            return;
        }
        if (_planButtonRect.Contains(x, y))
        {
            _chatMode = "plan";
            _activePanel = Panel.Chat;
            _onRefreshWork();
            ActivateChatInput();
            return;
        }
        if (_taskButtonRect.Contains(x, y))
        {
            // Close life menu, open command menu with prefill if any.
            string prefill = _chatInput?.Text.Trim() ?? string.Empty;
            exitThisMenu(playSound: false);
            _onOpenCommandMenu(prefill);
            return;
        }
        if (_memoryButtonRect.Contains(x, y))
        {
            _activePanel = Panel.Memory;
            _onRefreshMemory();
            return;
        }
        if (_careButtonRect.Contains(x, y))
        {
            _activePanel = Panel.Care;
            _careScroll = MaxCareScroll();
            _state.MarkAllCareHintsRead();
            return;
        }
        if (_setupButtonRect.Contains(x, y))
        {
            _onOpenSetup();
            // Keep life menu open behind setup; setup callbacks will refresh state.
            return;
        }
    }

    // -----------------------------------------------------------------------
    // Chat panel
    // -----------------------------------------------------------------------

    private void ActivateChatInput()
    {
        _chatInput.Text = string.Empty;
        _chatInput.SelectMe();
        if (Game1.keyboardDispatcher != null)
            Game1.keyboardDispatcher.Subscriber = _chatInput;
    }

    private void HandleChatClick(int x, int y)
    {
        if (_chatBackRect.Contains(x, y)) { _activePanel = Panel.Main; return; }

        if (_chatModeToggleRect.Contains(x, y))
        {
            _chatMode = _chatMode == "chat" ? "plan" : "chat";
            if (_chatMode == "plan") _onRefreshWork();
            return;
        }
        if (_chatSendRect.Contains(x, y)) { SubmitChat(); return; }

        if (_chatInput.X <= x && x <= _chatInput.X + _chatInput.Width &&
            _chatInput.Y <= y && y <= _chatInput.Y + _chatInput.Height)
        {
            _chatInput.SelectMe();
            if (Game1.keyboardDispatcher != null)
                Game1.keyboardDispatcher.Subscriber = _chatInput;
        }
    }

    private void SubmitChat()
    {
        string text = _chatInput?.Text.Trim() ?? string.Empty;
        if (string.IsNullOrWhiteSpace(text)) return;
        if (_state.IsChatPending) return;

        bool ok = _onSubmitLifeChat(text, _chatMode);
        if (ok)
        {
            bool followBottom = _scrollBack >= MaxChatScroll() - 24;
            _chatHistory.Add(("你", text, Color.DarkBlue));
            _chatInput!.Text = string.Empty;
            if (followBottom) _scrollBack = MaxChatScroll();
        }
    }

    private int ChatEntryHeight(string speaker, string text)
    {
        string wrapped = Game1.parseText($"[{speaker}]: {text}", Game1.smallFont, _chatViewport.Width - 8);
        return (int)Game1.smallFont.MeasureString(wrapped).Y + 8;
    }

    private int MaxChatScroll()
    {
        int total = _chatHistory.Sum(m => ChatEntryHeight(m.Speaker, m.Text));
        int height = _chatViewport.Height - (_chatMode == "plan" ? 64 : 0);
        return Math.Max(0, total - height);
    }

    private string CareEntryText(PendingCareHint hint)
    {
        string kind = hint.Kind switch
        {
            "morning" => "早晨",
            "evening" => "傍晚",
            "work-done" => "工作后",
            _ => "消息"
        };
        return $"[{FormatGameDate(hint.GameDate)} · {kind}] {hint.Text}";
    }

    private int CareEntryHeight(PendingCareHint hint)
    {
        string wrapped = Game1.parseText(CareEntryText(hint), Game1.smallFont, _careViewport.Width - 8);
        return (int)Game1.smallFont.MeasureString(wrapped).Y + 12;
    }

    private int MaxCareScroll()
    {
        int total = _state.RecentCareHints.Sum(CareEntryHeight);
        return Math.Max(0, total - _careViewport.Height);
    }

    // -----------------------------------------------------------------------
    // Memory panel
    // -----------------------------------------------------------------------

    private void HandleMemoryClick(int x, int y)
    {
        if (_memBackRect.Contains(x, y)) { _activePanel = Panel.Main; return; }

        // Wait for the persisted edit response before accepting another change.
        if (_state.PendingMemoryEditRequestId != null) return;

        // Delete confirmation takes priority while pending.
        if (_memoryDeletePendingId != null)
        {
            if (_memConfirmYesRect.Contains(x, y))
            {
                _onMemoryEdit("delete", _memoryDeletePendingId, null, null);
                _memoryDeletePendingId = null;
                _memorySelectedId = null;
            }
            else if (_memConfirmNoRect.Contains(x, y))
            {
                _memoryDeletePendingId = null;
            }
            return;
        }

        // Selected-entry detail strip: full text + correct/delete for this entry.
        var selected = _state.MemoryEntries.FirstOrDefault(e => e.Id == _memorySelectedId);
        if (selected != null && MemDetailStripRect.Contains(x, y))
        {
            if (selected.Source != "system")
            {
                if (_memDetailCorrectRect.Contains(x, y))
                {
                    _memoryCorrectPendingId = selected.Id;
                    _memoryDeletePendingId = null;
                    _memoryAddInput!.Text = selected.Text;
                    _memoryAddInput.SelectMe();
                    if (Game1.keyboardDispatcher != null)
                        Game1.keyboardDispatcher.Subscriber = _memoryAddInput;
                }
                else if (_memDetailDeleteRect.Contains(x, y))
                {
                    _memoryDeletePendingId = selected.Id;
                    _memoryCorrectPendingId = null;
                }
            }
            return;
        }

        if (_memAddKindRect.Contains(x, y))
        {
            if (_memoryCorrectPendingId == null)
                _memoryAddKind = _memoryAddKind == "preference" ? "agreement" : "preference";
            return;
        }
        if (_memoryCorrectPendingId != null && _memCancelEditRect.Contains(x, y))
        {
            _memoryCorrectPendingId = null;
            _memoryAddInput!.Text = string.Empty;
            return;
        }
        if (_memAddSendRect.Contains(x, y))
        {
            string text = _memoryAddInput?.Text.Trim() ?? string.Empty;
            if (!string.IsNullOrWhiteSpace(text))
            {
                bool ok;
                if (_memoryCorrectPendingId != null)
                {
                    ok = _onMemoryEdit("correct", _memoryCorrectPendingId, null, text);
                    if (ok) _memoryCorrectPendingId = null;
                }
                else
                {
                    ok = _onMemoryEdit("add", null, _memoryAddKind, text);
                }
                if (ok) _memoryAddInput!.Text = string.Empty;
            }
            return;
        }

        if (_memoryAddInput.X <= x && x <= _memoryAddInput.X + _memoryAddInput.Width &&
            _memoryAddInput.Y <= y && y <= _memoryAddInput.Y + _memoryAddInput.Height)
        {
            _memoryAddInput.SelectMe();
            if (Game1.keyboardDispatcher != null)
                Game1.keyboardDispatcher.Subscriber = _memoryAddInput;
            return;
        }

        // Entry-level correct/delete buttons (two-step, explicitly confirmed) and row selection
        var entries = _state.MemoryEntries;
        int entryY = _memViewport.Y - _memoryScroll;
        foreach (var entry in entries)
        {
            if (entry.Source != "system")
            {
                var correctRect = new Rectangle(_memViewport.Right - 160, entryY, 76, 24);
                if (correctRect.Contains(x, y))
                {
                    _memoryCorrectPendingId = entry.Id;
                    _memoryDeletePendingId = null;
                    _memoryAddInput!.Text = entry.Text;
                    _memoryAddInput.SelectMe();
                    if (Game1.keyboardDispatcher != null)
                        Game1.keyboardDispatcher.Subscriber = _memoryAddInput;
                    return;
                }
                var delRect = new Rectangle(_memViewport.Right - 80, entryY, 76, 24);
                if (delRect.Contains(x, y))
                {
                    _memoryDeletePendingId = entry.Id;
                    _memoryCorrectPendingId = null;
                    return;
                }
            }

            // Clicking the row itself selects the entry for the full-text detail strip.
            var rowRect = new Rectangle(_memViewport.X, entryY, _memViewport.Width, 52);
            if (rowRect.Contains(x, y))
            {
                _memorySelectedId = entry.Id;
                return;
            }

            entryY += 52;
            if (entryY > _memViewport.Bottom) break;
        }
    }

    /// <summary>Rectangle of the selected-entry detail strip at the bottom of the list.</summary>
    private Rectangle MemDetailStripRect =>
        new(_memViewport.X, _memViewport.Bottom - MemDetailStripHeight, _memViewport.Width, MemDetailStripHeight);

    private int MaxMemScroll()
    {
        int total = _state.MemoryEntries.Count * 52;
        return Math.Max(0, total - _memViewport.Height);
    }

    // -----------------------------------------------------------------------
    // Draw
    // -----------------------------------------------------------------------

    public override void draw(SpriteBatch b)
    {
        if (_layoutWidth != Game1.uiViewport.Width || _layoutHeight != Game1.uiViewport.Height)
            BuildLayout();

        // Apply incoming chat reply to history
        if (_state.ChatStatus == LifeChatStatus.Completed && !string.IsNullOrEmpty(_state.ReplyText))
        {
            string companion = _state.CompanionName;
            // Check if already added to avoid duplicates
            if (_chatHistory.Count == 0 || _chatHistory[^1].Speaker != companion ||
                _chatHistory[^1].Text != _state.ReplyText)
            {
                bool followBottom = _scrollBack >= MaxChatScroll() - 24;
                _chatHistory.Add((companion, _state.ReplyText, Color.DarkGreen));
                if (followBottom) _scrollBack = MaxChatScroll();
            }
        }

        b.Draw(Game1.fadeToBlackRect, Game1.graphics.GraphicsDevice.Viewport.Bounds, Color.Black * 0.45f);
        drawTextureBox(b, xPositionOnScreen, yPositionOnScreen, width, height, Color.White);

        // Close button
        b.DrawString(Game1.smallFont, "X",
            new Vector2(_closeButtonRect.X + 8, _closeButtonRect.Y + 2), Color.Red);

        switch (_activePanel)
        {
            case Panel.Main: DrawMainPanel(b); break;
            case Panel.Chat: DrawChatPanel(b); break;
            case Panel.Memory: DrawMemoryPanel(b); break;
            case Panel.Care: DrawCarePanel(b); break;
        }

        drawMouse(b);
    }

    private void DrawMainPanel(SpriteBatch b)
    {
        // Header: name + personality natural-language line
        string header = PersonalityLine(_state.CompanionName, _state.Personality);
        b.DrawString(Game1.dialogueFont, header,
            new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 12), Game1.textColor);

        // Unread care-hint indicator
        if (_state.UnreadCareHints.Count > 0)
        {
            b.DrawString(Game1.smallFont,
                $"阿星有 {_state.UnreadCareHints.Count} 条消息想和你聊聊 →",
                new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 50), Color.DarkOrange);
        }

        int mx = Game1.getOldMouseX(), my = Game1.getOldMouseY();
        DrawButton(b, _chatButtonRect, "随便聊聊", Color.SteelBlue, _chatButtonRect.Contains(mx, my));
        DrawButton(b, _planButtonRect, "商量计划", Color.SaddleBrown, _planButtonRect.Contains(mx, my));
        DrawButton(b, _taskButtonRect, "帮我做件事", Color.ForestGreen, _taskButtonRect.Contains(mx, my));

        DrawButton(b, _memoryButtonRect, "查看记忆", Color.DarkOrchid, _memoryButtonRect.Contains(mx, my));
        DrawButton(b, _setupButtonRect, "伙伴设置", Color.SlateGray, _setupButtonRect.Contains(mx, my));
        int unread = _state.UnreadCareHints.Count;
        string careLabel = unread > 0 ? $"伙伴消息  （{unread} 条新消息）" : "伙伴消息";
        DrawButton(b, _careButtonRect, careLabel, Color.DarkOrange, _careButtonRect.Contains(mx, my));
    }

    private void DrawChatPanel(SpriteBatch b)
    {
        string modeLabel = _chatMode == "plan" ? "商量计划模式" : "随便聊聊模式";
        b.DrawString(Game1.dialogueFont, modeLabel,
            new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 12), Game1.textColor);

        DrawButton(b, _chatBackRect, "← 返回", Color.SlateGray,
            _chatBackRect.Contains(Game1.getOldMouseX(), Game1.getOldMouseY()));

        if (_chatMode == "plan")
        {
            string current = _state.WorkPaused ? "已暂停" : _state.WorkMode == "free" ? "自主帮忙中" : "按指令帮忙";
            string goal = _state.WorkGoal ?? "还没有设定目标";
            string next = _state.ActiveGoalSummaries.FirstOrDefault()
                ?? _state.RecentTodoSummaries.FirstOrDefault()
                ?? "眼下没有待办，可商量下一步";
            string wait = _state.WaitingConditions.FirstOrDefault()
                ?? _state.PlanWaitReason
                ?? "没有等待条件";
            string[] lines = { $"当前：{current}　目标：{goal}", $"近期：{next}", $"等待：{wait}" };
            for (int i = 0; i < lines.Length; i++)
                b.DrawString(Game1.smallFont, ClipToWidth(Game1.smallFont, lines[i], _chatViewport.Width - 8),
                    new Vector2(_chatViewport.X + 4, _chatViewport.Y + i * 20), Color.DarkSlateGray);
        }

        Rectangle historyViewport = _chatMode == "plan"
            ? new Rectangle(_chatViewport.X, _chatViewport.Y + 64, _chatViewport.Width, _chatViewport.Height - 64)
            : _chatViewport;
        // Chat history
        float cy = historyViewport.Y - _scrollBack;
        foreach (var (speaker, text, color) in _chatHistory)
        {
            string line = $"[{speaker}]: {text}";
            string wrapped = Game1.parseText(line, Game1.smallFont, historyViewport.Width - 8);
            int entryHeight = (int)Game1.smallFont.MeasureString(wrapped).Y + 8;
            if (cy + entryHeight > historyViewport.Top && cy < historyViewport.Bottom)
                ClippedSpriteText.Draw(b, Game1.smallFont, wrapped,
                    new Vector2(historyViewport.X + 4, cy), color, historyViewport);
            cy += entryHeight;
        }

        // Chat status
        if (_state.IsChatPending)
        {
            string statusMsg = _state.ChatStatus switch
            {
                LifeChatStatus.Queued => $"排队中（位置 {_state.QueuePosition ?? 0}）…",
                LifeChatStatus.Processing => "伙伴正在思考…",
                _ => "发送中…"
            };
            b.DrawString(Game1.smallFont, statusMsg,
                new Vector2(historyViewport.X + 4, historyViewport.Bottom - 28), Color.DarkOrange);
        }

        int mx = Game1.getOldMouseX(), my = Game1.getOldMouseY();
        string toggleLabel = _chatMode == "chat" ? "切换：计划" : "切换：聊天";
        DrawButton(b, _chatModeToggleRect, toggleLabel, Color.SaddleBrown, _chatModeToggleRect.Contains(mx, my));
        DrawButton(b, _chatSendRect, "发送", _state.IsChatPending ? Color.Gray : Color.ForestGreen,
            _chatSendRect.Contains(mx, my));
        _chatInput.Draw(b);

        if (_chatMode == "plan")
            b.DrawString(Game1.smallFont, "只读建议，不自动派发任务",
                new Vector2(xPositionOnScreen + 112, yPositionOnScreen + 50), Color.DimGray);

    }

    private void DrawCarePanel(SpriteBatch b)
    {
        b.DrawString(Game1.dialogueFont, "伙伴消息",
            new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 12), Game1.textColor);
        DrawButton(b, _chatBackRect, "← 返回", Color.SlateGray,
            _chatBackRect.Contains(Game1.getOldMouseX(), Game1.getOldMouseY()));

        if (_state.RecentCareHints.Count == 0)
        {
            b.DrawString(Game1.smallFont, "还没有伙伴消息。",
                new Vector2(_careViewport.X + 4, _careViewport.Y + 4), Game1.textColor);
            return;
        }

        _careScroll = Math.Clamp(_careScroll, 0, MaxCareScroll());
        float y = _careViewport.Y - _careScroll;
        foreach (var hint in _state.RecentCareHints)
        {
            string wrapped = Game1.parseText(CareEntryText(hint), Game1.smallFont, _careViewport.Width - 8);
            int height = (int)Game1.smallFont.MeasureString(wrapped).Y + 12;
            if (y + height > _careViewport.Top && y < _careViewport.Bottom)
                ClippedSpriteText.Draw(b, Game1.smallFont, wrapped,
                    new Vector2(_careViewport.X + 4, y), Game1.textColor, _careViewport);
            y += height;
        }
    }

    private void DrawMemoryPanel(SpriteBatch b)
    {
        b.DrawString(Game1.dialogueFont, "记忆列表",
            new Vector2(xPositionOnScreen + 24, yPositionOnScreen + 12), Game1.textColor);

        if (_state.MemoryEditFeedback != null)
            b.DrawString(Game1.smallFont, _state.MemoryEditFeedback,
                new Vector2(xPositionOnScreen + 210, yPositionOnScreen + 22), Color.DarkGreen);

        DrawButton(b, _memBackRect, "← 返回", Color.SlateGray,
            _memBackRect.Contains(Game1.getOldMouseX(), Game1.getOldMouseY()));
        b.DrawString(Game1.smallFont, "点击条目查看全文",
            new Vector2(_memBackRect.Right + 24, yPositionOnScreen + 52), Color.DimGray);

        // Memory entries
        var entries = _state.MemoryEntries;
        int ey = _memViewport.Y - _memoryScroll;
        foreach (var entry in entries)
        {
            if (ey + 52 > _memViewport.Top && ey < _memViewport.Bottom)
            {
                string kindLabel = entry.Kind switch
                {
                    "preference" => "偏好",
                    "agreement" => "约定",
                    "event" => "共同经历",
                    _ => entry.Kind
                };
                string srcLabel = entry.Source switch
                {
                    "player" => "玩家",
                    "companion" => "伙伴",
                    "system" => "系统",
                    _ => entry.Source
                };
                string dateLabel = FormatGameDate(entry.GameDate);

                // Selected-row highlight behind the entry.
                if (entry.Id == _memorySelectedId)
                    b.Draw(Game1.fadeToBlackRect,
                        new Rectangle(_memViewport.X, ey, _memViewport.Width, 52),
                        Color.LightSteelBlue * 0.20f);

                b.DrawString(Game1.smallFont, $"[{kindLabel}·{srcLabel}·{dateLabel}]",
                    new Vector2(_memViewport.X + 4, ey), Color.DimGray);

                // Clip the body to the row's visible width so long entries (up to 200
                // chars) never spill over the buttons or the next row; the full text is
                // readable via the selected-entry detail strip below.
                float textMaxWidth = entry.Source == "system"
                    ? _memViewport.Width - 12
                    : _memViewport.Width - 172;
                b.DrawString(Game1.smallFont,
                    ClipToWidth(Game1.smallFont, entry.Text, textMaxWidth),
                    new Vector2(_memViewport.X + 4, ey + 20), Game1.textColor);

                // Correct / delete buttons for player/companion-sourced entries
                if (entry.Source != "system")
                {
                    var correctRect = new Rectangle(_memViewport.Right - 160, ey, 76, 24);
                    DrawButton(b, correctRect, "纠正", Color.SaddleBrown,
                        correctRect.Contains(Game1.getOldMouseX(), Game1.getOldMouseY()));
                    var delRect = new Rectangle(_memViewport.Right - 80, ey, 76, 24);
                    DrawButton(b, delRect, "删除", Color.Firebrick,
                        delRect.Contains(Game1.getOldMouseX(), Game1.getOldMouseY()));
                }
            }
            ey += 52;
        }

        // Selected-entry detail strip: full text, wrapped, with correct/delete actions.
        var selectedEntry = entries.FirstOrDefault(e => e.Id == _memorySelectedId);
        if (selectedEntry != null)
        {
            var strip = MemDetailStripRect;
            b.Draw(Game1.fadeToBlackRect, strip, Color.Black * 0.78f);
            b.DrawString(Game1.smallFont,
                Game1.parseText(selectedEntry.Text, Game1.smallFont, strip.Width - 188),
                new Vector2(strip.X + 8, strip.Y + 8), Game1.textColor);
            if (selectedEntry.Source != "system")
            {
                int mx1 = Game1.getOldMouseX(), my1 = Game1.getOldMouseY();
                DrawButton(b, _memDetailCorrectRect, "纠正", Color.SaddleBrown,
                    _memDetailCorrectRect.Contains(mx1, my1));
                DrawButton(b, _memDetailDeleteRect, "删除", Color.Firebrick,
                    _memDetailDeleteRect.Contains(mx1, my1));
            }
        }

        if (entries.Count == 0)
        {
            b.DrawString(Game1.smallFont, "还没有任何记忆。可以在聊天中记下约定或偏好。",
                new Vector2(_memViewport.X + 4, _memViewport.Y + 4), Color.DimGray);
        }

        // Delete-confirmation strip: deleting a memory always requires an explicit second click.
        if (_memoryDeletePendingId != null)
        {
            b.Draw(Game1.fadeToBlackRect,
                new Rectangle(_memViewport.X, _memConfirmYesRect.Y - 2, _memViewport.Width, 36),
                Color.Black * 0.75f);
            b.DrawString(Game1.smallFont, "确定删除这条记忆？此操作不可撤销。",
                new Vector2(_memViewport.X + 4, _memConfirmYesRect.Y + 4), Color.DarkOrange);
            int mx0 = Game1.getOldMouseX(), my0 = Game1.getOldMouseY();
            DrawButton(b, _memConfirmYesRect, "确认删除", Color.Firebrick,
                _memConfirmYesRect.Contains(mx0, my0));
            DrawButton(b, _memConfirmNoRect, "取消", Color.SlateGray,
                _memConfirmNoRect.Contains(mx0, my0));
        }

        // Add / correct memory input
        int mx = Game1.getOldMouseX(), my = Game1.getOldMouseY();
        bool correcting = _memoryCorrectPendingId != null;
        b.DrawString(Game1.smallFont, correcting ? "纠正记忆：" : "新增：",
            new Vector2(xPositionOnScreen + 24, _memoryAddInput.Y - 24), Color.DimGray);
        _memoryAddInput.Draw(b);
        DrawButton(b, _memAddKindRect,
            _memoryAddKind == "preference" ? "类型：偏好" : "类型：约定",
            Color.SaddleBrown, !correcting && _memAddKindRect.Contains(mx, my));
        DrawButton(b, _memAddSendRect, correcting ? "保存纠正" : "记下",
            _state.PendingMemoryEditRequestId != null ? Color.Gray : Color.ForestGreen,
            _memAddSendRect.Contains(mx, my));
        if (correcting)
        {
            DrawButton(b, _memCancelEditRect, "取消纠正", Color.SlateGray,
                _memCancelEditRect.Contains(mx, my));
        }
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    private static string PersonalityLine(string name, string personality) => personality switch
    {
        "gentle" => $"{name} 温柔地看着你",
        "lively" => $"{name} 活泼地向你打招呼",
        "calm" => $"{name} 沉稳地站在那里",
        "tsundere" => $"{name} 别过脸，但看上去很在意你",
        _ => $"{name} 看着你"
    };

    private static string FormatGameDate(string date)
    {
        string[] parts = date.Split(':');
        if (parts.Length != 3 || !int.TryParse(parts[0], out int year) ||
            !int.TryParse(parts[2], out int day)) return date;
        string season = parts[1] switch
        {
            "spring" => "春",
            "summer" => "夏",
            "fall" => "秋",
            "winter" => "冬",
            _ => parts[1]
        };
        return $"第{year}年{season}{day}日";
    }

    /// <summary>
    /// Truncates <paramref name="text"/> to a single line of at most
    /// <paramref name="maxWidth"/> pixels, appending an ellipsis when clipped.
    /// </summary>
    private static string ClipToWidth(SpriteFont font, string text, float maxWidth)
    {
        if (font.MeasureString(text).X <= maxWidth) return text;
        const string ellipsis = "…";
        float budget = maxWidth - font.MeasureString(ellipsis).X;
        var sb = new StringBuilder();
        float width = 0f;
        foreach (char c in text)
        {
            float charWidth = font.MeasureString(c.ToString()).X;
            if (width + charWidth > budget) break;
            sb.Append(c);
            width += charWidth;
        }
        return sb.ToString() + ellipsis;
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

    protected override void cleanupBeforeExit()
    {
        base.cleanupBeforeExit();
        if (Game1.keyboardDispatcher?.Subscriber == _chatInput ||
            Game1.keyboardDispatcher?.Subscriber == _memoryAddInput)
            Game1.keyboardDispatcher.Subscriber = null;
    }
}
