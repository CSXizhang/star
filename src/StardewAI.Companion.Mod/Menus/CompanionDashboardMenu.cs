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
    private readonly Dictionary<Vector2, Color> _labelColors = new();
    private readonly HashSet<Rectangle> _quietButtons = new();
    private readonly Dictionary<Rectangle, Vector2> _buttonSizes = new();
    private readonly CompanionDashboardRefreshState _refreshState = new();
    private readonly List<int> _dividers = new();
    private CompanionDashboardLayout _layout = null!;
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
    private bool _undoVisible;

    public CompanionDashboardMenu(LifeMenuUiState state, CompanionHubActions actions, Action<string?> openConversation, int tab = 0)
    {
        _state = state; _actions = actions; _openConversation = openConversation; _tab = tab;
        _style = state.PlayStyle; _personality = state.Personality; _frequency = state.CareFrequency;
        _profileLoaded = state.HasProfileState; _revision = state.ConversationRevision;
        _usage = tab == 2;
        _name = new TextBox(Game1.content.Load<Texture2D>(@"LooseSprites\textBox"), null, Game1.smallFont, Game1.textColor)
            { limitWidth = false, textLimit = 20, Text = state.CompanionName };
        Layout();
    }

    private void AddButton(int x, int y, int w, string text, Action click, bool fixedPosition = false, bool quiet = false)
    {
        var rect = new Rectangle(x, y, w, 44);
        _buttons.Add((rect, text, click, fixedPosition));
        _buttonSizes[rect] = Game1.smallFont.MeasureString(text);
        if (quiet) _quietButtons.Add(rect);
    }
    private void Label(int x, ref int y, string text, int w, Color? color = null)
    {
        if (string.IsNullOrWhiteSpace(text)) return;
        string wrapped = Game1.parseText(text, Game1.smallFont, w);
        _labels.Add((new(x, y), wrapped));
        if (color is { } ink) _labelColors[new(x, y)] = ink;
        y += (int)Game1.smallFont.MeasureString(wrapped).Y + 14;
    }
    private void Divider(ref int y)
    {
        y += 10; _dividers.Add(y); y += 20;
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
        _layout = new(Game1.uiViewport.Width, Game1.uiViewport.Height);
        width = _layout.Bounds.Width; height = _layout.Bounds.Height;
        xPositionOnScreen = _layout.Bounds.X; yPositionOnScreen = _layout.Bounds.Y;
        _body = _layout.Body;
        _track = new(_body.Right + 14, _body.Top, 12, _body.Height);
        BuildContent();
        int wanted = _tab == 0 && _jumpToBottom ? _maxScroll : Math.Clamp(_scroll, 0, _maxScroll);
        if (_tab == 0) _jumpToBottom = false;
        if (wanted != _scroll) { _scroll = wanted; BuildContent(); }
        int thumbHeight = Math.Max(30, (int)((double)_body.Height * _body.Height / (_body.Height + _maxScroll)));
        _thumb = new(_track.X, _track.Y + (_maxScroll == 0 ? 0 : (int)((double)_scroll / _maxScroll * (_track.Height - thumbHeight))), 12, thumbHeight);
        int? focusedId = currentlySnappedComponent?.myID;
        allClickableComponents = _buttons.Select((b, i) => (Button: b, Id: i))
            .Where(b => b.Button.Fixed || _body.Contains(b.Button.Rect))
            .Select(b => new ClickableComponent(b.Button.Rect, b.Button.Text) { myID = b.Id }).ToList();
        allClickableComponents.Add(new ClickableComponent(_layout.Close, "关闭") { myID = 999 });
        currentlySnappedComponent = allClickableComponents.FirstOrDefault(c => c.myID == focusedId);
        _undoVisible = _undoIds.Length > 0 && DateTime.UtcNow < _undoUntil;
        _refreshState.Changed(_state, CompanionCommandMenu.TaskState, Game1.uiViewport.Width, Game1.uiViewport.Height, _tab, _scroll);
    }
    private void BuildContent()
    {
        _buttons.Clear(); _buttonSizes.Clear(); _labels.Clear(); _labelColors.Clear(); _quietButtons.Clear(); _dividers.Clear(); _visibleUnread.Clear();
        string[] tabs = { "记录", "工作", "设置" };
        for (int i = 0; i < 3; i++) { int target = i; var rect = _layout.Tabs[i]; AddButton(rect.X, rect.Y, rect.Width, tabs[i], () => Switch(target), true); }
        int footerWidth = Math.Min(160, (_layout.Footer.Width - 12) / 2);
        AddButton(_layout.Footer.X, _layout.Footer.Y, footerWidth, "直接回复", () => Reply(null), true);
        if (_undoIds.Length > 0 && DateTime.UtcNow < _undoUntil)
            AddButton(_layout.Footer.Right - footerWidth, _layout.Footer.Y, footerWidth, "撤销删除", () => {
                if (_actions.SetDecisionVisibility(_undoIds, false)) { _undoIds = Array.Empty<string>(); _feedback = "已撤销删除。"; }
                else _feedback = "上一操作尚未确认，请稍后重试。";
            }, true);
        else if (_tab == 0 && _scroll < _maxScroll)
            AddButton(_layout.Footer.Right - footerWidth, _layout.Footer.Y, footerWidth, _newMessages ? "有新消息 ↓" : "回到最新 ↓", () => { _jumpToBottom = true; _newMessages = false; }, true);
        int x = _body.X, y = _body.Y - _scroll, contentWidth = _body.Width;
        var task = CompanionCommandMenu.TaskState;
        if (_tab == 0)
        {
            var entries = _state.Conversation.Where(e => e.DecisionStatus != "dismissed").ToArray();
            if (entries.Length == 0) Label(x, ref y, "还没有交谈记录。找伙伴聊聊吧。", contentWidth);
            foreach (var entry in entries)
            {
                Label(x, ref y, $"{entry.Speaker} · {entry.GameDate}" + (entry.RolledBack ? " · 读档前" : ""), contentWidth, Color.SaddleBrown);
                int textTop = y;
                Label(x + 12, ref y, entry.Text, contentWidth - 12);
                if (entry.Unread && textTop < _body.Bottom && y - 14 > _body.Top) _visibleUnread.Add(entry.Id);
                if (entry.DecisionStatus == "pending")
                {
                    var captured = entry;
                    AddButton(x + 12, y, 104, "答复", () => Reply(captured.DecisionId));
                    AddButton(x + 132, y, 90, "删除", () => Delete(new[] { captured.DecisionId! })); y += 54;
                }
                Divider(ref y);
            }
        }
        else if (_tab == 1)
        {
            Label(x, ref y, "当前安排 · " + (_state.WorkPaused || CompanionCommandMenu.AutonomyPaused ? "已暂停" : task.Stage), contentWidth, Color.DarkOliveGreen);
            string goalText = new[] { task.Goal, _state.WorkGoal }.FirstOrDefault(g => !string.IsNullOrWhiteSpace(g)) ?? "还没有安排工作，找伙伴聊聊吧。";
            Label(x, ref y, goalText, contentWidth);
            Label(x, ref y, task.ConnectionProblem ?? task.WaitReason ?? task.Current, contentWidth);
            bool paused = _state.WorkPaused || CompanionCommandMenu.AutonomyPaused;
            string[] actionTexts = { paused ? "继续工作" : "暂停工作", _details ? "收起详情" : "工作详情", "取消当前工作" };
            Action[] clicks = { () => { if (paused) _actions.Resume(); else _actions.Pause(); }, () => _details = !_details, () => _actions.Cancel() };
            int actionX = x;
            for (int i = 0; i < actionTexts.Length; i++)
            {
                int buttonWidth = i == 2 ? 152 : 128;
                if (actionX + buttonWidth > _body.Right) { actionX = x; y += 56; }
                AddButton(actionX, y, Math.Min(buttonWidth, contentWidth), actionTexts[i], clicks[i], quiet: i == 2);
                actionX += buttonWidth + 12;
            }
            y += 58;
            if (_details)
            {
                Label(x, ref y, task.Progress ?? "", contentWidth);
                Label(x, ref y, task.LastResult ?? "尚无执行结果。", contentWidth);
                Label(x, ref y, task.NextStep, contentWidth);
                foreach (string goal in task.OtherGoals.Where(g => g != task.Goal).Distinct()) Label(x, ref y, goal, contentWidth);
            }
            Divider(ref y);
            var pending = _state.PendingDecisions;
            int pendingHeaderY = y;
            Label(x, ref y, pending.Count == 0 ? "没有待决定事项。" : $"待你决定 · {pending.Count}", contentWidth >= 360 ? contentWidth - 140 : contentWidth, Color.SaddleBrown);
            if (pending.Count > 0)
            {
                if (contentWidth >= 360) AddButton(_body.Right - 128, pendingHeaderY, 128, "清除全部", () => Delete(_state.PendingDecisions.Select(e => e.DecisionId!).Take(200).ToArray()), quiet: true);
                else { AddButton(x, y, 128, "清除全部", () => Delete(_state.PendingDecisions.Select(e => e.DecisionId!).Take(200).ToArray()), quiet: true); y += 54; }
            }
            foreach (var entry in pending)
            {
                var captured = entry;
                int rowY = y;
                bool wide = contentWidth >= 480;
                Label(x, ref y, entry.Text, wide ? contentWidth - 220 : contentWidth);
                int buttonY = wide ? rowY : y, buttonX = wide ? _body.Right - 204 : x;
                int answerWidth = Math.Min(104, (contentWidth - 12) / 2);
                AddButton(buttonX, buttonY, answerWidth, "答复", () => Reply(captured.DecisionId));
                AddButton(buttonX + answerWidth + 12, buttonY, Math.Min(88, contentWidth - answerWidth - 12), "删除", () => Delete(new[] { captured.DecisionId! }), quiet: true);
                y = Math.Max(y, buttonY + 44) + 20;
            }
        }
        else
        {
            int columns = _layout.FieldColumns, fieldWidth = (contentWidth - (columns - 1) * 24) / columns;
            int fieldY = y;
            Label(x, ref fieldY, "伙伴名字", fieldWidth, Color.SaddleBrown);
            _name.X = x; _name.Y = fieldY; _name.Width = fieldWidth; _name.Height = 44;
            string[] captions = { "相处方向", "性格", "交流频率" };
            string[] values = { Name(_style), Name(_personality), Name(_frequency) };
            Action[] changes = { () => _style = Next(_style, "earn", "workhorse", "decor"), () => _personality = Next(_personality, "gentle", "lively", "calm", "tsundere"), () => _frequency = Next(_frequency, "quiet", "moderate", "chatty") };
            int rowHeight = (int)Game1.smallFont.MeasureString("伙伴名字").Y + 74;
            for (int i = 0; i < captions.Length; i++)
            {
                int cell = i + 1, fieldX = x + cell % columns * (fieldWidth + 24);
                fieldY = y + cell / columns * rowHeight;
                Label(fieldX, ref fieldY, captions[i], fieldWidth, Color.SaddleBrown);
                AddButton(fieldX, fieldY, fieldWidth, values[i] + "  ›", changes[i]);
            }
            y += ((4 + columns - 1) / columns) * rowHeight;
            Divider(ref y);
            AddButton(x, y, Math.Min(220, contentWidth), CompanionCommandMenu.ModeButtonText, () => _actions.ToggleAutonomy()); y += 58;
            Label(x, ref y, "主动帮忙关闭时，仍会处理你明确交代的工作。", contentWidth, Color.SaddleBrown);
            y += 4;
            AddButton(x, y, 140, _state.HasProfileState ? "保存设置" : "加载中", () => {
                if (!_state.HasProfileState || _state.PendingProfileSetRequestId != null) return;
                if (string.IsNullOrWhiteSpace(_name.Text)) { _feedback = "先填一个名字吧。"; return; }
                _savingRevision = _state.ProfileRevision;
                _actions?.SaveSettings(_name.Text.Trim(), _style, _personality, _frequency);
                _feedback = _state.PendingProfileSetRequestId == null ? "设置未发送，请检查连接。" : "设置已提交，等待确认。";
            });
            y += 58;
            Divider(ref y);
            AddButton(x, y, 140, "偏好与约定", () => _actions.Memory(), quiet: true);
            if (contentWidth < 300) y += 56;
            AddButton(contentWidth < 300 ? x : x + 156, y, 140, _usage ? "收起用量" : "查看用量", () => _usage = !_usage, quiet: true); y += 58;
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
    private void Reply(string? decisionId)
    {
        exitThisMenu(playSound: false);
        _openConversation(decisionId);
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
        if (_refreshState.Changed(_state, CompanionCommandMenu.TaskState, Game1.uiViewport.Width, Game1.uiViewport.Height, _tab, _scroll)
            || (_tab == 0 && _jumpToBottom) || _undoVisible != (_undoIds.Length > 0 && DateTime.UtcNow < _undoUntil))
            Layout();
        if (_tab == 0 && _scroll >= _maxScroll) _newMessages = false;
    }
    public override void receiveScrollWheelAction(int direction) { _scroll = Math.Clamp(_scroll + (direction > 0 ? -80 : 80), 0, _maxScroll); Layout(); }
    public override void receiveKeyPress(Keys key)
    {
        if (key is Keys.Escape or Keys.F8) exitThisMenu(playSound: false);
        if (key is Keys.PageUp or Keys.PageDown) { _scroll = Math.Clamp(_scroll + (key == Keys.PageUp ? -_body.Height : _body.Height), 0, _maxScroll); Layout(); }
        if (!_name.Selected && key is Keys.Left or Keys.Right) Switch((_tab + (key == Keys.Left ? 2 : 1)) % 3);
    }
    public override void receiveGamePadButton(Buttons button)
    {
        if (button is Buttons.B or Buttons.Back) { exitThisMenu(playSound: false); return; }
        if (button is Buttons.LeftShoulder or Buttons.RightShoulder) { Switch((_tab + (button == Buttons.LeftShoulder ? 2 : 1)) % 3); return; }
        if (button is Buttons.RightThumbstickUp or Buttons.RightThumbstickDown) { receiveScrollWheelAction(button == Buttons.RightThumbstickUp ? 120 : -120); return; }
        currentlySnappedComponent ??= allClickableComponents.FirstOrDefault();
        if (currentlySnappedComponent == null) return;
        if (button == Buttons.A) { var point = currentlySnappedComponent.bounds.Center; receiveLeftClick(point.X, point.Y); return; }
        var direction = button switch { Buttons.DPadLeft => new Point(-1, 0), Buttons.DPadRight => new Point(1, 0), Buttons.DPadUp => new Point(0, -1), Buttons.DPadDown => new Point(0, 1), _ => Point.Zero };
        if (direction == Point.Zero) return;
        var origin = currentlySnappedComponent.bounds.Center;
        var next = allClickableComponents.Where(c => direction.X * (c.bounds.Center.X - origin.X) + direction.Y * (c.bounds.Center.Y - origin.Y) > 0)
            .OrderBy(c => Math.Abs(c.bounds.Center.X - origin.X) * (direction.X == 0 ? 3 : 1) + Math.Abs(c.bounds.Center.Y - origin.Y) * (direction.Y == 0 ? 3 : 1)).FirstOrDefault();
        if (next != null) currentlySnappedComponent = next;
        snapCursorToCurrentSnappedComponent();
    }
    public override void receiveLeftClick(int x, int y, bool playSound = true)
    {
        if (_layout.Close.Contains(x, y)) { exitThisMenu(playSound: false); return; }
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
        ClippedSpriteText.Draw(batch, Game1.dialogueFont, "农场伙伴", new(xPositionOnScreen + 28, yPositionOnScreen + 20), Game1.textColor, new(xPositionOnScreen + 28, yPositionOnScreen + 16, width - 110, 38));
        ClippedSpriteText.Draw(batch, Game1.smallFont, _state.CompanionName, new(xPositionOnScreen + 30, yPositionOnScreen + 54), Color.SaddleBrown, new(xPositionOnScreen + 28, yPositionOnScreen + 52, width - 100, 28));
        batch.Draw(Game1.mouseCursors, _layout.Close, new Rectangle(337, 494, 12, 12), Color.White);
        DrawRule(batch, yPositionOnScreen + 136);
        DrawRule(batch, _layout.Footer.Y - 14);
        foreach (int divider in _dividers)
            if (divider >= _body.Top && divider < _body.Bottom) batch.Draw(Game1.fadeToBlackRect, new Rectangle(_body.X, divider, _body.Width, 1), Color.SaddleBrown * .25f);
        foreach (var label in _labels) ClippedSpriteText.Draw(batch, Game1.smallFont, label.Text, label.Position, _labelColors.GetValueOrDefault(label.Position, Game1.textColor), _body);
        if (_tab == 2 && _body.Contains(new Rectangle(_name.X, _name.Y, _name.Width, 44))) _name.Draw(batch);
        foreach (var b in _buttons)
        {
            if (!b.Fixed && !_body.Contains(b.Rect)) continue;
            bool selected = b.Rect == _layout.Tabs[_tab];
            bool hovered = b.Rect.Contains(Game1.getOldMouseX(), Game1.getOldMouseY());
            if (!_quietButtons.Contains(b.Rect))
                drawTextureBox(batch, Game1.menuTexture, new Rectangle(0, 256, 60, 60), b.Rect.X, b.Rect.Y, b.Rect.Width, b.Rect.Height, hovered ? new Color(255, 240, 195) : Color.White, .5f, false);
            if (selected) batch.Draw(Game1.fadeToBlackRect, new Rectangle(b.Rect.X + 12, b.Rect.Bottom - 5, b.Rect.Width - 24, 3), Color.DarkOliveGreen);
            Vector2 size = _buttonSizes[b.Rect];
            ClippedSpriteText.Draw(batch, Game1.smallFont, b.Text, new(b.Rect.X + Math.Max(12, (b.Rect.Width - size.X) / 2), b.Rect.Y + (b.Rect.Height - size.Y) / 2), selected ? Color.DarkOliveGreen : _quietButtons.Contains(b.Rect) ? Color.SaddleBrown : Game1.textColor, new(b.Rect.X + 10, b.Rect.Y + 4, b.Rect.Width - 20, b.Rect.Height - 8));
        }
        if (_maxScroll > 0) { batch.Draw(Game1.fadeToBlackRect, _track, Color.SaddleBrown * .25f); batch.Draw(Game1.fadeToBlackRect, _thumb, Color.SaddleBrown * .7f); }
        // Only entries included in this rendered viewport have actually been shown.
        foreach (string id in _visibleUnread) _state.MarkConversationRead(id);
        _visibleUnread.Clear();
        string footerText = _feedback ?? "Esc / F8 返回";
        ClippedSpriteText.Draw(batch, Game1.smallFont, footerText, new(_layout.Footer.X, _layout.Footer.Y + 48), Color.SaddleBrown, new(_layout.Footer.X, _layout.Footer.Y + 46, _layout.Footer.Width, 24));
        drawMouse(batch);
    }
    private void DrawRule(SpriteBatch batch, int y) => batch.Draw(Game1.fadeToBlackRect, new Rectangle(xPositionOnScreen + 28, y, width - 56, 2), Color.SaddleBrown * .3f);
    protected override void cleanupBeforeExit()
    {
        if (Game1.keyboardDispatcher?.Subscriber == _name) Game1.keyboardDispatcher.Subscriber = null;
        if (!Game1.eventUp) { Game1.dialogueUp = false; if (Game1.player != null && !Game1.player.UsingTool) Game1.player.CanMove = true; }
        base.cleanupBeforeExit();
    }
}
