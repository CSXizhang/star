using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class NativeChatTests
{
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
