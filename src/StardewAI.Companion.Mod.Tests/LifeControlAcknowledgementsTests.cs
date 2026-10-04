using StardewAI.Companion.Mod.Menus;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class LifeControlAcknowledgementsTests
{
    private static AutonomyStatePayload Ack(string action = "pause", bool paused = true, string request = "chat") =>
        new(request, "save", "command", paused, null, 0, "confirmed", DecisionEpoch: 4, ControlAction: action);

    [Fact]
    public void CurrentChatControlCanAcknowledgePauseAndResumeIndependentlyOfIdleMode()
    {
        var acknowledgements = new LifeControlAcknowledgements();
        acknowledgements.Begin("chat");
        Assert.True(acknowledgements.TryAccept(Ack(), "chat", false));
        Assert.False(acknowledgements.TryAccept(Ack(), "chat", false));
        Assert.True(acknowledgements.TryAccept(Ack("resume", false) with { DecisionEpoch = 5 }, "chat", false));
        Assert.False(acknowledgements.TryAccept(Ack("cancel", false) with { DecisionEpoch = 3 }, "chat", false));
    }

    [Fact]
    public void LateUnconfirmedUnknownOrSupersededRepliesCannotControlNativeActions()
    {
        var acknowledgements = new LifeControlAcknowledgements();
        acknowledgements.Begin("chat");
        Assert.False(acknowledgements.TryAccept(Ack(request: "old"), "chat", false));
        Assert.False(acknowledgements.TryAccept(Ack(), null, false));
        Assert.False(acknowledgements.TryAccept(Ack(), "chat", true));
        Assert.False(acknowledgements.TryAccept(Ack() with { Status = "failed" }, "chat", false));
        Assert.False(acknowledgements.TryAccept(Ack("unknown"), "chat", false));
        Assert.False(acknowledgements.TryAccept(Ack("resume", true), "chat", false));
        acknowledgements.Supersede();
        Assert.False(acknowledgements.TryAccept(Ack(), "chat", false));
        acknowledgements.Begin("new-chat");
        Assert.False(acknowledgements.TryAccept(Ack(), "new-chat", false));
        Assert.True(acknowledgements.TryAccept(Ack(request: "new-chat"), "new-chat", false));
    }
}
