using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class ConfirmedPauseUiTests
{
    [Theory]
    [InlineData("processing", true, false)]
    [InlineData("completed", true, false)]
    [InlineData("failed", true, false)]
    [InlineData("completed", false, true)]
    public void DiscussionAndResumeRequestPreserveActualWorkResult(string status, bool readOnly, bool resume)
    {
        var panel = new CompanionTaskPanelState();
        panel.Complete("两机实际收取并入箱2件");
        panel.ApplyConfirmedPause(true);
        string current = panel.Current, next = panel.NextStep;
        panel.ApplyChatReply(new StardewAI.Companion.Mod.Transport.ChatReplyPayload(
            "discussion", status, "展开的安排说明", SaveId: "save", CommandComplete: true,
            ReadOnly: readOnly, ResumeWorkRequested: resume));
        Assert.Equal("两机实际收取并入箱2件", panel.LastResult);
        Assert.Equal(current, panel.Current);
        Assert.Equal(next, panel.NextStep);
        Assert.Equal("已暂停", panel.Stage);
        Assert.False(panel.Running);
        panel.ApplyChatReply(new StardewAI.Companion.Mod.Transport.ChatReplyPayload(
            "native-work", "job-completed", "两机实际收取并入箱4件", SaveId: "save"));
        Assert.Equal("两机实际收取并入箱4件", panel.LastResult);
        Assert.Equal("已暂停", panel.Stage);
    }

    [Fact]
    public void PausedConversationPreservesWorkIdentityAndDoesNotResumeNativeAction()
    {
        var chat = new ChatCommandUiState();
        Assert.True(chat.BeginCommand("saved-work", "save"));
        chat.NoteLocalPauseRequested();
        chat.BeginControl("pause", "pause");
        chat.ApplyControlAck("pause", true, true);
        Assert.True(chat.CanSubmit);
        Assert.True(chat.BeginCommand("chat-turn", "save"));
        Assert.False(chat.CanSubmit);
        Assert.Equal("saved-work", chat.CommandId);
        Assert.False(chat.ShouldRequestWorkResume("chat-turn", "chat-turn", "save", "completed", null));
        Assert.True(chat.ApplyReply("chat-turn", "chat-turn", "save", "completed", true));
        Assert.Equal("saved-work", chat.CommandId);
        Assert.True(chat.IsPaused);
        Assert.True(chat.ShouldPauseNative);
        Assert.True(chat.CanSubmit);
    }

    [Fact]
    public void ExplicitCurrentCompletedResumeFlagOnlyRequestsControlAndNeedsItsAcknowledgement()
    {
        var chat = new ChatCommandUiState();
        chat.BeginCommand("saved-work", "save");
        chat.BeginControl("pause", "pause");
        chat.ApplyControlAck("pause", true, true);
        chat.BeginCommand("resume-turn", "save");
        Assert.False(chat.ShouldRequestWorkResume("resume-turn", "resume-turn", "wrong-save", "completed", true));
        Assert.False(chat.ShouldRequestWorkResume("resume-turn", "resume-turn", null, "completed", true));
        Assert.False(chat.ShouldRequestWorkResume("older-turn", "saved-work", "save", "completed", true));
        Assert.False(chat.ShouldRequestWorkResume("resume-turn", "resume-turn", "save", "processing", true));
        Assert.True(chat.ShouldRequestWorkResume("resume-turn", "resume-turn", "save", "completed", true));
        Assert.True(chat.ApplyReply("resume-turn", "resume-turn", "save", "completed", true));
        Assert.True(chat.BeginControl("resume-control", "resume"));
        Assert.Equal("saved-work", chat.CommandId);
        Assert.False(chat.CanSubmit);
        Assert.True(chat.IsPaused);
        Assert.True(chat.ShouldPauseNative);
        Assert.False(chat.ShouldRequestWorkResume("resume-turn", "resume-turn", "save", "completed", true));
        Assert.False(chat.ApplyControlAck("stale-control", true, false));
        Assert.True(chat.IsPaused);
        Assert.True(chat.ApplyControlAck("resume-control", true, false));
        Assert.False(chat.IsPaused);
        Assert.False(chat.ShouldPauseNative);
    }
    [Fact]
    public void LateTerminalReplyReleasesCommandButKeepsConfirmedPauseUntilResumeAck()
    {
        var chat = new ChatCommandUiState();
        chat.BeginCommand("root", "save");
        chat.BeginControl("pause", "pause");
        chat.ApplyControlAck("pause", true, true);
        Assert.True(chat.ApplyReply("root", "root", "save", "completed", true));
        Assert.False(chat.HasActiveCommand);
        Assert.True(chat.IsPaused);
        Assert.Equal("已暂停", chat.StatusText);
        chat.BeginControl("resume", "resume");
        chat.ApplyControlAck("resume", true, false);
        Assert.False(chat.IsPaused);
        Assert.Equal("已恢复", chat.StatusText);
        Assert.True(chat.BeginCommand("next", "save"));
        Assert.True(chat.ApplyReply("next", "next", "save", "processing", false));
        Assert.Equal("模型思考 / 观察与选择", chat.StatusText);
    }

    [Fact]
    public void InitialProfilePauseUsesChatAuthorityAndCannotOverrideUnconfirmedControl()
    {
        var chat = new ChatCommandUiState();
        Assert.True(chat.ApplyWorkPausedProjection(true));
        Assert.True(chat.IsPaused);
        Assert.Equal("已暂停", chat.StatusText);
        Assert.True(chat.CanSubmit); // ordinary conversation preserves pause
        Assert.True(chat.BeginControl("resume", "resume"));
        Assert.False(chat.ApplyWorkPausedProjection(false));
        Assert.True(chat.IsPaused);
        Assert.Equal("resume", chat.PendingControlId);
        chat.ApplyControlAck("resume", true, false);
        Assert.False(chat.IsPaused);
        chat.NoteLocalPauseRequested();
        Assert.False(chat.ApplyWorkPausedProjection(false));
        Assert.True(chat.LocalPauseRequested);
    }

    [Fact]
    public void ConfirmedPauseSuppressesLateProgressAndResumeAcceptsRealProgress()
    {
        var panel = new CompanionTaskPanelState();
        panel.Begin("saved work", false);
        panel.ApplyConfirmedPause(true);
        panel.NativeProgress("浇水", "进行中", 1, 12);
        Assert.False(panel.Running);
        Assert.Equal("已暂停", panel.Stage);
        panel.ApplyConfirmedPause(false);
        Assert.False(panel.Running);
        panel.NativeProgress("浇水", "进行中", 2, 12);
        Assert.True(panel.Running);
        Assert.Equal("浇水 2/12", panel.Progress);
    }

    [Fact]
    public void ConfirmedPauseDoesNotTimeoutButResumedCommandStillHasWatchdog()
    {
        var chat = new ChatCommandUiState();
        var start = DateTime.UtcNow;
        Assert.True(chat.BeginCommand("root", "save"));
        Assert.True(chat.ShouldReportInactivity(start.AddSeconds(121), start));
        chat.BeginControl("pause", "pause");
        chat.ApplyControlAck("pause", true, true);
        Assert.False(chat.ShouldReportInactivity(start.AddHours(1), start));
        Assert.True(chat.HasActiveCommand);
        chat.BeginControl("resume", "resume");
        chat.ApplyControlAck("resume", true, false);
        var resumedAt = start.AddHours(1);
        Assert.False(chat.ShouldReportInactivity(resumedAt.AddSeconds(119), resumedAt));
        Assert.True(chat.ShouldReportInactivity(resumedAt.AddSeconds(121), resumedAt));
    }
}
