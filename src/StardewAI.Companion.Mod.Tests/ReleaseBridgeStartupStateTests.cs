using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class ReleaseBridgeStartupStateTests
{
    private static readonly DateTime Now = new(2026, 10, 2, 0, 0, 0, DateTimeKind.Utc);

    [Fact]
    public void EarlyFailureIsImmediateButSameRetryErrorNotAnnouncedAgainUntilRecovered()
    {
        var state = new ReleaseBridgeStartupState();
        var failed = Task.FromResult(new ReleaseBridgeLaunchResult(1, "未找到 codex 客户端。"));
        state.Begin(failed, Now);
        Assert.Equal("未找到 codex 客户端。", state.Update(false, Now.AddSeconds(1)));
        Assert.False(state.CanStart(Now.AddSeconds(20)));
        Assert.True(state.CanStart(Now.AddSeconds(31)));
        state.Begin(failed, Now.AddSeconds(31));
        Assert.Null(state.Update(false, Now.AddSeconds(32)));
        Assert.Equal("未找到 codex 客户端。", state.Problem);
        Assert.Null(state.Update(true, Now.AddSeconds(33)));
        Assert.Null(state.Problem);
        state.Begin(failed, Now.AddSeconds(65));
        Assert.NotNull(state.Update(false, Now.AddSeconds(66)));
    }

    [Fact]
    public void SuccessfulExitWithoutHandshakeTimesOutAndLiveLauncherCannotBeDuplicated()
    {
        var state = new ReleaseBridgeStartupState();
        state.Begin(Task.FromResult(new ReleaseBridgeLaunchResult(0, null)), Now);
        Assert.False(state.CanStart(Now.AddSeconds(31)));
        Assert.Null(state.Update(false, Now.AddSeconds(59)));
        Assert.True(state.IsStarting);
        Assert.Contains("60 秒", state.Update(false, Now.AddSeconds(60)));
        Assert.Null(state.Update(false, Now.AddSeconds(61)));
        Assert.True(state.CanStart(Now.AddSeconds(61)));
        var live = new TaskCompletionSource<ReleaseBridgeLaunchResult>();
        state.Reset();
        state.Begin(live.Task, Now);
        Assert.NotNull(state.Update(false, Now.AddSeconds(60)));
        Assert.False(state.CanStart(Now.AddSeconds(90)));
        live.SetResult(new ReleaseBridgeLaunchResult(7, "启动器后来报告具体失败。"));
        Assert.Equal("启动器后来报告具体失败。", state.Update(false, Now.AddSeconds(91)));
    }

    [Fact]
    public void HandshakeAndSaveResetDiscardOldStartupFailure()
    {
        var state = new ReleaseBridgeStartupState();
        var delayed = new TaskCompletionSource<ReleaseBridgeLaunchResult>();
        state.Begin(delayed.Task, Now);
        state.Update(true, Now.AddSeconds(1));
        delayed.SetResult(new ReleaseBridgeLaunchResult(1, "旧启动错误"));
        Assert.Null(state.Update(false, Now.AddSeconds(61)));
        Assert.Null(state.Problem);
        state.Begin(Task.FromResult(new ReleaseBridgeLaunchResult(1, "旧存档错误")), Now.AddSeconds(62));
        state.Reset();
        Assert.Null(state.Update(false, Now.AddSeconds(63)));
        Assert.True(state.CanStart(Now.AddSeconds(63)));
    }
}
