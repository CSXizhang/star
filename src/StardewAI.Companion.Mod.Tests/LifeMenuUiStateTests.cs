using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>Contract §3 pure-logic projections for the life-menu UI state machine.</summary>
public class LifeMenuUiStateTests
{
    private static LifeMenuUiState NewState() => new();

    // ------------------------------------------------------------ chat submit/queued/done/failed

    [Fact]
    public void BeginChat_EntersSending_AndRejectsConcurrentSubmit()
    {
        var state = NewState();
        Assert.True(state.BeginChat("req-1", "chat"));
        Assert.True(state.IsChatPending);
        Assert.Equal(LifeChatStatus.Sending, state.ChatStatus);
        Assert.Equal("req-1", state.PendingChatRequestId);
        Assert.False(state.BeginChat("req-2", "plan"), "a second submit must not displace the in-flight one");
        Assert.Equal("req-1", state.PendingChatRequestId);
    }

    [Fact]
    public void ProcessingReply_KeepsRequestPending_AndTracksRevisions()
    {
        var state = NewState();
        state.BeginChat("req-1", "chat");
        Assert.True(state.ApplyChatReply("req-1", "processing", null, null, null, 3, 7));
        Assert.Equal(LifeChatStatus.Processing, state.ChatStatus);
        Assert.True(state.IsChatPending, "processing is not terminal; the request stays pending");
        Assert.Equal(3, state.ProfileRevision);
        Assert.Equal(7, state.MemoryRevision);
    }

    [Fact]
    public void QueuedReply_ShowsQueuePosition_AndStaysPending()
    {
        var state = NewState();
        state.BeginChat("req-1", "chat");
        Assert.True(state.ApplyChatReply("req-1", "queued", null, 2, null, 3, 7));
        Assert.Equal(LifeChatStatus.Queued, state.ChatStatus);
        Assert.Equal(2, state.QueuePosition);
        Assert.True(state.IsChatPending);
    }

    [Fact]
    public void CompletedReply_ClearsPending_KeepsReplyText()
    {
        var state = NewState();
        state.BeginChat("req-1", "chat");
        Assert.True(state.ApplyChatReply("req-1", "completed", "早上好呀", null, null, 3, 7));
        Assert.Equal(LifeChatStatus.Completed, state.ChatStatus);
        Assert.False(state.IsChatPending);
        Assert.Null(state.PendingChatRequestId);
        Assert.Equal("早上好呀", state.ReplyText);
    }

    [Fact]
    public void FailedReply_RecordsError_AndClearsPending()
    {
        var state = NewState();
        state.BeginChat("req-1", "chat");
        Assert.True(state.ApplyChatReply("req-1", "failed", null, null, "模型暂时不可用", 3, 7));
        Assert.Equal(LifeChatStatus.Failed, state.ChatStatus);
        Assert.False(state.IsChatPending);
        Assert.Equal("模型暂时不可用", state.ErrorText);
    }

    [Fact]
    public void ReplyForOtherRequest_IsIgnored()
    {
        var state = NewState();
        state.BeginChat("req-1", "chat");
        Assert.False(state.ApplyChatReply("req-other", "completed", "别的回复", null, null, 9, 9));
        Assert.Equal(LifeChatStatus.Sending, state.ChatStatus);
        Assert.True(state.IsChatPending);
        Assert.Equal(0, state.ProfileRevision);
        Assert.True(state.ApplyChatReply("req-1", "completed", "本请求的回复", null, null, 3, 7));
        Assert.Equal("本请求的回复", state.ReplyText);
    }

    // ------------------------------------------------------------ profile state

    [Fact]
    public void ApplyProfileState_ProjectsFields_AndClearsPendingSet()
    {
        var state = NewState();
        state.BeginProfileSet("set-1", 4);
        state.ApplyProfileState(
            onboarded: false, skipped: false, companionName: "阿星", playStyle: "earn",
            personality: "gentle", careFrequency: "moderate", profileRevision: 4,
            workMode: "command", workPaused: false, dailySpendLimit: null, memoryRevision: 7,
            requestId: "get-1");
        Assert.Equal("set-1", state.PendingProfileSetRequestId);
        state.ApplyProfileState(
            onboarded: true, skipped: false, companionName: "小星", playStyle: "workhorse",
            personality: "calm", careFrequency: "quiet", profileRevision: 5,
            workMode: "free", workPaused: false, dailySpendLimit: 300, memoryRevision: 7,
            requestId: "set-1");
        Assert.True(state.IsOnboarded);
        Assert.False(state.IsSkipped);
        Assert.Equal("小星", state.CompanionName);
        Assert.Equal("workhorse", state.PlayStyle);
        Assert.Equal("calm", state.Personality);
        Assert.Equal("quiet", state.CareFrequency);
        Assert.Equal(5, state.ProfileRevision);
        Assert.Equal("free", state.WorkMode);
        Assert.False(state.WorkPaused);
        Assert.Equal(300, state.DailySpendLimit);
        Assert.Equal(7, state.MemoryRevision);
        Assert.Null(state.PendingProfileSetRequestId);
    }

    [Fact]
    public void BeginProfileSet_RecordsExpectedRevisionForOptimisticLock()
    {
        var state = NewState();
        state.BeginProfileSet("set-9", 12);
        Assert.Equal("set-9", state.PendingProfileSetRequestId);
        Assert.Equal(12, state.PendingProfileSetExpectedRevision);
    }

    // ------------------------------------------------------------ memory edit optimistic lock

    [Fact]
    public void MemoryEdit_TracksPendingRequest_UntilStateArrives()
    {
        var state = NewState();
        var entries = new[] { new MemoryEntrySnapshot("m-1", "agreement", "每天浇水", "player", "1:spring:1", "2026-09-25T00:00:00Z") };
        state.ApplyMemoryState(2, entries);
        Assert.Equal(2, state.MemoryRevision);
        Assert.Single(state.MemoryEntries);
        state.BeginMemoryEdit("edit-1");
        Assert.Equal("edit-1", state.PendingMemoryEditRequestId);
        // An unrelated list refresh must not claim the edit has persisted.
        state.ApplyMemoryState(2, entries, requestId: "list-1");
        Assert.Equal("edit-1", state.PendingMemoryEditRequestId);
        Assert.Null(state.MemoryEditFeedback);
        state.ApplyMemoryState(3, entries, requestId: "edit-1", status: "confirmed");
        Assert.Equal(3, state.MemoryRevision);
        Assert.Null(state.PendingMemoryEditRequestId);
        Assert.Equal("记忆已保存。", state.MemoryEditFeedback);
    }

    [Fact]
    public void RejectedMemoryEdit_GivesRetryFeedbackOnlyAfterMatchingReply()
    {
        var state = NewState();
        state.BeginMemoryEdit("edit-2");
        state.ApplyMemoryState(4, Array.Empty<MemoryEntrySnapshot>(),
            requestId: "edit-2", status: "rejected", reason: "STALE_REVISION");

        Assert.Null(state.PendingMemoryEditRequestId);
        Assert.Equal("记忆已变化，请查看最新列表后重试。", state.MemoryEditFeedback);
        state.BeginMemoryEdit("edit-3");
        Assert.Null(state.MemoryEditFeedback);
    }

    [Fact]
    public void WorkProjection_UsesConfirmedValues_AndClearsOnSaveChange()
    {
        var state = NewState();
        state.ApplyWorkProjection("优先赚钱", new[] { "收获作物" },
            new[] { "按需补种" }, new[] { "等待种子" }, null);

        Assert.Equal("优先赚钱", state.WorkGoal);
        Assert.Equal("收获作物", Assert.Single(state.ActiveGoalSummaries));
        Assert.Equal("按需补种", Assert.Single(state.RecentTodoSummaries));
        Assert.Equal("等待种子", Assert.Single(state.WaitingConditions));

        state.Reset();
        Assert.Null(state.WorkGoal);
        Assert.Empty(state.ActiveGoalSummaries);
        Assert.Empty(state.RecentTodoSummaries);
        Assert.Empty(state.WaitingConditions);
    }

    // ------------------------------------------------------------ care hints

    [Fact]
    public void UnreadCareHints_CapAtFive_DroppingOldest()
    {
        var state = NewState();
        for (int i = 1; i <= 7; i++)
            state.AddUnreadCareHint(new PendingCareHint($"key-{i}", "1:spring:1", "morning", $"第{i}条"));
        Assert.Equal(5, state.UnreadCareHints.Count);
        Assert.DoesNotContain(state.UnreadCareHints, h => h.EventKey == "key-1");
        Assert.DoesNotContain(state.UnreadCareHints, h => h.EventKey == "key-2");
        Assert.Equal("key-3", state.UnreadCareHints[0].EventKey);
        Assert.Equal("key-7", state.UnreadCareHints[^1].EventKey);
    }

    [Fact]
    public void MarkAllCareHintsRead_ClearsPendingList()
    {
        var state = NewState();
        state.AddUnreadCareHint(new PendingCareHint("key-1", "1:spring:1", "evening", "晚安"));
        state.MarkAllCareHintsRead();
        Assert.Empty(state.UnreadCareHints);
        Assert.Equal("晚安", Assert.Single(state.RecentCareHints).Text);
        state.AddUnreadCareHint(new PendingCareHint("key-1", "1:spring:1", "evening", "晚安"));
        Assert.Empty(state.UnreadCareHints); // A replay cannot badge the same message again.
    }

    [Fact]
    public void Reset_ClearsChatAndHints_ButKeepsProfile()
    {
        var state = NewState();
        state.ApplyProfileState(true, false, "小星", "earn", "gentle", "moderate", 5, "free", false, 100, 7);
        state.BeginChat("req-1", "chat");
        state.AddUnreadCareHint(new PendingCareHint("key-1", "1:spring:1", "morning", "早安"));
        state.Reset();
        Assert.Equal(LifeChatStatus.Idle, state.ChatStatus);
        Assert.False(state.IsChatPending);
        Assert.Empty(state.UnreadCareHints);
        Assert.Empty(state.RecentCareHints);
        Assert.True(state.IsOnboarded);
        Assert.Equal("小星", state.CompanionName);
        Assert.Equal(5, state.ProfileRevision);
    }
}
