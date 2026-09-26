using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class CompanionFirstMeetingTests
{
    [Fact]
    public void WaitsForActualProfileBeforeAskingForName()
    {
        var state = new LifeMenuUiState();
        Assert.False(CompanionFirstMeeting.NeedsMeeting(state));
        state.MarkProfileStateReceived();
        Assert.True(CompanionFirstMeeting.NeedsName(state));
        state.BeginProfileSet("save", 0);
        Assert.Equal("阿星", state.CompanionName); // Draft input never mutates the confirmed name.
        state.EndProfileSet("other");
        Assert.Equal("save", state.PendingProfileSetRequestId);
        state.EndProfileSet("save");
        Assert.Null(state.PendingProfileSetRequestId);
        Assert.Equal("阿星", state.CompanionName);
    }

    [Theory]
    [InlineData(false, false, true)]
    [InlineData(true, false, false)]
    [InlineData(false, true, false)]
    public void LegacySavedProfileNeverRepeatsNaming(bool onboarded, bool skipped, bool needsMeeting)
    {
        var state = new LifeMenuUiState();
        state.MarkProfileStateReceived();
        state.ApplyProfileState(onboarded, skipped, "小满", "community", "calm", "quiet", 4, "command", true, 100, 3);
        Assert.Equal(needsMeeting, CompanionFirstMeeting.NeedsMeeting(state));
        Assert.False(CompanionFirstMeeting.NeedsName(state));
        var patch = CompanionFirstMeeting.Patch(null, "help");
        Assert.Null(patch.CompanionName);
        Assert.Null(patch.Personality);
        Assert.Null(patch.PlayStyle);
        Assert.Null(patch.CareFrequency);
        Assert.True(state.WorkPaused);
        Assert.Equal(3, state.MemoryRevision);
    }

    [Fact]
    public void NameUsesExistingTrimAndUnicodeLengthRules()
    {
        Assert.Equal("小满", CompanionFirstMeeting.NormalizeName(" 小满 "));
        Assert.Null(CompanionFirstMeeting.NormalizeName(" \t "));
        Assert.Null(CompanionFirstMeeting.NormalizeName(new string('星', 13)));
        Assert.NotNull(CompanionFirstMeeting.NormalizeName(string.Concat(Enumerable.Repeat("🌟", 12))));
    }

    [Fact]
    public void OnlyExplicitChatPreferenceChangesFrequencyAndSkipDoesNotCompleteMeeting()
    {
        Assert.Equal("chatty", CompanionFirstMeeting.Patch("小满", "chat").CareFrequency);
        Assert.Null(CompanionFirstMeeting.Patch("小满", "slow").CareFrequency);
        var skip = CompanionFirstMeeting.Patch(null, "later");
        Assert.True(skip.Skipped);
        Assert.Null(skip.Onboarded);
        Assert.Null(skip.CompanionName);
        Assert.Contains("不采购", CompanionFirstMeeting.Preference("help"));
    }
}
