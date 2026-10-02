using System.Runtime.Serialization;
using System.Text.Json;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Menus;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Transport;
using StardewValley.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class CompanionBedtimeTests
{
    [Theory]
    [InlineData(0, 2400)] [InlineData(30, 2430)] [InlineData(100, 2500)]
    [InlineData(2300, 2300)] [InlineData(2360, 2400)] [InlineData(2600, 2400)]
    public void BedtimeNormalizesMidnightAndRejectsInvalidClock(int input, int expected) =>
        Assert.Equal(expected, CompanionBedtime.Normalize(input));

    [Fact]
    public void WindDownIncludesTravelAndFinishingTimeEvenForLongRoutes()
    {
        Assert.False(CompanionBedtime.ShouldWindDown(2320, 2400, 0));
        Assert.True(CompanionBedtime.ShouldWindDown(2330, 2400, 0));
        Assert.True(CompanionBedtime.ShouldWindDown(2120, 2400, 140));
        Assert.False(CompanionBedtime.ShouldWindDown(2110, 2400, 140));
        Assert.True(CompanionBedtime.ShouldWindDown(2230, 2300, 0));
    }

    [Fact]
    public void OnlyCompanionMenusBypassMenuPause()
    {
        var conversation = (IClickableMenu)FormatterServices.GetUninitializedObject(typeof(CompanionLifeMenu));
        var inventory = (IClickableMenu)FormatterServices.GetUninitializedObject(typeof(InventoryMenu));
        Assert.True(CompanionMenuClock.IsConversation(conversation));
        Assert.False(CompanionMenuClock.IsConversation(inventory));
        Assert.False(CompanionMenuClock.IsConversation(null));
        try
        {
            CompanionMenuClock.OwnedQuestion = inventory;
            Assert.True(CompanionMenuClock.IsConversation(inventory));
            CompanionMenuClock.OwnedQuestion = null;
            Assert.False(CompanionMenuClock.IsConversation(inventory));
        }
        finally { CompanionMenuClock.OwnedQuestion = null; }
    }

    [Fact]
    public void SleepRestoresItsOwnBedtimeAndWakesWithoutAWorkTask()
    {
        var actor = new MechanicsActor();
        var observer = new SimulatedWorldObserver();
        var navigation = new NavigationStateMachine(actor, observer, new SameMapNavigator(observer), new WorldMapGraph());
        var rest = new CompanionRestController(actor, navigation);
        rest.RestoreSleep(2340);
        Assert.Equal("sleeping", rest.State);
        Assert.Equal(2340, rest.SleepStartedAt);
        rest.Wake();
        Assert.Equal("awake", rest.State);
        Assert.Null(rest.SleepStartedAt);
        Assert.Null(actor.ActiveTaskId);
    }

    [Fact]
    public void BedtimePersistsAndOldActorFilesHaveMidnightDefault()
    {
        var old = JsonSerializer.Deserialize<CompanionActorState>("{}")!;
        Assert.Equal(2400, old.PreferredBedtime);
        var saved = new CompanionActorState { PreferredBedtime = 2300, SleepDay = 4, SleepStartedAt = 2250 };
        var restored = JsonSerializer.Deserialize<CompanionActorState>(JsonSerializer.Serialize(saved))!;
        Assert.Equal(2300, restored.PreferredBedtime);
        Assert.Equal(4, restored.SleepDay);
        Assert.Equal(2250, restored.SleepStartedAt);
        Assert.Contains("\"bedtime\":2300", JsonSerializer.Serialize(new LifeProfilePatchDto(Bedtime: 2300)));
    }
}
