using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class CompanionTaskPanelStateTests
{
    [Fact]
    public void StartupFailureIsSeparateFromRealResultAndClearsOnHandshake()
    {
        var state = new CompanionTaskPanelState();
        state.Complete("已浇好八块地。");
        state.Disconnected("未找到 codex 客户端。", startupFailed: true);
        Assert.Equal("启动失败", state.Stage);
        Assert.Equal("未找到 codex 客户端。", state.ConnectionProblem);
        Assert.Equal("已浇好八块地。", state.Current);
        Assert.Equal("已浇好八块地。", state.LastResult);
        state.Connected();
        Assert.Null(state.ConnectionProblem);
        Assert.Equal("本次结果", state.Stage);
        state.ApplyConfirmedPause(true);
        string pauseNext = state.NextStep;
        state.Disconnected("未找到 codex 客户端。", startupFailed: true);
        Assert.Equal("已暂停", state.Stage);
        Assert.Equal(pauseNext, state.NextStep);
        state.ApplyConfirmedCancel();
        Assert.Contains("已取消", state.Current);
        Assert.Equal("已浇好八块地。", state.LastResult);
        state.Connected();
        Assert.Equal("已暂停", state.Stage);
        Assert.Null(state.ConnectionProblem);
        state.Reset();
        Assert.Null(state.ConnectionProblem);
    }

    [Fact]
    public void ConfirmedCancelDoesNotOfferToResumeCancelledWorkAfterRefresh()
    {
        var state = new CompanionTaskPanelState();
        state.Begin("收取成熟机器", false);
        state.Complete("已入箱两份蛋黄酱");
        state.ApplyConfirmedPause(true);
        state.ApplyConfirmedCancel();
        state.ApplyProjection("workhorse", "旧目标", Array.Empty<string>(), null, true);
        state.ApplyActivity("working", "旧任务", "旧下一步");
        Assert.Null(state.Goal);
        Assert.Contains("已取消", state.Current);
        Assert.Contains("只解除暂停", state.NextStep);
        Assert.Equal("已入箱两份蛋黄酱", state.LastResult);
        Assert.False(state.Running);
        state.ApplyConfirmedPause(false);
        Assert.Equal("已取消", state.Stage);
        Assert.DoesNotContain("原有安排保留", state.NextStep);
        state.Begin("新工作", false);
        state.ApplyProjection("workhorse", "新目标", Array.Empty<string>(), null, false);
        Assert.Equal("新目标", state.Goal);
        Assert.Equal("新工作", state.Current);
    }
    [Fact]
    public void DailyUsageSummaryHasNoLimitAndPreservesOmittedButClearsEmptyAndNewSave()
    {
        var state = new CompanionTaskPanelState();
        const string text = "2026-10-02 · 今日token 9,000,000 · API估算 $18.23；订阅实际费用不可用";
        state.ApplyUsageToday(text);
        state.ApplyUsageToday(null);
        Assert.Equal(text, state.UsageTodayText);
        state.ApplyUsageToday("");
        Assert.Null(state.UsageTodayText);
        state.ApplyUsageToday(text);
        state.Reset();
        Assert.Null(state.UsageTodayText);
        Assert.Equal("取消当前安排", CompanionCommandMenu.CancelButtonText);
        string oldMode = CompanionCommandMenu.AutonomyMode;
        bool oldPending = CompanionCommandMenu.ModeChangePending;
        try
        {
            CompanionCommandMenu.ModeChangePending = false;
            CompanionCommandMenu.AutonomyMode = "free";
            Assert.Equal("主动帮忙：开", CompanionCommandMenu.ModeButtonText);
        }
        finally { CompanionCommandMenu.AutonomyMode = oldMode; CompanionCommandMenu.ModeChangePending = oldPending; }
    }
    [Fact]
    public void LateProviderAndNativeUpdatesCannotOverwriteConfirmedPause()
    {
        var state = new CompanionTaskPanelState();
        state.ApplyActivity("working", "喂食并照料十只鸡", "加工空档继续种植");
        state.ApplyProjection("workhorse", "完成农场目标", Array.Empty<string>(), null, true);
        state.ApplyExecutionHistory(new[] { new Transport.RecentExecutionDto("feed", "鸡舍喂食", "1:summer:18", "completed", "已放入12份饲料") });
        state.ApplyActivity("working", "已选择，等待执行", "关闭面板继续");
        state.NativeProgress("拾蛋", "执行", 1, 6);
        Assert.Equal("已暂停", state.Stage);
        Assert.Contains("已暂停", state.Current);
        Assert.Contains("继续", state.NextStep);
        Assert.Contains("已暂停", state.WaitReason);
        Assert.Equal("鸡舍喂食：已放入12份饲料", state.LastResult);
        Assert.Null(state.Progress);
        Assert.False(state.Running);
        state.ApplyProjection("workhorse", "完成农场目标", Array.Empty<string>(), null, false);
        state.ApplyActivity("working", "当前照料十只鸡", "收取成熟机器");
        Assert.True(state.Running);
        Assert.Equal("当前照料十只鸡", state.Current);
    }

    [Fact]
    public void RecentExecutionProjectionIsBoundedDeduplicatedAndClearedBetweenSaves()
    {
        var state = new CompanionTaskPanelState();
        var records = Enumerable.Range(0, 10).Select(i => new Transport.RecentExecutionDto(i.ToString(), "工作", null, "completed", "真实结果")).ToList();
        records.Add(records[^1]);
        state.ApplyExecutionHistory(records);
        Assert.Equal(8, state.RecentExecutions.Count);
        Assert.Equal("2", state.RecentExecutions[0].CommandId);
        Assert.Equal("9", state.RecentExecutions[^1].CommandId);
        Assert.Equal("工作：真实结果", state.LastResult);
        state.ApplyExecutionHistory(null);
        Assert.Equal(8, state.RecentExecutions.Count);
        state.Reset();
        Assert.Empty(state.RecentExecutions);
    }

    [Fact]
    public void ImportantNoticeIsSaveScopedAndShownOnlyOnceWithoutOwningAChatRequest()
    {
        var state = new LifeMenuUiState();
        Assert.False(state.TryAddDecisionNotice("save-a", "save-b", "notice-1", "1:spring:3", "材料用完了。"));
        Assert.True(state.TryAddDecisionNotice("save-a", "save-a", "notice-1", "1:spring:3", "材料用完了。"));
        Assert.False(state.TryAddDecisionNotice("save-a", "save-a", "notice-1", "1:spring:3", "材料用完了。"));
        Assert.Single(state.RecentCareHints);
        Assert.Equal("材料用完了。", state.RecentCareHints[0].Text);
        state.MarkDecisionNoticesRead();
        Assert.Empty(state.UnreadCareHints);
        Assert.Single(state.RecentCareHints);
        state.Reset();
        Assert.Empty(state.RecentCareHints);
    }

    [Fact]
    public void RealProgressAndResultRemainVisibleAcrossProfileRefresh()
    {
        var state = new CompanionTaskPanelState();
        state.Begin("照料农田", false);
        state.NativeProgress("浇水", "照料作物", 3, 8);
        Assert.Equal("浇水 3/8", state.Progress);
        state.Complete("已浇好八块地。");
        state.ApplyProjection("workhorse", "照料农田", System.Array.Empty<string>(), null, false);
        state.ApplyActivity("idle", "暂时待命", "可以聊聊");
        Assert.Equal("已浇好八块地。", state.Current);
        Assert.Equal(state.Current, state.LastResult);
        Assert.Null(state.Progress);
        state.Begin("另一个安排", true);
        Assert.Equal("正在商量", state.Stage);
        Assert.Equal("已浇好八块地。", state.LastResult);
        state.ApplyActivity("working", "正在处理：春末菜地播种并浇水", "鸡舍施工后继续购鸡。");
        state.NativeProgress("加水", "已完成", 1, 1);
        Assert.Equal("正在处理：春末菜地播种并浇水", state.Current);
        Assert.Equal("已浇好八块地。", state.LastResult);
        Assert.Equal("鸡舍施工后继续购鸡。", state.NextStep);
        Assert.True(state.Running);
    }

    [Fact]
    public void TerminalNativeProgressDoesNotLeavePanelWorkingOrOldFailureVisible()
    {
        var state = new CompanionTaskPanelState();
        state.Complete("旧位置被牧草占用。", failed: true);
        state.NativeProgress("建造建筑", "执行", 0, 1);
        state.NativeProgress("建造建筑", "已完成", 1, 1);
        Assert.False(state.Running);
        Assert.Equal("本次结果", state.Stage);
        Assert.Contains("建造建筑 1/1", state.LastResult);
        Assert.DoesNotContain("牧草", state.LastResult);
    }

    [Fact]
    public void WaitingAndPauseAreNotFakeProgressAndSaveResetDropsOldOutcome()
    {
        var state = new CompanionTaskPanelState();
        state.ApplyActivity("waiting", "明天才到准备日期。", "明天再核对。");
        Assert.False(state.Running);
        Assert.Null(state.Progress);
        Assert.Equal("明天才到准备日期。", state.WaitReason);
        state.ApplyProjection("decor", "整理庭院", System.Array.Empty<string>(), null, true);
        Assert.Equal("已暂停", state.Stage);
        state.Complete("已完成清理。");
        state.Disconnected();
        Assert.NotNull(state.LastResult);
        Assert.Contains("服务", state.ConnectionProblem);
        Assert.Contains("继续", state.NextStep);
        state.Reset();
        Assert.Null(state.LastResult);
        Assert.Null(state.Goal);
    }
}
