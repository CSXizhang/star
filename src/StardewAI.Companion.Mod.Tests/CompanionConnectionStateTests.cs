using StardewAI.Companion.Mod.Menus;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class CompanionConnectionStateTests
{
    [Fact]
    public void FailedPersistenceRemainsRetryableWithoutRepeatingRuntimeHandoff()
    {
        var state = new CompanionConnectionState();
        state.Initialize("invalid-old-setting");
        Assert.Equal("builtin", state.Source);
        state.Begin("switch", "external");
        Assert.True(state.Apply(new("external", "waiting", "switch")));
        // A failed file write does not mark the acknowledged selection as saved.
        Assert.True(state.NeedsPersistence);
        Assert.False(state.IsSwitching);
        state.Apply(new("external", "connected"));
        Assert.True(state.NeedsPersistence);
        state.ResetConnection(); // Returning to title must not swallow a failed write.
        Assert.True(state.NeedsPersistence);
        state.NoteSaved("external");
        Assert.False(state.NeedsPersistence);
    }

    [Fact]
    public void StaleStatusCannotConfirmHandoffOrReopenActions()
    {
        var state = new CompanionConnectionState();
        state.Begin("switch-2", "external");
        Assert.False(state.Apply(new("builtin", "connected")));
        Assert.False(state.Apply(new("external", "connected", "switch-1")));
        Assert.True(state.IsSwitching);
        Assert.Equal("builtin", state.Source);
        Assert.True(state.Apply(new("external", "waiting", "switch-2")));
        Assert.False(state.IsSwitching);
        Assert.Equal("external", state.Source);
    }

    [Fact]
    public void FailedHandoffStaysClosedAndDisconnectCannotSelectBuiltinFallback()
    {
        var state = new CompanionConnectionState();
        state.Initialize("external");
        state.Apply(new("external", "connected", ClientName: "原客户端", ConversationName: "原伙伴"));
        Assert.Contains("原伙伴", state.Description);
        state.Apply(new("external", "disconnected"));
        Assert.Equal("external", state.Source);
        Assert.Contains("等待", state.Description);
        state.Begin("switch", "builtin");
        Assert.False(state.Apply(new("builtin", "error", "switch")));
        Assert.True(state.IsSwitching);
        Assert.Equal("external", state.Source);
    }
}
