using StardewAI.Companion.Mod.Menus;
using System.Reflection;
using System.Runtime.CompilerServices;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using HarmonyLib;
using StardewValley.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class NativeChatTests
{
    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void NativeSendClearsOnlyAcceptedInput(bool sent)
    {
        var owner = (ModEntry)RuntimeHelpers.GetUninitializedObject(typeof(ModEntry));
        var chat = (ChatBox)RuntimeHelpers.GetUninitializedObject(typeof(ChatBox));
        chat.chatBox = new ChatTextBox(null, null, null, Color.White);
        var snippet = (ChatSnippet)RuntimeHelpers.GetUninitializedObject(typeof(ChatSnippet));
        snippet.message = "先浇水";
        var snippets = (List<ChatSnippet>)typeof(ChatTextBox)
            .GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            .Single(f => f.FieldType == typeof(List<ChatSnippet>)).GetValue(chat.chatBox)!;
        snippets.Add(snippet);
        Assert.Equal("先浇水", string.Concat(snippets.Select(s => s.message)));
        typeof(ModEntry).GetField("_unsentChatText", BindingFlags.Instance | BindingFlags.NonPublic)!
            .SetValue(owner, "旧草稿");
        // Supply only font metrics; exercise the real ChatBox/TextBox clearing path.
        var harmony = new Harmony("stardew-tests.native-chat-font");
        harmony.Patch(AccessTools.Method(typeof(ChatBox), nameof(ChatBox.messageFont)),
            prefix: new HarmonyMethod(typeof(NativeChatTests), nameof(HeadlessChatFont)));
        try
        {
            typeof(ModEntry).GetMethod("CompleteNativeChatSend", BindingFlags.Instance | BindingFlags.NonPublic)!
                .Invoke(owner, new object[] { chat, "先浇水", sent });
        }
        finally { harmony.UnpatchAll(harmony.Id); }
        Assert.Equal(sent ? "" : "先浇水", string.Concat(snippets.Select(s => s.message)));
        Assert.Equal(sent ? null : "先浇水", typeof(ModEntry)
            .GetField("_unsentChatText", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(owner));
    }

    private static bool HeadlessChatFont(ref SpriteFont __result)
    {
        __result = new SpriteFont(null, new List<Rectangle>(), new List<Rectangle>(),
            new List<char>(), 16, 0, new List<Vector3>(), null);
        return false;
    }

    [Fact]
    public void ChatLinesFitActualFontWidthWithoutDroppingText()
    {
        string text = new string('字', 99);
        var parts = NativeChatText.Segments(text, s => s.Length * 20, 800).ToArray();
        Assert.All(parts, p => Assert.True(p.Length * 20 <= 800));
        Assert.Equal(text, string.Concat(parts));
    }
    [Fact]
    public void ReloadedQuestionMissingFromAuthoritativeStateCannotCaptureChat()
    {
        var state = new LifeMenuUiState();
        state.TryAddDecisionNotice("farm", "farm", "old", "1:spring:6", "旧问题");
        state.TryAddDecisionNotice("farm", "farm", "current", "1:spring:6", "当前问题");
        state.ReconcileDecisionIds(new[] { "current" });
        Assert.Single(state.PendingDecisions);
        Assert.Null(NativeChatText.ReplyTarget("old", state.PendingDecisions.Select(e => e.DecisionId!)));
    }

    [Fact]
    public void LongReplyKeepsAllWordsAndSurrogatePairs()
    {
        string text = new string('字', 159) + "😀" + new string('尾', 600);
        var parts = NativeChatText.Segments(text).ToArray();
        Assert.Equal(text, string.Concat(parts));
        Assert.All(parts, p => Assert.InRange(p.Length, 1, 160));
        Assert.All(parts, p => Assert.False(char.IsHighSurrogate(p[^1])));
    }

    [Fact]
    public void TableAndEmojiMarkupBecomeLiteralReadableText()
    {
        string text = string.Join("\n", NativeChatText.Segments("材料 | 现有 | 缺少\n--- | --- | ---\n木材 | 156 | 144\n[74]"));
        Assert.Contains("木材：现有 156；缺少 144", text);
        Assert.Contains("［74］", text);
        Assert.DoesNotContain("|", text);
        Assert.DoesNotContain("[", text);
    }

    [Fact]
    public void ReplyNeverMovesFromOldQuestionToNewQuestion()
    {
        Assert.Equal("old", NativeChatText.ReplyTarget("old", new[] { "old", "new" }));
        Assert.Null(NativeChatText.ReplyTarget("old", new[] { "new" }));
        Assert.Equal("only", NativeChatText.ReplyTarget(null, new[] { "only" }));
        Assert.Null(NativeChatText.ReplyTarget(null, new[] { "one", "two" }));
    }
}
