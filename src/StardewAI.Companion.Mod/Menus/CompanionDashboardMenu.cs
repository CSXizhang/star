using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>F8's centered management window; the NPC remains the only speech input.</summary>
public sealed class CompanionDashboardMenu : IClickableMenu
{
    private readonly LifeMenuUiState _state;
    private readonly CompanionHubActions _actions;
    private readonly Action<string?> _openConversation;
    private readonly TextBox _name;
    private readonly List<(Rectangle Rect, string Text, Action Click, bool Fixed)> _buttons = new();
    private readonly List<(Vector2 Position, string Text)> _labels = new();
    private readonly List<string> _visibleUnread = new();
    private Rectangle _body, _track, _thumb;
    private int _tab, _scroll, _maxScroll, _revision;
    private readonly int[] _positions = new int[3];
    private bool _details, _usage, _profileLoaded, _dragging, _jumpToBottom = true, _newMessages;
    private int? _savingRevision;
    private string _style, _personality, _frequency;
    private string? _feedback;
    private string[] _undoIds = Array.Empty<string>();
    private DateTime _undoUntil;

    public CompanionDashboardMenu(LifeMenuUiState state, CompanionHubActions actions, Action<string?> openConversation, int tab = 0)
    {
        _state = state; _actions = actions; _openConversation = openConversation; _tab = tab;
        _style = state.PlayStyle; _personality = state.Personality; _frequency = state.CareFrequency;
        _profileLoaded = state.HasProfileState; _revision = state.ConversationRevision;
        _name = new TextBox(Game1.content.Load<Texture2D>(@"LooseSprites\textBox"), null, Game1.smallFont, Game1.textColor)
            { limitWidth = false, textLimit = 20, Text = state.CompanionName };
        Layout();
    }

    private void AddButton(int x, int y, int w, string text, Action click, bool fixedPosition = false) =>
        _buttons.Add((new(x, y, w, 40), text, click, fixedPosition));
    private void Label(int x, ref int y, string text, int w)
    {
        if (string.IsNullOrWhiteSpace(text)) return;
        string wrapped = Game1.parseText(text, Game1.smallFont, w);
        _labels.Add((new(x, y), wrapped));
        y += (int)Game1.smallFont.MeasureString(wrapped).Y + 14;
    }
    private void Switch(int tab)
    {
        _positions[_tab] = _scroll; _tab = tab; _scroll = _positions[tab]; _feedback = null;
        _name.Selected = false;
        if (Game1.keyboardDispatcher?.Subscriber == _name) Game1.keyboardDispatcher.Subscriber = null;
        Layout();
    }
    private void Layout()
    {
        width = Math.Min(840, Math.Max(320, Game1.uiViewport.Width - 32));
        height = Math.Min(620, Math.Max(320, Game1.uiViewport.Height - 32));
        xPositionOnScreen = (Game1.uiViewport.Width - width) / 2; yPositionOnScreen = (Game1.uiViewport.Height - height) / 2;
        _body = new(xPositionOnScreen + 28, yPositionOnScreen + 112, width - 76, height - 190);
        _track = new(_body.Right + 14, _body.Top, 12, _body.Height);
        BuildContent();
        int wanted = _tab == 0 && _jumpToBottom ? _maxScroll : Math.Clamp(_scroll, 0, _maxScroll);
        if (_tab == 0) _jumpToBottom = false;
        if (wanted != _scroll) { _scroll = wanted; BuildContent(); }
        foreach (string id in _visibleUnread) _state.MarkConversationRead(id);
        int thumbHeight = Math.Max(30, (int)((double)_body.Height * _body.Height / (_body.Height + _maxScroll)));
        _thumb = new(_track.X, _track.Y + (_maxScroll == 0 ? 0 : (int)((double)_scroll / _maxScroll * (_track.Height - thumbHeight))), 12, thumbHeight);
    }
    private void BuildContent()
    {
        _buttons.Clear(); _labels.Clear(); _visibleUnread.Clear();
        string[] tabs = { "记录", "工作", "设置" };
        int tabWidth = Math.Min(104, (width - 72) / 3);
        for (int i = 0; i < 3; i++) { int target = i; AddButton(xPositionOnScreen + 28 + i * (tabWidth + 8), yPositionOnScreen + 58, tabWidth, tabs[i] + (_tab == i ? " •" : ""), () => Switch(target), true); }
        AddButton(xPositionOnScreen + width - 76, yPositionOnScreen + 14, 48, "×", () => exitThisMenu(playSound: false), true);
        int footerWidth = Math.Min(146, (width - 72) / 2);
        AddButton(xPositionOnScreen + 28, yPositionOnScreen + height - 56, footerWidth, "找伙伴说话", () => _openConversation(null), true);
        if (_undoIds.Length > 0 && DateTime.UtcNow < _undoUntil)
            AddButton(xPositionOnScreen + width - 28 - footerWidth, yPositionOnScreen + height - 56, footerWidth, "撤销删除", () => {
                if (_actions.SetDecisionVisibility(_undoIds, false)) { _undoIds = Array.Empty<string>(); _feedback = "已撤销删除。"; }
                else _feedback = "上一操作尚未确认，请稍后重试。";
            }, true);
        else if (_tab == 0 && _scroll < _maxScroll)
            AddButton(xPositionOnScreen + width - 28 - footerWidth, yPositionOnScreen + height - 56, footerWidth, _newMessages ? "有新消息 ↓" : "回到最新 ↓", () => { _jumpToBottom = true; _newMessages = false; }, true);
        int x = _body.X, y = _body.Y - _scroll, contentWidth = _body.Width;
        var task = CompanionCommandMenu.TaskState;
        if (_tab == 0)
        {
            var entries = _state.Conversation.Where(e => e.DecisionStatus != "dismissed").ToArray();
            if (entries.Length == 0) Label(x, ref y, "还没有交谈记录。找伙伴聊聊吧。", contentWidth);
            foreach (var entry in entries)
            {
                int entryTop = y;
                Label(x, ref y, $"{entry.Speaker} · {entry.GameDate}" + (entry.RolledBack ? " · 读档前" : ""), contentWidth);
                Label(x + 12, ref y, entry.Text, contentWidth - 12);
                if (entry.Unread && entryTop < _body.Bottom && y > _body.Top) _visibleUnread.Add(entry.Id);
                if (entry.DecisionStatus == "pending")
                {
                    var captured = entry;
                    AddButton(x + 12, y, 104, "答复", () => _openConversation(captured.DecisionId));
                    AddButton(x + 132, y, 90, "删除", () => Delete(new[] { captured.DecisionId! })); y += 54;
                }
                y += 18;
            }
        }
        else if (_tab == 1)
        {
            Label(x, ref y, task.Goal ?? _state.WorkGoal ?? "还没有安排工作，直接在对话里告诉我即可。", contentWidth);
            Label(x, ref y, task.ConnectionProblem ?? task.WaitReason ?? task.Current, contentWidth);
            bool paused = _state.WorkPaused || CompanionCommandMenu.AutonomyPaused;
            AddButton(x, y, 120, paused ? "继续工作" : "暂停工作", () => { if (paused) _actions?.Resume(); else _actions?.Pause(); });
            if (contentWidth < 410) y += 52;
            AddButton(contentWidth < 410 ? x : x + 132, y, 140, "取消当前工作", () => _actions?.Cancel());
            if (contentWidth < 410) y += 52;
            AddButton(contentWidth < 410 ? x : x + 284, y, 116, _details ? "收起详情" : "工作详情", () => _details = !_details); y += 56;
            if (_details)
            {
                Label(x, ref y, task.Progress ?? "", contentWidth);
                Label(x, ref y, task.LastResult ?? "尚无执行结果。", contentWidth);
                Label(x, ref y, task.NextStep, contentWidth);
                foreach (string goal in task.OtherGoals.Where(g => g != task.Goal).Distinct()) Label(x, ref y, goal, contentWidth);
            }
            var pending = _state.PendingDecisions;
            Label(x, ref y, pending.Count == 0 ? "没有待决定事项。" : $"待决定（{pending.Count}）", contentWidth);
            if (pending.Count > 0) { AddButton(x, y, 140, "清除全部", () => Delete(_state.PendingDecisions.Select(e => e.DecisionId!).Take(200).ToArray())); y += 52; }
            foreach (var entry in pending)
            {
                var captured = entry;
                string title = Game1.parseText(entry.Text, Game1.smallFont, contentWidth - 220).Split('\n')[0];
                AddButton(x, y, contentWidth - 108, title, () => { _openConversation(captured.DecisionId); });
                AddButton(x + contentWidth - 98, y, 90, "删除", () => Delete(new[] { captured.DecisionId! })); y += 52;
            }
        }
        else
        {
            Label(x, ref y, "伙伴名字", contentWidth);
            _name.X = x; _name.Y = y; _name.Width = 220; _name.Height = 44; y += 56;
            bool narrow = contentWidth < 580;
            AddButton(x, y, 190, "方向：" + Name(_style), () => _style = Next(_style, "earn", "workhorse", "decor"));
            if (narrow) y += 52;
            AddButton(narrow ? x : x + 206, y, 190, "性格：" + Name(_personality), () => _personality = Next(_personality, "gentle", "lively", "calm", "tsundere"));
            if (narrow) y += 52;
            AddButton(narrow ? x : x + 412, y, 170, "交流：" + Name(_frequency), () => _frequency = Next(_frequency, "quiet", "moderate", "chatty")); y += 54;
            AddButton(x, y, 140, _state.HasProfileState ? "保存设置" : "加载中", () => {
                if (!_state.HasProfileState || _state.PendingProfileSetRequestId != null) return;
                if (string.IsNullOrWhiteSpace(_name.Text)) { _feedback = "先填一个名字吧。"; return; }
                _savingRevision = _state.ProfileRevision;
                _actions?.SaveSettings(_name.Text.Trim(), _style, _personality, _frequency);
                _feedback = _state.PendingProfileSetRequestId == null ? "设置未发送，请检查连接。" : "设置已提交，等待确认。";
            });
            if (contentWidth < 500) y += 52;
            AddButton(contentWidth < 500 ? x : x + 156, y, 180, CompanionCommandMenu.ModeButtonText, () => _actions?.ToggleAutonomy());
            if (contentWidth < 500) y += 52;
            AddButton(contentWidth < 500 ? x : x + 352, y, 140, "偏好与约定", () => _actions?.Memory()); y += 56;
            Label(x, ref y, "主动帮忙关闭时，仍会处理你明确交代的工作。", contentWidth);
            AddButton(x, y, 140, _usage ? "收起用量" : "查看用量", () => _usage = !_usage); y += 56;
            if (_usage) Label(x, ref y, string.IsNullOrWhiteSpace(task.UsageSummary) ? "暂无用量记录。" : task.UsageSummary, contentWidth);
        }

        _maxScroll = Math.Max(0, y + _scroll - _body.Bottom);
    }
    private static string Next(string current, params string[] options) => options[(Array.IndexOf(options, current) + 1) % options.Length];
    private static string Name(string key) => key switch { "earn" => "赚钱", "workhorse" => "干活", "decor" => "布置", "gentle" => "温柔", "lively" => "活泼", "calm" => "沉稳", "tsundere" => "嘴硬心软", "quiet" => "安静", "moderate" => "适中", "chatty" => "健谈", _ => key };

    private void Delete(string[] ids)
    {
        if (!_actions.SetDecisionVisibility(ids, true)) { _feedback = "操作未发送，请检查连接或稍后重试。"; return; }
        _undoIds = ids; _undoUntil = DateTime.UtcNow.AddSeconds(8); _feedback = "已删除，可在 8 秒内撤销。";
    }
    public override void update(GameTime time)
    {
        base.update(time);
        if (!_profileLoaded && _state.HasProfileState)
        {
            _profileLoaded = true; _name.Text = _state.CompanionName;
            _style = _state.PlayStyle; _personality = _state.Personality; _frequency = _state.CareFrequency;
        }
        if (_savingRevision is int revision && _state.PendingProfileSetRequestId == null)
        {
            _feedback = _state.ProfileRevision > revision ? "设置已保存。" : "设置未确认，请检查连接后重试。";
            _savingRevision = null;
        }

        if (_revision != _state.ConversationRevision)
        {
            if (_tab == 0 && _scroll >= _maxScroll - 4) _jumpToBottom = true;
            else _newMessages = true;
            _revision = _state.ConversationRevision;
        }
        Layout();
        if (_tab == 0 && _scroll >= _maxScroll) _newMessages = false;
    }
    public override void receiveScrollWheelAction(int direction) { _scroll = Math.Clamp(_scroll + (direction > 0 ? -80 : 80), 0, _maxScroll); Layout(); }
    public override void receiveKeyPress(Keys key)
    {
        if (key is Keys.Escape or Keys.F8) exitThisMenu(playSound: false);
        if (key is Keys.PageUp or Keys.PageDown) { _scroll = Math.Clamp(_scroll + (key == Keys.PageUp ? -_body.Height : _body.Height), 0, _maxScroll); Layout(); }
    }
    public override void receiveLeftClick(int x, int y, bool playSound = true)
    {
        foreach (var b in _buttons.ToArray())
            if (b.Rect.Contains(x, y) && (b.Fixed || _body.Contains(b.Rect))) { b.Click(); Layout(); return; }
        if (_track.Contains(x, y) && _maxScroll > 0) { _dragging = true; leftClickHeld(x, y); return; }
        _name.Selected = _tab == 2 && _body.Contains(new Rectangle(_name.X, _name.Y, _name.Width, 44)) && new Rectangle(_name.X, _name.Y, _name.Width, 44).Contains(x, y);
        if (Game1.keyboardDispatcher != null) Game1.keyboardDispatcher.Subscriber = _name.Selected ? _name : null;
    }
    public override void leftClickHeld(int x, int y)
    {
        if (!_dragging) return;
        _scroll = (int)(Math.Clamp((double)(y - _track.Top - _thumb.Height / 2) / Math.Max(1, _track.Height - _thumb.Height), 0, 1) * _maxScroll); Layout();
    }
    public override void releaseLeftClick(int x, int y) => _dragging = false;
    public override void gameWindowSizeChanged(Rectangle oldBounds, Rectangle newBounds) => Layout();
    public override void draw(SpriteBatch batch)
    {
        batch.Draw(Game1.fadeToBlackRect, new Rectangle(0, 0, Game1.uiViewport.Width, Game1.uiViewport.Height), Color.Black * .35f);
        drawTextureBox(batch, xPositionOnScreen, yPositionOnScreen, width, height, Color.White);
        ClippedSpriteText.Draw(batch, Game1.smallFont, _state.CompanionName + " · 伙伴记录", new Vector2(xPositionOnScreen + 28, yPositionOnScreen + 22), Game1.textColor, new(xPositionOnScreen + 28, yPositionOnScreen + 14, width - 120, 36));
        foreach (var label in _labels) ClippedSpriteText.Draw(batch, Game1.smallFont, label.Text, label.Position, Game1.textColor, _body);
        if (_tab == 2 && _body.Contains(new Rectangle(_name.X, _name.Y, _name.Width, 44))) _name.Draw(batch);
        foreach (var b in _buttons)
        {
            if (!b.Fixed && !_body.Contains(b.Rect)) continue;
            drawTextureBox(batch, b.Rect.X, b.Rect.Y, b.Rect.Width, b.Rect.Height, Color.White);
            ClippedSpriteText.Draw(batch, Game1.smallFont, b.Text, new(b.Rect.X + 12, b.Rect.Y + 8), Game1.textColor, new(b.Rect.X + 6, b.Rect.Y, b.Rect.Width - 12, b.Rect.Height));
        }
        if (_maxScroll > 0) { batch.Draw(Game1.fadeToBlackRect, _track, Color.SaddleBrown * .25f); batch.Draw(Game1.fadeToBlackRect, _thumb, Color.SaddleBrown * .7f); }
        if (_feedback != null) ClippedSpriteText.Draw(batch, Game1.smallFont, _feedback, new(xPositionOnScreen + 182, yPositionOnScreen + height - 45), Game1.textColor, new(xPositionOnScreen + 182, yPositionOnScreen + height - 56, width - 376, 44));
        drawMouse(batch);
    }
    protected override void cleanupBeforeExit()
    {
        if (Game1.keyboardDispatcher?.Subscriber == _name) Game1.keyboardDispatcher.Subscriber = null;
        if (!Game1.eventUp) { Game1.dialogueUp = false; if (Game1.player != null && !Game1.player.UsingTool) Game1.player.CanMove = true; }
        base.cleanupBeforeExit();
    }
}
