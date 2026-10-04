using StardewAI.Companion.Mod.Observation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class AnimalPettingConditionsTests
{
    [Theory]
    [InlineData(1850, false, false)]
    [InlineData(1900, false, true)]
    [InlineData(2510, false, true)]
    [InlineData(1900, true, false)]
    [InlineData(2510, true, false)]
    public void NativeSleepGateMatchesTimeAndActualMovement(int time, bool moving, bool blocked)
    {
        Assert.Equal(blocked, AnimalPettingConditions.SleepingBlocksPetting(time, moving));
    }
}
