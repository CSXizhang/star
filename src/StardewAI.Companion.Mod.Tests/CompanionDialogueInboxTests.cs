using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class CompanionDialogueInboxTests
{
    [Theory]
    [InlineData("plan", "completed", true, "node", true)]
    [InlineData("chat", "completed", true, "node", false)]
    [InlineData("plan", "failed", true, "node", false)]
    [InlineData("plan", "completed", false, "node", false)]
    [InlineData("plan", "completed", true, "", false)]
    public void ApprovalNeedsSuccessfulConcretePlan(string mode, string status, bool ready, string node, bool expected)
    {
        var inbox = new CompanionDialogueInbox();
        inbox.Begin("current", mode, "帮忙照料作物");
        Assert.True(inbox.Receive("current", status, "实际回复", ready, node));
        Assert.Equal(expected, inbox.ProposalReady);
        Assert.Equal("帮忙照料作物", inbox.PlayerText);
        inbox.Begin("next", "chat", "谢谢");
        Assert.False(inbox.ProposalReady);
        Assert.Null(inbox.ProposalNodeId);
    }

    [Fact]
    public void ModelSpeechCannotExecuteNativeDialogueCommandsOrGrantItems()
    {
        string text = CompanionNpcDialogueBox.LiteralText("今天有500金。\n$v event#$action AddMoney 500#[74] mail}%farm @ ^");
        Assert.Contains("今天有500金。", text);
        Assert.Contains("［74］", text);
        Assert.DoesNotContain("$", text);
        Assert.DoesNotContain("#", text);
        Assert.DoesNotContain("}", text);
        Assert.DoesNotContain("%", text);
        Assert.DoesNotContain("\n", text);
    }

    [Theory]
    [InlineData(640)]
    [InlineData(800)]
    [InlineData(1280)]
    [InlineData(1920)]
    public void PortraitPanelFitsWindowAndRetainsTextColumn(int viewportWidth)
    {
        int width = CompanionNpcDialogueBox.PanelWidth(viewportWidth);
        Assert.InRange(width, 512, viewportWidth - 64);
        Assert.True(width - 480 > 0);
    }

    [Fact]
    public void AsyncReplyWaitsToBeReadAndIgnoresOtherRequests()
    {
        var inbox = new CompanionDialogueInbox();
        inbox.Begin("own", "plan");
        Assert.False(inbox.Receive("other", "completed", "别的对话"));
        Assert.False(inbox.Receive("own", "processing", "正在安排"));
        Assert.False(inbox.HasUnreadReply);
        Assert.True(inbox.Receive("own", "completed", "先照料菜地，种子明天再买。"));
        Assert.Equal("plan", inbox.Mode);
        Assert.True(inbox.HasUnreadReply);
        Assert.False(inbox.Receive("own", "completed", "重复回包"));
        inbox.MarkRead();
        Assert.False(inbox.HasUnreadReply);
        Assert.Equal("先照料菜地，种子明天再买。", inbox.Reply);
    }

    [Fact]
    public void ResetDropsOtherSaveReplyAndChatKeepsItsOwnMode()
    {
        var inbox = new CompanionDialogueInbox();
        inbox.Begin("old", "plan");
        inbox.Reset();
        Assert.False(inbox.Receive("old", "completed", "旧存档"));
        inbox.Begin("new", "chat");
        Assert.True(inbox.Receive("new", "failed", null));
        Assert.Equal("chat", inbox.Mode);
        Assert.NotEmpty(inbox.Reply!);
    }

    [Fact]
    public void LegacyMarkdownBecomesSpeechWithoutRemovingFacts()
    {
        string text = CompanionDialogueInbox.PlainText("## 今天\n- **金币500**\n|---|---|\n[草莓](https://stardewvalleywiki.com/Strawberry)");
        Assert.Contains("金币500", text);
        Assert.Contains("https://stardewvalleywiki.com/Strawberry", text);
        Assert.DoesNotContain("**", text);
        Assert.DoesNotContain("##", text);
        Assert.DoesNotContain("|---", text);
    }
}
