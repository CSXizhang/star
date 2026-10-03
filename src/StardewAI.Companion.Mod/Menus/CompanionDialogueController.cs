using System.Text.RegularExpressions;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>One companion conversation; replies wait for player interaction, never replace other menus.</summary>
public sealed class CompanionDialogueInbox
{
    public string Mode { get; private set; } = "chat";
    public string? PendingRequestId { get; private set; }
    public string? Reply { get; private set; }
    public bool HasUnreadReply { get; private set; }
    public bool ProposalReady { get; private set; }
    public string? ProposalNodeId { get; private set; }
    public string? PlayerText { get; private set; }
    public bool ReplySucceeded { get; private set; }

    public void Begin(string requestId, string mode, string? playerText = null)
    {
        PendingRequestId = requestId;
        Mode = mode == "plan" ? "plan" : "chat";
        ProposalReady = false;
        ProposalNodeId = null;
        PlayerText = playerText;
        ReplySucceeded = false;
    }

    public bool Receive(string requestId, string status, string? text, bool proposalReady = false, string? proposalNodeId = null)
    {
        if (requestId != PendingRequestId || status is not ("completed" or "failed")) return false;
        PendingRequestId = null;
        Reply = string.IsNullOrWhiteSpace(text)
            ? "我这边暂时没能接上话。等一下再来找我，好吗？"
            : PlainText(text);
        HasUnreadReply = true;
        ReplySucceeded = status == "completed";
        ProposalReady = Mode == "plan" && ReplySucceeded && proposalReady && !string.IsNullOrWhiteSpace(proposalNodeId);
        ProposalNodeId = ProposalReady ? proposalNodeId : null;
        return true;
    }

    public void MarkRead() => HasUnreadReply = false;
    public void Reset()
    {
        PendingRequestId = null;
        Reply = null;
        HasUnreadReply = false;
        Mode = "chat";
        ProposalReady = false;
        ProposalNodeId = PlayerText = null;
        ReplySucceeded = false;
    }

    public static string PlainText(string text)
    {
        // Render legacy/model formatting as speech, retaining its actual words.
        string clean = Regex.Replace(text, @"\[([^\]]+)\]\(([^)]+)\)", "$1（$2）");
        clean = Regex.Replace(clean, @"(?m)^\s*(?:#{1,6}\s+|[-*•]\s+|\d+[.)]\s+)", "");
        clean = Regex.Replace(clean, @"(?m)^\s*\|?\s*:?-{3,}.*$", "");
        return clean.Replace("**", "").Replace("`", "").Replace("|", "，").Trim();
    }
}

/// <summary>Native NPC speech and response choices for companion interaction and F8.</summary>
public sealed class CompanionDialogueController
{
    private readonly LifeMenuUiState _state;
    private readonly Func<string, string, string?, bool> _submit;
    private readonly Action _refresh;
    private readonly Action _settings;
    private readonly Action _memory;
    private readonly Func<Farmer?> _farmer;
    private readonly Func<LifeProfilePatchDto, string?> _saveProfile;
    private readonly Func<string, string?> _savePreference;
    private readonly Func<bool> _canHelp;
    private readonly Action _progress;
    private readonly Action? _conversation;
    private readonly Func<string?> _connectionProblem;
    private readonly Action? _records;
    private readonly Action? _hubSettings;
    private readonly Action? _replyInput;
    private readonly CompanionF8Flow _f8Flow = new();
    private bool _f8Active;
    private string? _directionRequest;
    private DateTime _directionSentAt;
    private string? _directionFeedback;
    private bool _directionConfirmed;
    private string? _meetingProfileRequest;
    private string? _meetingMemoryRequest;
    private string? _draftName;
    private string? _meetingChoice;
    private string? _meetingFeedback;
    private bool _helpAfterFeedback;
    private DateTime _meetingSentAt;
    private string DisplayName => _state.ProfileRevision == 0 && !_state.IsOnboarded && !_state.IsSkipped
        ? "新伙伴" : _state.CompanionName;
    private RenderTarget2D? _portrait;
    private readonly CompanionDialogueInbox _inbox = new();
    private IClickableMenu? _ownedMenu;
    private IClickableMenu? _pages;
    private Action? _next;
    private bool _returnAfterPages;
    private Action? _afterPages;
    private LocalizedContentManager.LanguageCode? _previousDialogueLanguage;

    private void UseReadableDialogueFont()
    {
        // Stardew's English SpriteText has no Chinese glyphs. Its native speech
        // and question boxes use the current language's font, so keep Chinese
        // active only while this companion conversation owns the menu.
        if (_previousDialogueLanguage != null ||
            LocalizedContentManager.CurrentLanguageCode == LocalizedContentManager.LanguageCode.zh) return;
        _previousDialogueLanguage = LocalizedContentManager.CurrentLanguageCode;
        LocalizedContentManager.CurrentLanguageCode = LocalizedContentManager.LanguageCode.zh;
    }

    private void RestoreDialogueLanguage()
    {
        if (_previousDialogueLanguage is not { } previous) return;
        _previousDialogueLanguage = null;
        LocalizedContentManager.CurrentLanguageCode = previous;
    }

    public CompanionDialogueController(LifeMenuUiState state, Func<string, string, string?, bool> submit,
        Action refresh, Action settings, Action memory, Func<Farmer?> farmer,
        Func<LifeProfilePatchDto, string?> saveProfile, Func<string, string?> savePreference, Func<bool> canHelp,
        Action progress, Func<string?>? connectionProblem = null, Action? conversation = null,
        Action? records = null, Action? hubSettings = null, Action? replyInput = null)
    {
        _state = state;
        _submit = submit;
        _refresh = refresh;
        _settings = settings;
        _memory = memory;
        _farmer = farmer;
        _saveProfile = saveProfile;
        _savePreference = savePreference;
        _canHelp = canHelp;
        _progress = progress;
        _conversation = conversation;
        _connectionProblem = connectionProblem ?? (() => null);
        _records = records;
        _hubSettings = hubSettings;
        _replyInput = replyInput;
    }

    public void Reset()
    {
        RestoreDialogueLanguage();
        _inbox.Reset();
        _ownedMenu = _pages = null;
        _next = null;
        _returnAfterPages = false;
        _afterPages = null;
        _portrait?.Dispose();
        _portrait = null;
        _meetingProfileRequest = _meetingMemoryRequest = _draftName = _meetingChoice = _meetingFeedback = null;
        _helpAfterFeedback = false;
        CompanionMenuClock.OwnedQuestion = null;
        _directionRequest = _directionFeedback = null;
        _directionConfirmed = false;
        _f8Active = false;
    }

    public void Update()
    {
        // Native question boxes expose exitThisMenu separately from closeDialogue.
        // Recover only our closed conversation's movement lock, never another event.
        if (CompanionMenuClock.OwnedQuestion != null && Game1.activeClickableMenu == null)
        {
            if (_next == null && _pages == null) _f8Active = false;
            CompanionMenuClock.OwnedQuestion = null;
            if (!Game1.eventUp)
            {
                Game1.dialogueUp = false;
                if (Game1.player != null && !Game1.player.UsingTool) Game1.player.CanMove = true;
            }
        }
        if (_previousDialogueLanguage != null && _next == null && _pages == null &&
            (_ownedMenu == null || !ReferenceEquals(Game1.activeClickableMenu, _ownedMenu)))
            RestoreDialogueLanguage();
        if ((_meetingProfileRequest != null || _meetingMemoryRequest != null) &&
            DateTime.UtcNow - _meetingSentAt > TimeSpan.FromSeconds(30))
        {
            if (_meetingProfileRequest != null) _state.EndProfileSet(_meetingProfileRequest);
            _meetingProfileRequest = _meetingMemoryRequest = null;
            _meetingFeedback = "保存的确认还没回来，我先不开始新工作。等连接恢复，我们再核对一下。";
            MeetingReady();
        }
        if (_directionRequest != null && DateTime.UtcNow - _directionSentAt > TimeSpan.FromSeconds(30))
        {
            _state.EndProfileSet(_directionRequest);
            _directionRequest = null;
            _directionFeedback = "保存的确认还没回来。连接恢复后再核对方向，暂时不开始新安排。";
            MeetingReady();
        }
        if (Game1.eventUp || Game1.currentLocation?.currentEvent != null)
        {
            _next = null;
            _pages = null;
            _afterPages = null;
            return;
        }
        if (_pages != null && !ReferenceEquals(Game1.activeClickableMenu, _pages))
        {
            _pages = null;
            if (Game1.activeClickableMenu == null)
                _next = _afterPages ?? (_returnAfterPages ? ShowResponses : null);
            _afterPages = null;
            _returnAfterPages = false;
        }
        if (_next == null) return;
        if (Game1.activeClickableMenu != null)
        {
            // A callback can run just before its native question box closes.
            // Wait for our own menu only; never open over an unrelated one.
            if (!ReferenceEquals(Game1.activeClickableMenu, _ownedMenu)) _next = null;
            return;
        }
        var next = _next;
        _next = null;
        next();
    }

    public void SaveSettingsAndPlan(string name, string style, string personality, string frequency)
    {
        _directionRequest = _saveProfile(new LifeProfilePatchDto(PlayStyle: style,
            Personality: personality, CareFrequency: frequency, CompanionName: name));
        _directionSentAt = DateTime.UtcNow;
        if (_directionRequest == null) _directionFeedback = "设置还没保存成功，等连接恢复再试一次吧。";
    }

    public void ReturnToConversation() => _next = _f8Active ? ShowF8Choices : _conversation ?? Open;
    public void ReturnToDashboard(Action openDashboard) => _next = openDashboard;

    public void OpenF8()
    {
        if (Game1.activeClickableMenu != null || Game1.eventUp) return;
        _f8Active = true;
        _f8Flow.Begin(_state);
        _refresh();
        ShowF8Unread();
    }

    public void ReturnToF8Choices()
    {
        _f8Active = true;
        _next = ShowF8Choices;
    }

    public void OwnF8Child(Action? returnTo = null)
    {
        _ownedMenu = Game1.activeClickableMenu;
        if (_ownedMenu != null) _ownedMenu.exitFunction = () => (returnTo ?? ReturnToF8Choices)();
    }

    public static bool CanCloseF8Root(bool f8Active, bool eventActive, object? activeMenu, object? ownedMenu, object? ownedQuestion) =>
        f8Active && !eventActive && activeMenu != null &&
        ReferenceEquals(activeMenu, ownedMenu) && ReferenceEquals(activeMenu, ownedQuestion);

    public static bool CanCloseDialogue(bool f8Active, bool eventActive, object? activeMenu, object? ownedMenu,
        object? ownedQuestion, bool speechOrInput) =>
        CanCloseF8Root(f8Active, eventActive, activeMenu, ownedMenu, ownedQuestion) ||
        (!eventActive && speechOrInput && activeMenu != null && ReferenceEquals(activeMenu, ownedMenu));

    public bool HandleEscape()
    {
        var menu = Game1.activeClickableMenu;
        if (!CanCloseDialogue(_f8Active, Game1.eventUp || Game1.currentLocation?.currentEvent != null,
            menu, _ownedMenu, CompanionMenuClock.OwnedQuestion,
            menu is CompanionNpcDialogueBox or CompanionSpeechInputMenu)) return false;

        // Escape dismisses the conversation, rather than advancing speech or
        // invoking the input's return callback and reopening the next menu.
        _f8Active = false;
        _next = null;
        _pages = null;
        _afterPages = null;
        _returnAfterPages = false;
        _ownedMenu = null;
        menu!.exitFunction = null;
        if (menu is CompanionNpcDialogueBox) menu.receiveKeyPress(Keys.Escape);
        else menu.exitThisMenu(playSound: false);
        CompanionMenuClock.OwnedQuestion = null;
        Game1.dialogueUp = false;
        if (Game1.player != null && !Game1.player.UsingTool) Game1.player.CanMove = true;
        RestoreDialogueLanguage();
        return true;
    }

    /// <summary>F8 only affects the current conversation's own menu.</summary>
    public bool HandleF8()
    {
        if (!_f8Active || _ownedMenu == null || !ReferenceEquals(Game1.activeClickableMenu, _ownedMenu)) return false;
        if (ReferenceEquals(_ownedMenu, CompanionMenuClock.OwnedQuestion))
        {
            _f8Active = false;
            _next = null;
            _ownedMenu.exitThisMenu(playSound: false);
        }
        else if (_ownedMenu is CompanionNpcDialogueBox or CompanionSpeechInputMenu)
            _ownedMenu.receiveKeyPress(Keys.F8);
        else _ownedMenu.exitThisMenu(playSound: false);
        return true;
    }

    private void ShowF8Unread()
    {
        if (Game1.activeClickableMenu != null || Game1.eventUp) return;
        var entry = _f8Flow.TakeNext(_state);
        if (entry == null) { ShowF8Choices(); return; }
        ShowSpeech(entry.Text, false, () => _f8Flow.Presented(_state, entry.Id));
        _afterPages = ShowF8Unread;
    }

    private void ShowF8Choices()
    {
        if (Game1.activeClickableMenu != null || Game1.eventUp) return;
        Ask("", new[]
        {
            new Response("reply", "直接回复"),
            new Response("history", "历史记录"),
            new Response("memory", "偏好和约定"),
            new Response("settings", "设置和用量"),
        }, key =>
        {
            switch (key)
            {
                case "reply": (_replyInput ?? _conversation ?? (() => ShowInput("chat")))(); break;
                case "history": (_records ?? _progress)(); OwnF8Child(); break;
                case "memory": _memory(); OwnF8Child(); break;
                case "settings": (_hubSettings ?? _settings)(); OwnF8Child(); break;
            }
        });
    }

    public void OpenReplyInput(Func<string, string?, bool, bool> submit, string? noticeId = null)
    {
        UseReadableDialogueFont();
        string? target = _f8Flow.PendingReplyTarget(_state, noticeId ?? CompanionConversationMenu.DraftDecisionId, includePresented: _f8Active);
        CompanionConversationMenu.DraftDecisionId = target;
        string prompt = target == null ? $"对{DisplayName}说……"
            : $"答复{DisplayName}的待决定事项（回车发送并确认选择）";
        _ownedMenu = new CompanionSpeechInputMenu(DisplayName, text =>
        {
            // Recheck the notice after typing; it may have been resolved by another reply.
            string? pendingTarget = _f8Flow.PendingReplyTarget(_state, target, includePresented: false);
            if (submit(text, pendingTarget, pendingTarget != null))
            {
                CompanionConversationMenu.DraftText = string.Empty;
                CompanionConversationMenu.DraftDecisionId = null;
                _next = _f8Active ? ShowF8Choices : _conversation ?? Open;
            }
            else
            {
                ShowSpeech(_connectionProblem() ?? "消息未发送，请稍后重试。", false);
                _afterPages = () => OpenReplyInput(submit, target);
            }
        }, ReturnToConversation, prompt, initialText: CompanionConversationMenu.DraftText,
            saveDraft: text => CompanionConversationMenu.DraftText = text);
        _pages = null;
        _next = null;
        Game1.activeClickableMenu = _ownedMenu;
    }

    public void OpenDashboard(CompanionHubActions actions, Action<string?> openConversation, int tab = 0)
    {
        if (Game1.eventUp) return;
        UseReadableDialogueFont();
        if (Game1.keyboardDispatcher != null) Game1.keyboardDispatcher.Subscriber = null;
        _ownedMenu = new CompanionDashboardMenu(_state, actions, openConversation, tab);
        if (_f8Active) _ownedMenu.exitFunction = ReturnToF8Choices;
        _pages = null; _next = null;
        Game1.activeClickableMenu = _ownedMenu;
    }

    public void OpenConversation(Func<string, string?, bool, bool> submit, Action openRecords, string? noticeId = null)
    {
        if (Game1.eventUp) return;
        _f8Active = false;
        UseReadableDialogueFont();
        _ownedMenu = new CompanionConversationMenu(_state, submit, MakeDialogue, _connectionProblem, openRecords, noticeId);
        _pages = null;
        _next = null;
        Game1.activeClickableMenu = _ownedMenu;
    }

    public void Open()
    {
        if (Game1.activeClickableMenu != null || Game1.eventUp) return;
        _refresh();
        if (_connectionProblem() is { } problem)
        {
            ShowSpeech(problem, false);
            return;
        }
        if (_directionConfirmed)
        {
            _directionConfirmed = false;
            ShowSpeech("好，记下了。", false);
        }
        else if (_directionFeedback != null)
        {
            ShowSpeech(_directionFeedback, false);
            _directionFeedback = null;
            _afterPages = OpenDirections;
        }
        else if (_directionRequest != null)
            ShowSpeech("我正在记下这个方向，确认后就一起看看眼前能做什么。", false);
        else if (_meetingFeedback != null)
        {
            ShowSpeech(_meetingFeedback, false);
            _meetingFeedback = null;
            _afterPages = ShowGreeting;
            if (_helpAfterFeedback)
            {
                _helpAfterFeedback = false;
                _afterPages = () =>
                {
                    if (_canHelp()) Submit("我希望你多帮帮忙，先做一件眼前需要的农活。", "plan");
                    else ShowSpeech("我记下了。眼前的工作或暂停先照旧，等你方便再叫我。", false);
                };
            }
        }
        else if (_meetingProfileRequest != null || _meetingMemoryRequest != null)
            ShowSpeech("我正在记下我们的约定，稍等一下。", false);
        else if (!_state.HasProfileState)
            ShowSpeech("伙伴服务已连接，正在读取你的设置。稍后再来聊聊吧。", false);
        else if (CompanionFirstMeeting.NeedsMeeting(_state)) ShowFirstMeeting();
        else if (_inbox.HasUnreadReply && _inbox.Reply != null)
        {
            if (_conversation != null) _conversation(); else ShowSpeech(_inbox.Reply, true, _inbox.MarkRead);
        }
        else if (_state.IsChatPending)
        { if (_conversation != null) _conversation(); else ShowSpeech("我还在想这件事。你先忙，想好了我会叫你。", false); }
        else if (_conversation != null) _conversation();
        else ShowGreeting();
    }

    private void ShowFirstMeeting()
    {
        if (!CompanionFirstMeeting.NeedsName(_state)) { ShowFirstPreference(); return; }
        Ask("你好，很高兴来到这里。你想怎么称呼我？", new[]
        {
            new Response("name", "让我想个名字……"),
            new Response("later", "以后再说吧。"),
        }, key =>
        {
            if (key == "later") SaveFirstMeeting("later");
            else ShowNameInput();
        });
    }

    private void ShowNameInput()
    {
        _ownedMenu = new CompanionSpeechInputMenu("伙伴", text =>
        {
            _draftName = CompanionFirstMeeting.NormalizeName(text);
            if (_draftName == null)
            {
                ShowSpeech("名字用一到十二个字就好，再想一个吧。", false);
                _afterPages = ShowNameInput;
            }
            else ShowFirstPreference();
        }, ShowFirstMeeting, "你想怎么称呼我？（1—12个字）", 24);
        Game1.activeClickableMenu = _ownedMenu;
    }

    private void ShowFirstPreference()
    {
        Ask("以后你希望我们怎么相处？可以慢慢来。", new[]
        {
            new Response("help", "多帮帮忙，先做件现有农活。"),
            new Response("chat", "多聊聊天吧。"),
            new Response("slow", "先相处看看。"),
            new Response("later", "稍后再说。"),
        }, SaveFirstMeeting);
    }

    private void SaveFirstMeeting(string choice)
    {
        _meetingChoice = choice;
        // Existing profiles retain their chosen name/personality/play style.
        _meetingProfileRequest = _saveProfile(CompanionFirstMeeting.Patch(
            CompanionFirstMeeting.NeedsName(_state) ? _draftName : null, choice));
        _meetingSentAt = DateTime.UtcNow;
        if (_meetingProfileRequest == null)
            ShowSpeech("这会儿还没记下来。等连接恢复，我们再接着聊。", false);
    }

    public void ReceiveProfile(string requestId, string status)
    {
        if (requestId == _directionRequest)
        {
            _directionRequest = null;
            _directionConfirmed = status == "confirmed";
            if (!_directionConfirmed) _directionFeedback = "方向还没保存成功，原来的安排不变。我们再试一次吧。";
            MeetingReady();
            return;
        }
        if (requestId != _meetingProfileRequest) return;
        _meetingProfileRequest = null;
        if (status != "confirmed")
            _meetingFeedback = "刚才的设置没能保存，我们再试一次吧。";
        else if (_meetingChoice == "later")
            _meetingFeedback = "没关系，先熟悉这里吧。想好了可以从伙伴设置里告诉我。";
        else
        {
            _meetingMemoryRequest = _savePreference(CompanionFirstMeeting.Preference(_meetingChoice!));
            _meetingSentAt = DateTime.UtcNow;
            if (_meetingMemoryRequest != null) return;
            _meetingFeedback = "名字已经记下了，相处的约定暂时没能保存。等连接恢复，我们再聊。";
        }
        MeetingReady();
    }

    public void ReceiveMemory(string requestId, string status)
    {
        if (requestId != _meetingMemoryRequest) return;
        _meetingMemoryRequest = null;
        _helpAfterFeedback = status == "confirmed" && _meetingChoice == "help";
        _meetingFeedback = status == "confirmed"
            ? _helpAfterFeedback ? "我记住了，很高兴和你一起生活。接下来我看看眼前有什么能帮忙的。"
                : "我记住了，很高兴和你一起生活。以后就照我们说的，慢慢熟悉彼此吧。"
            : "名字已经记下了，相处的约定没能保存。可以到我们的约定里再告诉我。";
        MeetingReady();
    }

    private void MeetingReady()
    {
        // Continue only when our own flow is unobstructed; otherwise retain feedback.
        if (Game1.activeClickableMenu == null && !Game1.eventUp) _next = Open;
        else Game1.addHUDMessage(new HUDMessage("初次见面的约定有消息了，回来聊聊吧。"));
    }

    public void Receive(string requestId, string status, string? text, bool proposalReady = false, string? proposalNodeId = null)
    {
        _inbox.Receive(requestId, status, text, proposalReady, proposalNodeId);
    }

    private void Ask(string text, Response[] responses, Action<string> answer)
    {
        void ShowChoices()
        {
            UseReadableDialogueFont();
            // Stardew deliberately uses its separate response layout for choices.
            Game1.currentLocation.createQuestionDialogue($"{CompanionNpcDialogueBox.LiteralText(DisplayName)}：", responses,
                (_, key) => _next = () => answer(key));
            _ownedMenu = Game1.activeClickableMenu;
            CompanionMenuClock.OwnedQuestion = _ownedMenu;
        }
        if (string.IsNullOrWhiteSpace(text)) ShowChoices();
        else
        {
            ShowSpeech(text, false);
            _afterPages = ShowChoices;
        }
    }

    private void ShowGreeting()
    {
        if (_conversation != null && (_state.IsOnboarded || _state.IsSkipped))
        {
            _conversation();
            return;
        }
        var choices = new List<Response>
        {
            new("say", _state.UnreadReplyCount > 0 ? $"直接对话（{_state.UnreadReplyCount} 条新回复）" : "直接对话"),
            new("progress", "查看当前计划"),
            new("usage", "查看模型用量"),
            new("direction", "确定大方向：赚钱、帮助干活、装修农场"),
        };
        if (_state.PendingDecisions.Count > 0)
            choices.Insert(0, new Response("decision-notices", "听听需要我决定的事"));
        if (_inbox.Reply != null) choices.Add(new Response("again", "接着刚才的方案"));
        choices.Add(new Response("other", "说点别的……"));
        choices.Add(new Response("bye", "先去忙了"));
        Ask("", choices.ToArray(), key =>
        {
            switch (key)
            {
                case "chat": Submit("今天过得怎么样？", "chat"); break;
                case "plan": SubmitDirectionPlan(); break;
                case "direction": OpenDirections(); break;
                case "progress": _progress(); break;
                case "usage":
                    _refresh();
                    ShowSpeech(string.IsNullOrWhiteSpace(CompanionCommandMenu.TaskState.UsageSummary)
                        ? "正在读取用量记录，稍后再来看。" : CompanionCommandMenu.TaskState.UsageSummary, false);
                    _afterPages = ShowGreeting;
                    break;
                case "say": if (_conversation != null) _conversation(); else ShowInput("chat"); break;
                case "again": ShowSpeech(_inbox.Reply!, true); break;
                case "other": ShowOtherTopics(); break;
                case "decision-notices": ShowDecisionNotices(); break;
            }
        });
    }

    public void OpenDirections()
    {
        if (Game1.activeClickableMenu != null || Game1.eventUp) return;
        _refresh();
        if (_connectionProblem() is { } problem)
        {
            ShowSpeech(problem, false);
            return;
        }
        if (!_state.HasProfileState)
        {
            ShowSpeech("还在读取你的设置，稍后我们再选方向。", false);
            _afterPages = ShowGreeting;
            return;
        }
        Ask("", new[]
        {
            new Response("earn", "赚钱经营：照料作物、收获与出货"),
            new Response("workhorse", "帮助干活：照料农场与日常杂务"),
            new Response("decor", "装修农场：布局、布置与清理"),
            new Response("chat", "先随便聊聊"),
            new Response("back", "回到刚才"),
        }, key =>
        {
            if (key == "chat") { ShowInput("chat"); return; }
            if (key == "back") { ShowGreeting(); return; }
            _directionRequest = _saveProfile(new LifeProfilePatchDto(PlayStyle: key));
            _directionSentAt = DateTime.UtcNow;
            if (_directionRequest == null)
            {
                ShowSpeech("这会儿还没能保存方向，等连接恢复再试。", false);
                _afterPages = ShowGreeting;
            }
        });
    }

    private void SubmitDirectionPlan() => Submit(
        $"我们接下来按{CompanionTaskPanelState.DirectionName(_state.PlayStyle)}这个方向，你觉得眼前先做什么好？", "plan");

    private void ShowOtherTopics()
    {
        Ask("想聊哪件事？", new[]
        {
            new Response("memory", "看看我们的约定"),
            new Response("care", "听听你的近况"),
            new Response("bedtime", "作息：设置睡觉时间"),
            new Response("settings", "伙伴设置"),
            new Response("back", "回到刚才的话题"),
        }, key =>
        {
            switch (key)
            {
                case "memory": _memory(); break;
                case "settings": _settings(); break;
                case "bedtime": ShowBedtime(); break;
                case "care":
                    var text = string.Join("\n", _state.RecentCareHints.Select(h => h.Text));
                    _state.MarkAllCareHintsRead();
                    ShowSpeech(string.IsNullOrWhiteSpace(text) ? "这会儿没有新的消息。" : text, false);
                    break;
                case "back": ShowGreeting(); break;
            }
        });
    }

    private void ShowBedtime()
    {
        Ask("希望我几点前上床？", new[]
        {
            new Response("2200", "晚上十点"), new Response("2300", "晚上十一点"),
            new Response("2400", "午夜十二点（默认）"), new Response("2500", "凌晨一点"),
            new Response("custom", "说一个别的时间……"), new Response("back", "返回"),
        }, key =>
        {
            if (key == "back") { ShowGreeting(); return; }
            if (key == "custom") { if (_conversation != null) _conversation(); else ShowInput("chat"); return; }
            _directionRequest = _saveProfile(new LifeProfilePatchDto(Bedtime: int.Parse(key)));
            _directionSentAt = DateTime.UtcNow;
        });
    }

    private void ShowResponses() => ShowGreeting();

    private void ShowDecisionNotices()
    {
        if (_conversation != null) { _conversation(); return; }
        string text = string.Join("\n", _state.UnreadCareHints.Where(h => h.Kind == "player-decision").Select(h => h.Text));
        _state.MarkDecisionNoticesRead();
        ShowSpeech(string.IsNullOrWhiteSpace(text) ? "这会儿没有新的待决定事项。" : text, false);
        _afterPages = ShowGreeting;
    }

    private void ShowSpeech(string text, bool responses, Action? onDisplayed = null)
    {
        UseReadableDialogueFont();
        _pages = _ownedMenu = new CompanionNpcDialogueBox(MakeDialogue(text), onDisplayed);
        _returnAfterPages = responses;
        Game1.activeClickableMenu = _pages;
    }

    private Dialogue MakeDialogue(string text)
    {
        // Render the real mechanics actor's head with Stardew's own renderer.
        // No NPC artwork or duplicate world actor is introduced.
        var farmer = _farmer();
        if (_portrait == null && farmer != null)
        {
            var device = Game1.graphics.GraphicsDevice;
            var targets = device.GetRenderTargets();
            var viewport = device.Viewport;
            var target = new RenderTarget2D(device, 64, 64);
            try
            {
                device.SetRenderTarget(target);
                device.Clear(Color.Transparent);
                using var batch = new SpriteBatch(device);
                batch.Begin(SpriteSortMode.FrontToBack, BlendState.AlphaBlend, SamplerState.PointClamp);
                farmer.FarmerRenderer.drawMiniPortrat(batch, new Vector2(8, 8), 0.9f, 3f, 2, farmer, 1f);
                batch.End();
                _portrait = target;
            }
            catch { target.Dispose(); throw; }
            finally
            {
                device.SetRenderTargets(targets);
                device.Viewport = viewport;
            }
        }
        var speaker = new NPC { Name = "StardewAI_Companion", displayName = CompanionNpcDialogueBox.LiteralText(DisplayName), Portrait = _portrait };
        return new Dialogue(speaker, null, CompanionNpcDialogueBox.LiteralText(text));
    }

    private void ShowInput(string mode)
    {
        UseReadableDialogueFont();
        _ownedMenu = new CompanionSpeechInputMenu(_state.CompanionName,
            text => Submit(text, mode), () => _next = ShowResponses);
        Game1.activeClickableMenu = _ownedMenu;
    }

    private void Submit(string text, string mode, string? acceptedNodeId = null)
    {
        if (_submit(text, mode, acceptedNodeId) && _state.PendingChatRequestId != null)
        {
            _inbox.Begin(_state.PendingChatRequestId, mode, text);
            // Leave the game unpaused while the model and real work continue.
            _ownedMenu = _pages = null;
            Game1.addHUDMessage(new HUDMessage($"{_state.CompanionName}：让我看看，等会儿告诉你。", HUDMessage.newQuest_type));
        }
        else ShowSpeech(_connectionProblem() ?? "这会儿还没接上，稍后再和我说吧。", false);
    }
}
