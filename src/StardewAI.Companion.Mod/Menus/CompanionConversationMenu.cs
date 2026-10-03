using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;
using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Menus;

public sealed record CompanionHubActions(Action Pause, Action Resume, Action Cancel, Action ToggleAutonomy,
    Action<string, string, string, string> SaveSettings, Action Memory,
    Func<IReadOnlyList<string>, bool, bool> SetDecisionVisibility);

/// <summary>NPC speech and input only; management and scrollable history live in F8.</summary>
public sealed class CompanionConversationMenu : IClickableMenu
{
    private readonly LifeMenuUiState _state;
    private readonly Func<string, string?, bool, bool> _submit;
    private readonly Func<string, Dialogue> _dialogue;
    private readonly Func<string?> _connection;
    private readonly Action _openRecords;
    private readonly TextBox _input;
    private CompanionNpcDialogueBox _speech = null!;
    private readonly List<(Rectangle Rect, string Text, Action Click, bool Fixed)> _buttons = new();
    private string? _replyTo, _feedback;
    private int _revision;
    private LifeChatStatus _status;
    public static string DraftText { get; set; } = "";
    public static string? DraftDecisionId { get; set; }

    public CompanionConversationMenu(LifeMenuUiState state, Func<string, string?, bool, bool> submit,
        Func<string, Dialogue> dialogue, Func<string?> connection, Action openRecords, string? noticeId = null)
    {
        _state = state; _submit = submit; _dialogue = dialogue; _connection = connection; _openRecords = openRecords;
        _input = new TextBox(Game1.content.Load<Texture2D>(@"LooseSprites\textBox"), null, Game1.smallFont, Game1.textColor)
            { limitWidth = false, textLimit = 500, Text = DraftText };
        _input.OnEnterPressed += _ => Send(false);
        var selected = state.PendingDecisions.FirstOrDefault(e => e.DecisionId == (noticeId ?? DraftDecisionId));
        _replyTo = selected?.DecisionId;
        ShowEntry(selected ?? state.Conversation.LastOrDefault(e => e.DecisionStatus != "dismissed"));
        if (state.IsChatPending) _status = LifeChatStatus.Idle;
        Focus();
    }
    private void Focus()
    {
        _input.Selected = true;
        if (Game1.keyboardDispatcher != null) Game1.keyboardDispatcher.Subscriber = _input;
    }
    private void ShowEntry(CompanionConversationEntry? entry)
    {
        string text = entry == null ? _connection() ?? "我在这里，想聊什么就告诉我。" : entry.IsPlayer ? "你：" + entry.Text : entry.Text;
        if (entry?.RolledBack == true) text = "读档前的交谈：" + text;
        _speech = new CompanionNpcDialogueBox(_dialogue(text), entry == null ? null : () => _state.MarkConversationRead(entry.Id));
        _revision = _state.ConversationRevision; _status = _state.ChatStatus;
        Layout();
    }
    private void Layout()
    {
        _buttons.Clear();
        int left = _speech.x, top = _speech.y, w = _speech.width;
        xPositionOnScreen = left; yPositionOnScreen = top; width = w; height = _speech.height + 60;
        _input.X = left + 8; _input.Y = top + _speech.height + 12; _input.Height = 44;
        _input.Width = w - (_replyTo == null ? 16 : 172);
        if (_replyTo != null)
            _buttons.Add((new(left + w - 158, _input.Y, 150, 40), "确认选择", () => Send(true), true));
    }
    private void Send(bool confirm)
    {
        string text = _input.Text.Trim();
        if (text.Length == 0 || _state.IsChatPending) return;
        if (!_submit(text, _replyTo, confirm)) { _feedback = "消息未发送，请检查连接。"; return; }
        _input.Text = DraftText = ""; _replyTo = DraftDecisionId = null; _feedback = null; _status = LifeChatStatus.Idle; Focus();
    }
    public override void update(GameTime time)
    {
        base.update(time);
        if (_revision != _state.ConversationRevision && !_state.IsChatPending)
        {
            if (_replyTo != null && _state.PendingDecisions.Any(e => e.DecisionId == _replyTo)) _revision = _state.ConversationRevision;
            else { _replyTo = null; ShowEntry(_state.Conversation.LastOrDefault(e => e.DecisionStatus != "dismissed")); }
        }
        if (_status != _state.ChatStatus)
        {
            _status = _state.ChatStatus;
            if (_state.IsChatPending) _speech = new CompanionNpcDialogueBox(_dialogue(_state.ChatStatus == LifeChatStatus.Queued ? "你的话记下了，忙完就回复。" : "让我看看。"));
        }
        Layout(); _speech.update(time);
    }
    public override void receiveKeyPress(Keys key)
    {
        if (key == Keys.Escape) exitThisMenu(playSound: false);
        if (key == Keys.Enter) Send(false);
        if (key == Keys.F8) OpenRecords();
    }
    public void PreserveDraft() { DraftText = _input.Text; DraftDecisionId = _replyTo; }
    public void OpenRecords() { PreserveDraft(); exitThisMenu(playSound: false); _openRecords(); }
    public override void receiveLeftClick(int x, int y, bool playSound = true)
    {
        foreach (var b in _buttons) if (b.Rect.Contains(x, y)) { b.Click(); return; }
        if (new Rectangle(_input.X, _input.Y, _input.Width, 44).Contains(x, y)) Focus();
        else if (new Rectangle(_speech.x, _speech.y, _speech.width, _speech.height).Contains(x, y)) _speech.AdvanceConversationPage();
    }
    public override void gameWindowSizeChanged(Rectangle oldBounds, Rectangle newBounds) { _speech.gameWindowSizeChanged(oldBounds, newBounds); Layout(); }
    public override void draw(SpriteBatch batch)
    {
        _speech.draw(batch); _input.Draw(batch);
        foreach (var b in _buttons)
        {
            drawTextureBox(batch, b.Rect.X, b.Rect.Y, b.Rect.Width, b.Rect.Height, Color.White);
            ClippedSpriteText.Draw(batch, Game1.smallFont, b.Text, new(b.Rect.X + 12, b.Rect.Y + 8), Game1.textColor, b.Rect);
        }
        if (_input.Text.Length == 0) ClippedSpriteText.Draw(batch, Game1.smallFont, _feedback ?? (_state.IsChatPending ? "正在等回复……" : "对我说……（回车发送，F8 查看记录）"), new(_input.X + 12, _input.Y + 12), Color.DimGray, new(_input.X, _input.Y, _input.Width, 44));
        drawMouse(batch);
    }
    protected override void cleanupBeforeExit()
    {
        PreserveDraft();
        if (Game1.keyboardDispatcher?.Subscriber == _input) Game1.keyboardDispatcher.Subscriber = null;
        if (!Game1.eventUp) { Game1.dialogueUp = false; if (Game1.player != null && !Game1.player.UsingTool) Game1.player.CanMove = true; }
        base.cleanupBeforeExit();
    }
}
