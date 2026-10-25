using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class ConversationDeliveryStateTests
{
    [Fact]
    public void CareHintsPersistAndDoNotReappearAfterChatDelivery()
    {
        var state = new LifeMenuUiState();
        var hint = new StardewAI.Companion.Mod.Domain.PendingCareHint("morning", "1:spring:1", "morning", "早上好，今天先浇水。");
        state.AddUnreadCareHint(hint);
        state.AddUnreadCareHint(hint);
        Assert.Single(state.Conversation);
        var restored = new LifeMenuUiState();
        restored.RestoreConversation(state.Conversation);
        var entry = Assert.Single(restored.Conversation);
        Assert.Equal(hint.Text, entry.Text);
        Assert.True(entry.Unread);
        restored.MarkConversationRead(entry.Id);
        restored.AddUnreadCareHint(hint);
        Assert.Equal(0, restored.UnreadReplyCount);
        Assert.Single(restored.Conversation);
    }

    [Fact]
    public void LateReplyForPersistedPlayerMessageSurvivesPendingStateReset()
    {
        var state = new LifeMenuUiState();
        state.RecordConversation("old:player", "你", "材料还缺多少？", "1:spring:1", true);
        state.BeginChat("new", "chat");
        Assert.True(state.ApplyChatReply("old", "completed", "还缺木材。", null, null, 0, 0));
        Assert.Equal("new", state.PendingChatRequestId);
        state.RecordConversation("old:companion", "伙伴", "还缺木材。", "1:spring:1", false, true);
        Assert.False(state.ApplyChatReply("old", "completed", "还缺木材。", null, null, 0, 0));
        Assert.False(state.ApplyChatReply("unknown", "completed", "错误消息", null, null, 0, 0));
    }

    [Fact]
    public void ExplicitDecisionDraftCannotAnswerResolvedQuestion()
    {
        var state = new LifeMenuUiState();
        state.TryAddDecisionNotice("farm", "farm", "trees", "1:spring:1", "树要保留吗？");
        state.TryAddDecisionNotice("farm", "farm", "seeds", "1:spring:1", "明天买种子吗？");
        var flow = new CompanionF8Flow();
        Assert.Null(flow.PendingReplyTarget(state));
        Assert.Equal("trees", flow.PendingReplyTarget(state, "trees", includePresented: false));
        Assert.Equal("seeds", flow.PendingReplyTarget(state, "seeds"));
        Assert.Equal(2, state.PendingDecisions.Count);
        state.ConfirmDecisionReply("trees");
        Assert.Null(flow.PendingReplyTarget(state));
        Assert.Equal("seeds", flow.PendingReplyTarget(state, "seeds"));
        state.SetDecisionStatus("seeds", "dismissed");
        Assert.Null(flow.PendingReplyTarget(state, "trees", includePresented: false));
        Assert.Null(flow.PendingReplyTarget(state, "seeds"));
    }

}
