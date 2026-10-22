using System.Text.Json;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Menus;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class UnifiedConversationTests
{
    [Fact]
    public void ReadingAndChangingDayNeverResolvesDecision()
    {
        var state = new LifeMenuUiState();
        Assert.True(state.TryAddDecisionNotice("farm", "farm", "question", "1:spring:1", "树保留吗？"));
        state.MarkConversationRead(); state.MarkDecisionNoticesRead(); state.MarkAllCareHintsRead();
        Assert.Single(state.PendingDecisions);
        Assert.False(state.TryAddDecisionNotice("farm", "farm", "question", "1:spring:2", "树保留吗？"));
        state.RecordConversation("reply:player", "你", "保留", "1:spring:2", true, replyToDecisionId: "question");
        Assert.Single(state.PendingDecisions); // Submission alone is not an acknowledgement.
        state.ConfirmDecisionReply("question");
        Assert.Empty(state.PendingDecisions);
        Assert.Equal(2, state.Conversation.Count);
    }

    [Fact]
    public void PendingDecisionSurvivesHistoryRetentionAndReload()
    {
        string dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        try
        {
            var state = new LifeMenuUiState();
            state.TryAddDecisionNotice("farm", "farm", "question", "1:spring:1", "树保留吗？");
            for (int i = 0; i < 250; i++) state.RecordConversation("chat-" + i, "伙伴", "日常对话", "1:spring:2", false);
            var store = new CompanionConversationStore(dir);
            store.Save("farm", state.Conversation);
            var restored = new LifeMenuUiState(); restored.RestoreConversation(store.Load("farm"));
            Assert.Single(restored.PendingDecisions);
            Assert.False(restored.TryAddDecisionNotice("farm", "farm", "question", "1:spring:3", "树保留吗？"));
            Assert.Empty(store.Load("other"));
        }
        finally { if (Directory.Exists(dir)) Directory.Delete(dir, true); }
    }

    [Fact]
    public void DecisionReplyWirePreservesIdentityAndLegacyFields()
    {
        var submit = new LifeChatSubmitPayload("r", "farm", "chat", "保留", ReplyToNoticeId: "question");
        string wire = JsonSerializer.Serialize(submit);
        Assert.Contains("\"replyToNoticeId\":\"question\"", wire);
        Assert.Equal(submit, JsonSerializer.Deserialize<LifeChatSubmitPayload>(wire));
        var reply = new LifeChatReplyPayload("r", "farm", "completed", 1, 1, AnsweredNoticeId: "question");
        Assert.Equal("question", JsonSerializer.Deserialize<LifeChatReplyPayload>(JsonSerializer.Serialize(reply))?.AnsweredNoticeId);
    }
}
