using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Menus;
using StardewAI.Companion.Mod.Transport;
using System.Text.Json;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>Independent acceptance of durable visible history, unread identity and save isolation.</summary>
public sealed class ConversationAcceptanceTests
{
    [Fact]
    public void CurrentPlanUsesSelectedScopeRatherThanOldActiveGoalOrCancelledWork()
    {
        var work = new CompanionWorkStateDto("command", false, Goal: "偏好：赚钱", ActiveGoals: new()
        {
            new("old", "旧安排：取水壶", "active"),
            new("cancelled", "已取消的种植", "cancelled"),
            new("current", "今天负责种植畜牧", "active")
        }, CurrentGoalId: "current");
        Assert.Equal("今天负责种植畜牧", CompanionTaskPanelState.CurrentGoalText(work));
        Assert.Equal("current", CompanionTaskPanelState.SelectCurrentGoal(work)?.Id);
        Assert.Null(CompanionTaskPanelState.SelectCurrentGoal(work with { CurrentGoalId = "cancelled" }));
        Assert.Equal("偏好：赚钱", CompanionTaskPanelState.CurrentGoalText(work with { CurrentGoalId = null }));
    }

    [Fact]
    public void FailureRecordShowsActualDateTimeAndReasonWithoutInventingMissingDates()
    {
        var record = new RecentExecutionDto("command", "补满水壶", "1:spring:16", "failed", "未能完成补水",
            ReasonCode: "WAITING_TIMEOUT", Reason: "通路被堵住，请先清理水边通路。", GameTime: 930,
            RecordedAt: "2026-10-02T08:19:09Z");
        string rendered = CompanionTaskPanelState.ExecutionRecordText(record);
        Assert.Contains("第1年 春16日 09:30", rendered);
        Assert.Contains("失败", rendered);
        Assert.Contains("通路被堵住", rendered);
        Assert.DoesNotContain("原因已记录", rendered);
        string oldRendered = CompanionTaskPanelState.ExecutionRecordText(record with { GameDate = null, GameTime = null, RecordedAt = null });
        Assert.Contains("日期未记录", oldRendered);
        Assert.Contains("记录时间未知", oldRendered);
        Assert.DoesNotContain("春16日", oldRendered);
    }

    [Fact]
    public void OptionalPlanAndFailureFieldsRoundTripAndOldPayloadStillLoads()
    {
        var work = new CompanionWorkStateDto("command", true, Goal: "照料主田区", CurrentGoalId: "current",
            PauseReason: "会话恢复失败，等待重试", UpdatedAt: "2026-10-02T08:24:18Z", RecentExecutions: new()
            { new("job", "浇水", "1:spring:16", "partial", "浇了8格", ReasonCode: "WATER_INCOMPLETE", Reason: "水不够", GameTime: 920, RecordedAt: "2026-10-02T08:19:08Z") });
        var decoded = JsonSerializer.Deserialize<CompanionWorkStateDto>(JsonSerializer.Serialize(work))!;
        Assert.Equal(work.CurrentGoalId, decoded.CurrentGoalId);
        Assert.Equal(work.PauseReason, decoded.PauseReason);
        Assert.Equal(work.UpdatedAt, decoded.UpdatedAt);
        Assert.Equal("水不够", Assert.Single(decoded.RecentExecutions!).Reason);
        var legacy = JsonSerializer.Deserialize<CompanionWorkStateDto>("{\"mode\":\"command\",\"paused\":false}")!;
        Assert.Null(legacy.CurrentGoalId);
        Assert.Null(legacy.UpdatedAt);
        Assert.Null(legacy.RecentExecutions);
    }

    [Fact]
    public void ClosedPanelReplySurvivesReopenAndDuplicateDeliveryDoesNotNotifyAgain()
    {
        string directory = Path.Combine(Path.GetTempPath(), "companion-conversation-" + Guid.NewGuid());
        try
        {
            var state = new LifeMenuUiState();
            var archive = new CompanionConversationStore(directory);
            int changed = 0;
            state.ConversationChanged += () => { changed++; archive.Save("farm-a", state.Conversation); };
            Assert.True(state.RecordConversation("request-1:player", "你", "先把田浇完", "1:spring:16", true));
            // No menu is constructed: a reply received after closing it must still be persisted.
            Assert.True(state.RecordConversation("request-1:reply", "阿星", "田里交给我。", "1:spring:16", false, unread: true));
            Assert.False(state.RecordConversation("request-1:reply", "阿星", "田里交给我。", "1:spring:16", false, unread: true));
            Assert.Equal(2, changed);
            Assert.Equal(1, state.UnreadReplyCount);
            var reopened = new LifeMenuUiState();
            reopened.RestoreConversation(new CompanionConversationStore(directory).Load("farm-a"));
            Assert.Equal(new[] { "先把田浇完", "田里交给我。" }, reopened.Conversation.Select(e => e.Text));
            Assert.All(reopened.Conversation, e => Assert.Equal("1:spring:16", e.GameDate));
            Assert.Equal(1, reopened.UnreadReplyCount);
            reopened.MarkConversationRead();
            Assert.Equal(0, reopened.UnreadReplyCount);
            Assert.Equal(2, reopened.Conversation.Count);
        }
        finally { if (Directory.Exists(directory)) Directory.Delete(directory, recursive: true); }
    }

    [Fact]
    public void EqualRepliesFromDifferentRequestsRemainSeparateAndFarmSwitchDoesNotLeakHistory()
    {
        string directory = Path.Combine(Path.GetTempPath(), "companion-conversation-" + Guid.NewGuid());
        try
        {
            var state = new LifeMenuUiState();
            Assert.True(state.RecordConversation("first", "阿星", "好，我来。", "1:spring:16", false, true));
            Assert.True(state.RecordConversation("second", "阿星", "好，我来。", "1:spring:17", false, true));
            Assert.Equal(2, state.UnreadReplyCount);
            var store = new CompanionConversationStore(directory);
            store.Save("farm-a", state.Conversation);
            store.Save("farm-b", new[] { new CompanionConversationEntry("other", "你", "农场二的消息", "2:fall:3", true) });
            state.RestoreConversation(store.Load("farm-b"));
            Assert.Single(state.Conversation);
            Assert.Equal("农场二的消息", state.Conversation[0].Text);
            Assert.Equal(0, state.UnreadReplyCount);
            state.RestoreConversation(store.Load("farm-a"));
            Assert.Equal(2, state.Conversation.Count);
            Assert.Equal(new[] { "1:spring:16", "1:spring:17" }, state.Conversation.Select(e => e.GameDate));
            Assert.Empty(store.Load("farm-that-never-existed"));
        }
        finally { if (Directory.Exists(directory)) Directory.Delete(directory, recursive: true); }
    }

    [Fact]
    public void FailedExchangeTerminatesPendingRequestAndRetainsDatedReadableFailure()
    {
        var state = new LifeMenuUiState();
        state.BeginChat("current", "chat");
        Assert.False(state.ApplyChatReply("old", "failed", "过时失败", null, "OLD", 1, 1));
        Assert.True(state.IsChatPending);
        Assert.True(state.ApplyChatReply("current", "failed", "会话恢复失败，请重试。", null, "DSH_RUNTIME_FAILED", 1, 1));
        Assert.False(state.IsChatPending);
        Assert.Equal(LifeChatStatus.Failed, state.ChatStatus);
        Assert.Equal("DSH_RUNTIME_FAILED", state.ErrorText);
        state.RecordConversation("current:failure", "阿星", state.ReplyText!, "1:spring:16", false, true);
        Assert.Equal("1:spring:16", Assert.Single(state.Conversation).GameDate);
        Assert.Contains("会话恢复失败", state.Conversation[0].Text);
        Assert.Equal(1, state.UnreadReplyCount);
    }
}
