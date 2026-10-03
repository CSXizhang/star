using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class BedtimeProjectionTests
{
    [Fact]
    public void RepeatedProfileProjectionOnlyRequestsOneBedtimeSave()
    {
        var state = new LifeMenuUiState();
        int actorBedtime = 2400, saves = 0;
        for (int projection = 0; projection < 126; projection++)
        {
            if (state.ApplyBedtime(2300, actorBedtime)) saves++;
            actorBedtime = state.Bedtime;
        }
        Assert.Equal(1, saves);
        Assert.Equal(2300, actorBedtime);
        Assert.False(state.ApplyBedtime(2300, actorBedtime));
        Assert.True(state.ApplyBedtime(2400, actorBedtime));
    }

    [Fact]
    public void EquivalentMidnightAndInvalidTimesDoNotSaveDefaultAgain()
    {
        var state = new LifeMenuUiState();
        Assert.False(state.ApplyBedtime(0, 2400));
        Assert.False(state.ApplyBedtime(2600, 2400));
        Assert.True(state.ApplyBedtime(2400, 2200));
        Assert.False(state.ApplyBedtime(2400));
    }
}
