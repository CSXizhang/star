using HarmonyLib;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;
using StardewAI.Companion.Mod.Menus;

namespace StardewAI.Companion.Mod;

public sealed partial class ModEntry
{
    private static ModEntry? _nativeChatOwner;
    private readonly HashSet<string> _publishedChatEntries = new();
    private readonly HashSet<string> _presentedDecisions = new();
    private string? _nativeChatSave;
    private string? _chatDecisionId;
    private string? _unsentChatText;
    private bool _nativeChatSendVisible;
    private bool _nativeChatMouseDown;
    private string? _nativeChatDecisionSave;

    private Rectangle NativeChatSendBounds => new(
        Math.Min(Game1.chatBox.xPositionOnScreen + Game1.chatBox.width + 8, Game1.uiViewport.Width - 100),
        Game1.chatBox.yPositionOnScreen, 96, 56);

    private void DrawNativeChatSend(SpriteBatch batch)
    {
        _nativeChatSendVisible = Context.IsWorldReady && !Context.IsMultiplayer && Game1.activeClickableMenu == null
            && !Game1.eventUp && Game1.chatBox?.isActive() == true;
        if (!_nativeChatSendVisible) return;
        var bounds = NativeChatSendBounds;
        IClickableMenu.drawTextureBox(batch, bounds.X, bounds.Y, bounds.Width, bounds.Height, Color.White);
        batch.DrawString(Game1.smallFont, "发送", new Vector2(bounds.X + 16, bounds.Y + 14), Color.Black);
    }

    private void InstallNativeChat()
    {
        _nativeChatOwner = this;
        new Harmony(ModManifest.UniqueID + ".chat").Patch(
            AccessTools.Method(typeof(ChatBox), "textBoxEnter", new[] { typeof(string) }),
            prefix: new HarmonyMethod(typeof(ModEntry), nameof(OnNativeChatSend)));
        new Harmony(ModManifest.UniqueID + ".chat").Patch(
            AccessTools.Method(typeof(ChatBox), "draw", new[] { typeof(SpriteBatch) }),
            prefix: new HarmonyMethod(typeof(ModEntry), nameof(OnNativeChatDraw)));
    }

    private static bool OnNativeChatDraw() => !CompanionMenuClock.IsConversation(Game1.activeClickableMenu);

    private static bool OnNativeChatSend(string text_to_send)
    {
        var owner = _nativeChatOwner;
        if (owner == null || !Context.IsWorldReady || Context.IsMultiplayer ||
            string.IsNullOrWhiteSpace(text_to_send) || text_to_send.StartsWith('/')) return true;
        string? target = NativeChatText.ReplyTarget(owner._chatDecisionId,
            owner._lifeMenuUiState.PendingDecisions.Select(e => e.DecisionId).OfType<string>());
        // Natural language is interpreted in the shared round, without a second approval step.
        owner.CompleteNativeChatSend(Game1.chatBox, text_to_send,
            owner.DispatchLifeChat(text_to_send, "chat", null, target, false));
        return false;
    }

    private void CompleteNativeChatSend(ChatBox chatBox, string text, bool sent)
    {
        if (sent)
        {
            chatBox.setText("");
            _unsentChatText = null;
        }
        else _unsentChatText = text;
    }

    private void TickNativeChatInput()
    {
        // SMAPI suppresses ButtonPressed while a native text subscriber is active.
        // Read before the game handles click-away and clears the native draft.
        bool mouseDown = Microsoft.Xna.Framework.Input.Mouse.GetState().LeftButton == Microsoft.Xna.Framework.Input.ButtonState.Pressed;
        bool sendClicked = mouseDown && !_nativeChatMouseDown && _nativeChatSendVisible;
        _nativeChatMouseDown = mouseDown;
        if (sendClicked && Context.IsWorldReady && !Context.IsMultiplayer && Game1.activeClickableMenu == null
            && !Game1.eventUp && NativeChatSendBounds.Contains(Game1.getMouseX(true), Game1.getMouseY(true)))
        {
            Helper.Input.Suppress(SButton.MouseLeft);
            Game1.chatBox.textBoxEnter(Game1.chatBox.chatBox);
        }
    }

    private void PumpNativeChat()
    {
        if (!Context.IsWorldReady || Game1.chatBox == null || _conversationSaveId == null) return;
        if (_nativeChatSave != _conversationSaveId)
        {
            _nativeChatSave = _conversationSaveId;
            _publishedChatEntries.Clear();
            _presentedDecisions.Clear();
            _chatDecisionId = null;
            foreach (var entry in _lifeMenuUiState.Conversation.Where(e => !e.Unread || e.RolledBack))
                _publishedChatEntries.Add(entry.Id);
        }
        if (_unsentChatText != null)
        {
            Game1.chatBox.activate();
            Game1.chatBox.setText(_unsentChatText);
            _unsentChatText = null;
        }
        foreach (var entry in _lifeMenuUiState.Conversation.ToArray())
        {
            if (!_publishedChatEntries.Add(entry.Id) || entry.RolledBack) continue;
            var font = ChatBox.messageFont(LocalizedContentManager.CurrentLanguageCode);
            foreach (string part in NativeChatText.Segments(entry.Text,
                line => font.MeasureString(entry.Speaker + "：" + line).X, Game1.chatBox.width - 32))
                Game1.chatBox.addMessage($"{entry.Speaker}：{part}", entry.IsPlayer ? Color.LightBlue : Color.White);
            _lifeMenuUiState.MarkConversationRead(entry.Id);
        }
        if (_nativeChatDecisionSave != _conversationSaveId || Game1.activeClickableMenu != null
            || Game1.eventUp || Game1.chatBox.isActive()) return;
        var notice = _lifeMenuUiState.PendingDecisions.FirstOrDefault(e => !_presentedDecisions.Contains(e.Id));
        if (notice == null) return;
        _presentedDecisions.Add(notice.Id);
        _chatDecisionId = notice.DecisionId;
        GetCompanionDialogue().OpenDecision(notice, (answer, chosen) =>
        {
            if (!_lifeMenuUiState.PendingDecisions.Any(e => e.DecisionId == notice.DecisionId))
            {
                Game1.chatBox.addInfoMessage("这件事已经处理过了。");
                return;
            }
            if (answer == null) { Game1.chatBox.activate(); return; }
            if (!DispatchLifeChat(answer, "chat", null, notice.DecisionId, chosen))
            {
                _unsentChatText = answer;
                _presentedDecisions.Remove(notice.Id);
            }
        });
    }
}
