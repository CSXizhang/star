using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

[CollectionDefinition("Companion UI globals", DisableParallelization = true)]
public sealed class CompanionUiGlobalsCollection { }

[Collection("Companion UI globals")]
public sealed class CompanionDashboardRefreshStateTests
{
    [Fact]
    public void UnchangedHistoryFramesAndWorkProgressKeepCachedLayout()
    {
        var state = new LifeMenuUiState();
        var task = new CompanionTaskPanelState();
        var cache = new CompanionDashboardRefreshState();
        Assert.True(cache.Changed(state, task, 1280, 720, 0, 0));
        for (int frame = 0; frame < 600; frame++)
        {
            task.NativeProgress("浇水", "进行中", frame, 600);
            Assert.False(cache.Changed(state, task, 1280, 720, 0, 0));
        }
        state.RecordConversation("reply", "伙伴", "已浇完。", "1:spring:1", false, unread: true);
        Assert.True(cache.Changed(state, task, 1280, 720, 0, 0));
        state.MarkConversationRead("reply");
        Assert.False(cache.Changed(state, task, 1280, 720, 0, 0));
        Assert.True(cache.Changed(state, task, 1280, 720, 0, 80));
        Assert.False(cache.Changed(state, task, 1280, 720, 0, 80));
        Assert.True(cache.Changed(state, task, 960, 540, 0, 80));
    }

    [Fact]
    public void WorkAndUsageChangesRefreshTheirRespectivePages()
    {
        var state = new LifeMenuUiState();
        var task = new CompanionTaskPanelState();
        var cache = new CompanionDashboardRefreshState();
        Assert.True(cache.Changed(state, task, 1280, 720, 1, 0));
        Assert.False(cache.Changed(state, task, 1280, 720, 1, 0));
        task.ApplyConfirmedPause(true);
        Assert.True(cache.Changed(state, task, 1280, 720, 1, 0));
        Assert.True(cache.Changed(state, task, 1280, 720, 2, 0));
        Assert.False(cache.Changed(state, task, 1280, 720, 2, 0));
        task.ApplyUsageToday("今日用量：1000 tokens");
        Assert.True(cache.Changed(state, task, 1280, 720, 2, 0));
        Assert.False(cache.Changed(state, task, 1280, 720, 2, 0));
    }

    [Fact]
    public void DaytimeRestShowsRecoveryAndResumptionInsteadOfWaitingForTomorrow()
    {
        var task = new CompanionTaskPanelState();
        task.SetRestState("resting", null);
        Assert.Equal("休息恢复体力", task.Stage);
        Assert.Contains("恢复体力", task.Current);
        Assert.Equal("恢复体力后继续原来的工作。", task.NextStep);
        task.SetRestState("sleeping", null);
        Assert.Equal("已就寝", task.Stage);
        Assert.Equal("明天醒来后继续安排。", task.NextStep);
    }

    [Fact]
    public void FailedModeChangeClearsSavingLabelEvenWhenConfirmedModeIsUnchanged()
    {
        bool originalPending = CompanionCommandMenu.ModeChangePending;
        try
        {
            var state = new LifeMenuUiState();
            var task = new CompanionTaskPanelState();
            var cache = new CompanionDashboardRefreshState();
            CompanionCommandMenu.ModeChangePending = false;
            Assert.True(cache.Changed(state, task, 1280, 720, 2, 0));
            CompanionCommandMenu.ModeChangePending = true;
            Assert.True(cache.Changed(state, task, 1280, 720, 2, 0));
            CompanionCommandMenu.ModeChangePending = false;
            Assert.True(cache.Changed(state, task, 1280, 720, 2, 0));
            Assert.False(cache.Changed(state, task, 1280, 720, 2, 0));
            Assert.True(cache.Changed(state, task, 1280, 720, 0, 0));
            CompanionCommandMenu.ModeChangePending = true;
            Assert.False(cache.Changed(state, task, 1280, 720, 0, 0));
        }
        finally { CompanionCommandMenu.ModeChangePending = originalPending; }
    }
}
