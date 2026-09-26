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

    public void Begin(string requestId, string mode)
    {
        PendingRequestId = requestId;
        Mode = mode == "plan" ? "plan" : "chat";
    }

    public bool Receive(string requestId, string status, string? text)
    {
        if (requestId != PendingRequestId || status is not ("completed" or "failed")) return false;
        PendingRequestId = null;
        Reply = string.IsNullOrWhiteSpace(text)
            ? "我这边暂时没能接上话。等一下再来找我，好吗？"
            : PlainText(text);
        HasUnreadReply = true;
        return true;
    }

    public void MarkRead() => HasUnreadReply = false;
    public void Reset()
    {
        PendingRequestId = null;
        Reply = null;
        HasUnreadReply = false;
        Mode = "chat";
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
    private readonly Func<string, string, bool> _submit;
    private readonly Action _refresh;
    private readonly Action _settings;
    private readonly Action _memory;
    private readonly Func<Farmer?> _farmer;
    private readonly Func<LifeProfilePatchDto, string?> _saveProfile;
    private readonly Func<string, string?> _savePreference;
    private readonly Func<bool> _canHelp;
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

    public CompanionDialogueController(LifeMenuUiState state, Func<string, string, bool> submit,
        Action refresh, Action settings, Action memory, Func<Farmer?> farmer,
        Func<LifeProfilePatchDto, string?> saveProfile, Func<string, string?> savePreference, Func<bool> canHelp)
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

    public void ReturnToConversation() => _next = Open;

    public void Open()
    {
        if (Game1.activeClickableMenu != null || Game1.eventUp) return;
        _refresh();
        if (_meetingFeedback != null)
        {
            ShowSpeech(_meetingFeedback, false);
            _meetingFeedback = null;
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

    public void Receive(string requestId, string status, string? text)
    {
        if (_inbox.Receive(requestId, status, text))
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
            new("chat", "随便聊聊"),
            new("plan", "接下来做什么？"),
            new("say", "我想说……"),
        };
        if (_inbox.Reply != null) choices.Add(new Response("again", "接着刚才聊"));
        choices.Add(new Response("other", "说点别的……"));
        choices.Add(new Response("bye", "先去忙了"));
        Ask("我在。", choices.ToArray(), key =>
        {
            switch (key)
            {
                case "chat": Submit("今天过得怎么样？", "chat"); break;
                case "plan": Submit("看看眼前的农场，你觉得接下来怎么安排？", "plan"); break;
                case "say": ShowInput("chat"); break;
                case "again": ShowSpeech(_inbox.Reply!, true); break;
                case "other": ShowOtherTopics(); break;
            }
        });
    }

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
        if (_inbox.Mode == "plan")
        {
            responses.Add(new Response("agree", "行，你看着办。"));
            responses.Add(new Response("change", "换个打算吧。"));
        }
        responses.Add(new Response("say", "我想说……"));
        responses.Add(new Response("plan", "聊聊接下来的安排"));
        responses.Add(new Response("bye", "好，等会儿再聊。"));
        Ask("", responses.ToArray(), key =>
        {
            switch (key)
            {
                case "agree": Submit("行，你看着办。", "plan"); break;
                case "change": Submit("先不采纳刚才的方案，换个打算吧。", "plan"); break;
                case "say": ShowInput(_inbox.Mode); break;
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

    private void Submit(string text, string mode)
    {
        if (_submit(text, mode) && _state.PendingChatRequestId != null)
        {
            _inbox.Begin(_state.PendingChatRequestId, mode);
            // Leave the game unpaused while the model and real work continue.
            _ownedMenu = _pages = null;
            Game1.addHUDMessage(new HUDMessage($"{_state.CompanionName}：让我看看，等会儿告诉你。", HUDMessage.newQuest_type));
        }
        else ShowSpeech("这会儿还没接上，稍后再和我说吧。", false);
    }
}
