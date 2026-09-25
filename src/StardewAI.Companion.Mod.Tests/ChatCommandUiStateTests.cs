using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class ChatCommandUiStateTests
{
    [Fact]
    public void OneInstructionSurvivesJobAndChainTurnsUntilCommandComplete()
    {
        var state = new ChatCommandUiState();
        Assert.True(state.BeginCommand("root-1", "save-a"));
        Assert.True(state.ApplyReply("root-1", "root-1", "save-a", "selected", false));
        Assert.True(state.ApplyReply("root-1", "root-1", "save-a", "job-completed", false));
        Assert.True(state.HasActiveCommand);
        Assert.Equal("作业已完成，等待后续", state.StatusText);
        Assert.False(state.CanSubmit);
        Assert.True(state.ApplyReply("chain-2", "root-1", "save-a", "processing", false));
        Assert.Equal("chain-2", state.TurnId);
        Assert.False(state.ApplyReply("root-1", "root-1", "save-a", "processing", false));
        Assert.False(state.ApplyReply("root-1", "root-1", "save-b", "completed", true));
        Assert.True(state.ApplyReply("chain-2", "root-1", "save-a", "completed", true));
        Assert.Null(state.CommandId);
        Assert.True(state.CanSubmit);
    }

    [Fact]
    public void LegacyTerminalStillReleasesSingleRequest()
    {
        var state = new ChatCommandUiState();
        Assert.True(state.BeginCommand("legacy", "save-a"));
        Assert.True(state.ApplyReply("legacy", null, "save-a", "job-completed", null));
        Assert.Equal("作业已完成", state.StatusText);
        Assert.True(state.CanSubmit);
    }

    [Fact]
    public void CancelWaitsForItsAckAndOldRepliesCannotTakeOverNextInstruction()
    {
        var state = new ChatCommandUiState();
        Assert.True(state.BeginCommand("old", "save-a"));
        Assert.True(state.BeginControl("control-1", "cancel"));
        Assert.False(state.CanSubmit);
        Assert.False(state.BeginControl("control-2", "cancel"));
        Assert.False(state.ApplyControlAck("unrelated", true, false));
        Assert.True(state.ApplyReply("chain-old", "old", "save-a", "processing", false));
        Assert.False(state.CanSubmit);
        Assert.True(state.ApplyControlAck("control-1", true, false));
        Assert.True(state.CanSubmit);
        Assert.True(state.BeginCommand("new", "save-a"));
        Assert.False(state.ApplyReply("chain-old", "old", "save-a", "completed", true));
        Assert.Equal("new", state.CommandId);
    }

    [Fact]
    public void RejectedOrTimedOutControlKeepsTheCommandAndConfirmedPauseState()
    {
        var state = new ChatCommandUiState();
        Assert.True(state.BeginControl("pause-1", "pause"));
        Assert.True(state.ApplyControlAck("pause-1", true, true));
        Assert.True(state.IsPaused);
        Assert.False(state.CanSubmit);
        Assert.True(state.BeginControl("resume-1", "resume"));
        Assert.True(state.FailControl("resume-1"));
        Assert.True(state.IsPaused);
        Assert.True(state.BeginControl("resume-2", "resume"));
        Assert.True(state.ApplyControlAck("resume-2", true, false));
        Assert.True(state.BeginCommand("root", "save-a"));
        Assert.True(state.BeginControl("cancel-1", "cancel"));
        Assert.True(state.ApplyControlAck("cancel-1", false, false));
        Assert.Equal("root", state.CommandId);
        Assert.False(state.CanSubmit);
    }

    [Fact]
    public void LocalPauseStillBlocksNewInputWhenServiceRejectsAndResumeCanBeRetried()
    {
        var state = new ChatCommandUiState();
        state.NoteLocalPauseRequested();
        Assert.True(state.BeginControl("pause", "pause"));
        Assert.True(state.ApplyControlAck("pause", false, false));
        Assert.False(state.HasPendingControl);
        Assert.True(state.LocalPauseRequested);
        Assert.False(state.CanSubmit);
        Assert.True(state.BeginControl("resume", "resume"));
        Assert.True(state.ApplyControlAck("resume", true, false));
        Assert.False(state.CanSubmit); // native safe-point resume is still pending
        state.NoteLocalResumed();
        Assert.True(state.CanSubmit);
    }

    [Fact]
    public void FailedSendReleasesOnlyTheMatchingCommand()
    {
        var state = new ChatCommandUiState();
        Assert.True(state.BeginCommand("current", "save-a"));
        Assert.False(state.FailSend("older"));
        Assert.True(state.HasActiveCommand);
        Assert.True(state.FailSend("current"));
        Assert.True(state.CanSubmit);
    }
}
