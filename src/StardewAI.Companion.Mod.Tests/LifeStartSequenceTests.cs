using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>
/// Contract §1.8 "开始一起生活" start sequence: profile → goal → free, in that
/// order, with any rejection aborting back to Idle and out-of-order acks ignored.
/// </summary>
public class LifeStartSequenceTests
{
    private static LifeStartSequence Begin(
        string saveId = "save-1",
        string companionName = "阿星",
        string playStyle = "earn",
        string profileRequestId = "prof-req")
    {
        var seq = new LifeStartSequence();
        seq.Begin(saveId, companionName, playStyle, profileRequestId);
        return seq;
    }

    // ------------------------------------------------------------ happy path: profile → goal → free → complete

    [Fact]
    public void Begin_FromIdle_EntersAwaitProfileConfirm()
    {
        var seq = Begin();
        Assert.Equal(LifeStartStep.AwaitProfileConfirm, seq.Step);
        Assert.Equal("save-1", seq.SaveId);
        Assert.Equal("阿星", seq.CompanionName);
        Assert.Equal("prof-req", seq.ProfileRequestId);
        Assert.Null(seq.Goal);
    }

    [Fact]
    public void ProfileConfirmed_SendsGoalPreference_AndMapsGoal()
    {
        var seq = Begin(playStyle: "workhorse");

        var advance = seq.ApplyProfileState("prof-req", "confirmed", null);

        Assert.Equal(LifeStartAdvance.SendGoalPreference, advance);
        Assert.Equal(LifeStartStep.AwaitGoalAck, seq.Step);
        Assert.Equal("任劳任怨：浇水除草收获、喂动物、收机器成品等日常杂务", seq.Goal);
        Assert.NotNull(seq.GoalRequestId);
        Assert.False(string.IsNullOrWhiteSpace(seq.GoalRequestId));
    }

    [Fact]
    public void GoalAckConfirmed_SendsModeFree()
    {
        var seq = Begin();
        seq.ApplyProfileState("prof-req", "confirmed", null);

        var advance = seq.ApplyControlAck(seq.GoalRequestId!, "confirmed");

        Assert.Equal(LifeStartAdvance.SendModeFree, advance);
        Assert.Equal(LifeStartStep.AwaitModeAck, seq.Step);
        Assert.NotNull(seq.ModeRequestId);
        Assert.NotEqual(seq.GoalRequestId, seq.ModeRequestId);
    }

    [Fact]
    public void FreeAckConfirmed_Completes_AndResetsToIdle()
    {
        var seq = Begin();
        seq.ApplyProfileState("prof-req", "confirmed", null);
        seq.ApplyControlAck(seq.GoalRequestId!, "confirmed");

        var advance = seq.ApplyControlAck(seq.ModeRequestId!, "confirmed");

        Assert.Equal(LifeStartAdvance.Complete, advance);
        Assert.Equal(LifeStartStep.Idle, seq.Step);
        Assert.Null(seq.SaveId);
        Assert.Null(seq.GoalRequestId);
        Assert.Null(seq.ModeRequestId);
    }

    // ------------------------------------------------------------ rejection at each step → Abort + back to Idle

    [Fact]
    public void ProfileRejected_Aborts_WithReason_AndBackToIdle()
    {
        var seq = Begin();

        var advance = seq.ApplyProfileState("prof-req", "rejected", "服务端拒绝了设置");

        Assert.Equal(LifeStartAdvance.Abort, advance);
        Assert.Equal(LifeStartStep.Idle, seq.Step);
        Assert.Equal("设置未确认：服务端拒绝了设置", seq.AbortReason);
        Assert.Null(seq.SaveId);
        Assert.Null(seq.CompanionName);
    }

    [Fact]
    public void ProfileRejected_WithoutReason_UsesDefaultReason()
    {
        var seq = Begin();

        seq.ApplyProfileState("prof-req", "rejected", null);

        Assert.Equal("设置未确认。", seq.AbortReason);
        Assert.Equal(LifeStartStep.Idle, seq.Step);
    }

    [Fact]
    public void GoalAckRejected_Aborts_WithReason_AndBackToIdle()
    {
        var seq = Begin();
        seq.ApplyProfileState("prof-req", "confirmed", null);

        var advance = seq.ApplyControlAck(seq.GoalRequestId!, "rejected");

        Assert.Equal(LifeStartAdvance.Abort, advance);
        Assert.Equal(LifeStartStep.Idle, seq.Step);
        Assert.Equal("目标设置未确认。", seq.AbortReason);
    }

    [Fact]
    public void FreeAckRejected_Aborts_WithReason_AndBackToIdle()
    {
        var seq = Begin();
        seq.ApplyProfileState("prof-req", "confirmed", null);
        seq.ApplyControlAck(seq.GoalRequestId!, "confirmed");

        var advance = seq.ApplyControlAck(seq.ModeRequestId!, "rejected");

        Assert.Equal(LifeStartAdvance.Abort, advance);
        Assert.Equal(LifeStartStep.Idle, seq.Step);
        Assert.Equal("自由模式未确认。", seq.AbortReason);
    }

    [Fact]
    public void AfterAbort_CanRetryFromIdle()
    {
        var seq = Begin();
        seq.ApplyProfileState("prof-req", "rejected", "boom");
        Assert.Equal(LifeStartStep.Idle, seq.Step);

        seq.Begin("save-1", "阿星", "earn", "prof-req-2");

        Assert.Equal(LifeStartStep.AwaitProfileConfirm, seq.Step);
        Assert.Null(seq.AbortReason);
        Assert.Equal(LifeStartAdvance.SendGoalPreference, seq.ApplyProfileState("prof-req-2", "confirmed", null));
    }

    // ------------------------------------------------------------ out-of-order / duplicate acks are ignored

    [Fact]
    public void ControlAckWhileAwaitingProfile_IsIgnored()
    {
        var seq = Begin();

        Assert.Equal(LifeStartAdvance.Ignore, seq.ApplyControlAck("anything", "confirmed"));
        Assert.Equal(LifeStartStep.AwaitProfileConfirm, seq.Step);
    }

    [Fact]
    public void ProfileAckWithWrongRequestId_IsIgnored()
    {
        var seq = Begin();

        Assert.Equal(LifeStartAdvance.Ignore, seq.ApplyProfileState("prof-other", "confirmed", null));
        Assert.Equal(LifeStartStep.AwaitProfileConfirm, seq.Step);
        Assert.Null(seq.Goal);
    }

    [Fact]
    public void DuplicateProfileAck_IsIgnored()
    {
        var seq = Begin();
        Assert.Equal(LifeStartAdvance.SendGoalPreference, seq.ApplyProfileState("prof-req", "confirmed", null));

        var goalRequestId = seq.GoalRequestId;
        Assert.Equal(LifeStartAdvance.Ignore, seq.ApplyProfileState("prof-req", "confirmed", null));
        Assert.Equal(LifeStartStep.AwaitGoalAck, seq.Step);
        Assert.Equal(goalRequestId, seq.GoalRequestId);
    }

    [Fact]
    public void ModeAckBeforeGoalAck_IsIgnored()
    {
        var seq = Begin();
        seq.ApplyProfileState("prof-req", "confirmed", null);

        Assert.Equal(LifeStartAdvance.Ignore, seq.ApplyControlAck("future-mode-req", "confirmed"));
        Assert.Equal(LifeStartStep.AwaitGoalAck, seq.Step);
        Assert.Null(seq.ModeRequestId);
    }

    [Fact]
    public void DuplicateGoalAck_AfterModeSent_IsIgnored()
    {
        var seq = Begin();
        seq.ApplyProfileState("prof-req", "confirmed", null);
        Assert.Equal(LifeStartAdvance.SendModeFree, seq.ApplyControlAck(seq.GoalRequestId!, "confirmed"));

        var modeRequestId = seq.ModeRequestId;
        Assert.Equal(LifeStartAdvance.Ignore, seq.ApplyControlAck(seq.GoalRequestId!, "confirmed"));
        Assert.Equal(LifeStartStep.AwaitModeAck, seq.Step);
        Assert.Equal(modeRequestId, seq.ModeRequestId);
    }

    [Fact]
    public void AckForUnknownRequestId_IsIgnored()
    {
        var seq = Begin();
        seq.ApplyProfileState("prof-req", "confirmed", null);
        seq.ApplyControlAck(seq.GoalRequestId!, "confirmed");

        Assert.Equal(LifeStartAdvance.Ignore, seq.ApplyControlAck("req-nope", "confirmed"));
        Assert.Equal(LifeStartStep.AwaitModeAck, seq.Step);
    }

    [Fact]
    public void Begin_WhenAlreadyInFlight_IsNoOp()
    {
        var seq = Begin();
        seq.ApplyProfileState("prof-req", "confirmed", null);

        seq.Begin("save-2", "别人", "decor", "prof-req-2");

        Assert.Equal(LifeStartStep.AwaitGoalAck, seq.Step);
        Assert.Equal("save-1", seq.SaveId);
    }

    [Fact]
    public void Abort_WhenIdle_IsNoOp()
    {
        var seq = new LifeStartSequence();
        seq.Abort();
        Assert.Equal(LifeStartStep.Idle, seq.Step);
        Assert.Null(seq.AbortReason);
    }

    // ------------------------------------------------------------ MapGoal: four tiers

    [Fact]
    public void MapGoal_EarnAndWorkhorse_ReturnsDistinctTexts()
    {
        Assert.Equal("优先赚钱：收获出货、按需补种，遵守每日购买上限", LifeStartSequence.MapGoal("earn"));
        Assert.Equal("任劳任怨：浇水除草收获、喂动物、收机器成品等日常杂务", LifeStartSequence.MapGoal("workhorse"));
    }

    [Theory]
    [InlineData("community")]
    [InlineData("decor")]
    public void MapGoal_PlanningCapabilities_AreLabeledAsSuch(string playStyle)
    {
        var goal = LifeStartSequence.MapGoal(playStyle);

        Assert.Contains("（规划中）", goal);
    }

    [Fact]
    public void MapGoal_UnknownPlayStyle_FallsBackToEarn()
    {
        Assert.Equal(LifeStartSequence.MapGoal("earn"), LifeStartSequence.MapGoal("???"));
    }

    [Fact]
    public void SequenceUsesMappedGoal_ForCommunityPlayStyle()
    {
        var seq = Begin(playStyle: "community");

        seq.ApplyProfileState("prof-req", "confirmed", null);

        Assert.Contains("（规划中）", seq.Goal);
    }

    // ------------------------------------------------------------ LifeMenuUiState.HasProfileState (E3)

    [Fact]
    public void HasProfileState_DefaultsFalse_UntilMarkedReceived()
    {
        var state = new LifeMenuUiState();
        Assert.False(state.HasProfileState);

        state.MarkProfileStateReceived();

        Assert.True(state.HasProfileState);
    }

    [Fact]
    public void HasProfileState_ApplyingProfilePayload_DoesNotSetIt()
    {
        var state = new LifeMenuUiState();
        state.ApplyProfileState(
            onboarded: true, skipped: false, companionName: "阿星",
            playStyle: "earn", personality: "gentle", careFrequency: "moderate",
            profileRevision: 1, workMode: "free", workPaused: false,
            dailySpendLimit: 500, memoryRevision: 0);

        Assert.False(state.HasProfileState, "a payload apply must not count as the reply-arrival marker");
    }

    [Fact]
    public void HasProfileState_Reset_ClearsIt()
    {
        var state = new LifeMenuUiState();
        state.MarkProfileStateReceived();
        Assert.True(state.HasProfileState);

        state.Reset();

        Assert.False(state.HasProfileState);
    }
}
