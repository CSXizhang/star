using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class CompanionTaskPanelStateTests
{
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
        Assert.Contains("服务", state.NextStep);
        state.Reset();
        Assert.Null(state.LastResult);
        Assert.Null(state.Goal);
    }
}
