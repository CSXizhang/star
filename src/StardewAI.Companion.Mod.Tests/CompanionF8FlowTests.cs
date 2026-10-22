using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class CompanionF8FlowTests
{
    [Fact]
    public void OpeningWithoutUnreadSpeechGoesStraightToChoices()
    {
        var state = new LifeMenuUiState();
        state.RecordConversation("read", "伙伴", "已经看过", "1:spring:1", false);
        state.RecordConversation("player", "你", "玩家输入", "1:spring:1", true, unread: true);
        var flow = new CompanionF8Flow();
        flow.Begin(state);
        Assert.Null(flow.TakeNext(state));
    }

    [Fact]
    public void SelectingSpeechDoesNotConsumeUnreadUntilItIsActuallyPresented()
    {
        var state = new LifeMenuUiState();
        state.RecordConversation("first", "伙伴", "第一条", "1:spring:1", false, unread: true);
        state.RecordConversation("second", "伙伴", "第二条", "1:spring:1", false, unread: true);
        var flow = new CompanionF8Flow();
        flow.Begin(state);
        Assert.Equal("first", flow.TakeNext(state)?.Id);
        Assert.Equal(2, state.UnreadReplyCount);
        flow.Presented(state, "first");
        Assert.Equal(1, state.UnreadReplyCount);
        Assert.Equal("second", flow.TakeNext(state)?.Id);
        // Closing before rendering leaves the second speech for the next F8.
        flow.Begin(state);
        Assert.Equal("second", flow.TakeNext(state)?.Id);
    }

    [Fact]
    public void LaterArrivalsWaitForTheNextPlayerInteraction()
    {
        var state = new LifeMenuUiState();
        state.RecordConversation("first", "伙伴", "第一条", "1:spring:1", false, unread: true);
        var flow = new CompanionF8Flow();
        flow.Begin(state);
        Assert.Equal("first", flow.TakeNext(state)?.Id);
        state.RecordConversation("new", "伙伴", "新消息", "1:spring:1", false, unread: true);
        flow.Presented(state, "first");
        Assert.Null(flow.TakeNext(state));
        Assert.True(state.Conversation.Single(e => e.Id == "new").Unread);
        flow.Begin(state);
        Assert.Equal("new", flow.TakeNext(state)?.Id);
    }

    [Fact]
    public void DecisionReplyKeepsItsIdentityWithoutReadingResolvingIt()
    {
        var state = new LifeMenuUiState();
        state.TryAddDecisionNotice("farm", "farm", "trees", "1:spring:1", "树要保留吗？");
        state.TryAddDecisionNotice("farm", "farm", "seeds", "1:spring:1", "明天买种子吗？");
        var flow = new CompanionF8Flow();
        flow.Begin(state);
        flow.Presented(state, flow.TakeNext(state)!.Id);
        Assert.Equal("trees", flow.PendingReplyTarget(state));
        Assert.Null(flow.PendingReplyTarget(state, includePresented: false));
        Assert.Equal("seeds", flow.PendingReplyTarget(state, "seeds"));
        Assert.Equal(2, state.PendingDecisions.Count);
        state.ConfirmDecisionReply("trees");
        Assert.Null(flow.PendingReplyTarget(state));
        Assert.Equal("seeds", flow.PendingReplyTarget(state, "seeds"));
        state.SetDecisionStatus("seeds", "dismissed");
        Assert.Null(flow.TakeNext(state));
        Assert.Null(flow.PendingReplyTarget(state, "seeds"));
    }

    [Fact]
    public void ReplyingToLaterOrdinarySpeechDoesNotAnswerAnEarlierDecision()
    {
        var state = new LifeMenuUiState();
        state.TryAddDecisionNotice("farm", "farm", "trees", "1:spring:1", "树要保留吗？");
        state.RecordConversation("chat", "伙伴", "今天真好。", "1:spring:1", false, unread: true);
        var flow = new CompanionF8Flow();
        flow.Begin(state);
        flow.Presented(state, flow.TakeNext(state)!.Id);
        flow.Presented(state, flow.TakeNext(state)!.Id);
        Assert.Null(flow.PendingReplyTarget(state));
        Assert.Single(state.PendingDecisions);
    }
}
