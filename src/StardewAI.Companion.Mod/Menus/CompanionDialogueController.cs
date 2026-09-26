using System.Text.RegularExpressions;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
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

/// <summary>Native DialogueBox/Response flow, independent of F8's command UI.</summary>
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

    public CompanionDialogueController(LifeMenuUiState state, Func<string, string, string?, bool> submit,
        Action refresh, Action settings, Action memory, Func<Farmer?> farmer,
        Func<LifeProfilePatchDto, string?> saveProfile, Func<string, string?> savePreference, Func<bool> canHelp,
        Action progress)
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
    }

    public void Reset()
    {
        _inbox.Reset();
        _ownedMenu = _pages = null;
        _next = null;
        _returnAfterPages = false;
        _afterPages = null;
        _portrait?.Dispose();
        _portrait = null;
        _meetingProfileRequest = _meetingMemoryRequest = _draftName = _meetingChoice = _meetingFeedback = null;
        _helpAfterFeedback = false;
        _directionRequest = _directionFeedback = null;
        _directionConfirmed = false;
    }

    public void Update()
    {
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

    public void ReturnToConversation() => _next = Open;

    public void Open()
    {
        if (Game1.activeClickableMenu != null || Game1.eventUp) return;
        _refresh();
        if (_directionConfirmed)
        {
            _directionConfirmed = false;
            SubmitDirectionPlan();
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
                    if (_canHelp()) Submit("我刚刚选择多帮忙。现在请用现有资源安排并做一件眼前能做的农活，不采购、不卖物品、不取消其他工作、不打开自由模式。若暂停或忙碌，只保留我的偏好，不启动。", "plan");
                    else ShowSpeech("我记下了。眼前的工作或暂停先照旧，等你方便再叫我。", false);
                };
            }
        }
        else if (_meetingProfileRequest != null || _meetingMemoryRequest != null)
            ShowSpeech("我正在记下我们的约定，稍等一下。", false);
        else if (!_state.HasProfileState)
            ShowSpeech("你好！我还在整理行李，等一下再来聊聊吧。", false);
        else if (CompanionFirstMeeting.NeedsMeeting(_state)) ShowFirstMeeting();
        else if (_inbox.HasUnreadReply && _inbox.Reply != null)
        {
            _inbox.MarkRead();
            ShowSpeech(_inbox.Reply, true);
        }
        else if (_state.IsChatPending)
            ShowSpeech("我还在想这件事。你先忙，想好了我会叫你。", false);
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
        if (_inbox.Receive(requestId, status, text, proposalReady, proposalNodeId))
            Game1.addHUDMessage(new HUDMessage($"{_state.CompanionName}有话想告诉你，走近聊聊吧。", HUDMessage.newQuest_type));
    }

    private void Ask(string text, Response[] responses, Action<string> answer)
    {
        void ShowChoices()
        {
            // Stardew deliberately uses its separate response layout for choices.
            Game1.currentLocation.createQuestionDialogue($"{CompanionNpcDialogueBox.LiteralText(DisplayName)}：", responses,
                (_, key) => _next = () => answer(key));
            _ownedMenu = Game1.activeClickableMenu;
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
        var choices = new List<Response>
        {
            new("plan", "按现在的方向商量下一步"),
            new("direction", "选个方向：赚钱、干活、献祭、装修"),
            new("progress", "看看任务进度"),
            new("say", "随便聊聊……"),
        };
        if (_inbox.Reply != null) choices.Add(new Response("again", "接着刚才的方案"));
        choices.Add(new Response("other", "说点别的……"));
        choices.Add(new Response("bye", "先去忙了"));
        Ask($"我在。我们现在的方向是{CompanionTaskPanelState.DirectionName(_state.PlayStyle)}，也可以先随便聊聊。", choices.ToArray(), key =>
        {
            switch (key)
            {
                case "chat": Submit("今天过得怎么样？", "chat"); break;
                case "plan": SubmitDirectionPlan(); break;
                case "direction": OpenDirections(); break;
                case "progress": _progress(); break;
                case "say": ShowInput("chat"); break;
                case "again": ShowSpeech(_inbox.Reply!, true); break;
                case "other": ShowOtherTopics(); break;
            }
        });
    }

    public void OpenDirections()
    {
        if (Game1.activeClickableMenu != null || Game1.eventUp) return;
        _refresh();
        if (!_state.HasProfileState)
        {
            ShowSpeech("还在读取你的设置，稍后我们再选方向。", false);
            _afterPages = ShowGreeting;
            return;
        }
        Ask("想一起往哪个方向走？选好后先商量具体安排，不会马上花钱或改掉正在做的事。", new[]
        {
            new Response("earn", "赚钱经营：照料作物、收获与出货"),
            new Response("workhorse", "日常干活：浇水、清杂物、动物和机器"),
            new Response("community", "社区献祭：找材料与保留物品"),
            new Response("decor", "农场装修：商量布局与清理准备"),
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
        $"我们接下来按{CompanionTaskPanelState.DirectionName(_state.PlayStyle)}这个方向。请先读取眼前真实情况，给我一个能力范围内可行的小方案，说明我需要亲自做的部分。现在只商量，先不要采纳、派工、花钱或取消旧安排。", "plan");

    private void ShowOtherTopics()
    {
        Ask("想聊哪件事？", new[]
        {
            new Response("memory", "看看我们的约定"),
            new Response("care", "听听你的近况"),
            new Response("settings", "伙伴设置"),
            new Response("back", "回到刚才的话题"),
        }, key =>
        {
            switch (key)
            {
                case "memory": _memory(); break;
                case "settings": _settings(); break;
                case "care":
                    var text = string.Join("\n", _state.RecentCareHints.Select(h => h.Text));
                    _state.MarkAllCareHintsRead();
                    ShowSpeech(string.IsNullOrWhiteSpace(text) ? "这会儿没有新的消息。" : text, false);
                    break;
                case "back": ShowGreeting(); break;
            }
        });
    }

    private void ShowResponses()
    {
        var responses = new List<Response>();
        if (_inbox.ProposalReady)
        {
            responses.Add(new Response("agree", "按这个安排。"));
        }
        if (_inbox.Mode == "chat" && _inbox.ReplySucceeded && !string.IsNullOrWhiteSpace(_inbox.PlayerText))
            responses.Add(new Response("discuss", "就这件事商量安排"));
        responses.Add(new Response("say", _inbox.Mode == "plan" ? "调整一下……" : "我想说……"));
        responses.Add(new Response("progress", "看看任务进度"));
        responses.Add(new Response("direction", "换个方向商量"));
        responses.Add(new Response("chat", "随便聊聊"));
        responses.Add(new Response("bye", "好，等会儿再聊。"));
        Ask("", responses.ToArray(), key =>
        {
            switch (key)
            {
                case "agree": if (_inbox.ProposalReady) Submit("按刚才这个具体方案安排吧。只采纳这个方案，不扩大花钱或取消无关工作。", "plan", _inbox.ProposalNodeId); break;
                case "discuss": Submit($"我们刚才聊的是：{_inbox.PlayerText}\n你的回复是：{_inbox.Reply}\n现在就这件事商量一个具体可行的小方案。先不要采纳、派工或消费。", "plan"); break;
                case "change": Submit("先不采纳刚才的方案，换个打算吧。", "plan"); break;
                case "say": ShowInput(_inbox.Mode); break;
                case "progress": _progress(); break;
                case "direction": OpenDirections(); break;
                case "chat": ShowInput("chat"); break;
                case "plan": Submit("看看眼前的农场，你觉得接下来怎么安排？", "plan"); break;
            }
        });
    }

    private void ShowSpeech(string text, bool responses)
    {
        _pages = _ownedMenu = new CompanionNpcDialogueBox(MakeDialogue(text));
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
        else ShowSpeech("这会儿还没接上，稍后再和我说吧。", false);
    }
}
