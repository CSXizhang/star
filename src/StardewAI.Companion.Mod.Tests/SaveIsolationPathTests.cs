using System.IO;
using StardewAI.Companion.TestDriver;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class SaveIsolationPathTests
{
    [Fact]
    public void DailyRootAndNormalizedDescendantAreRejectedButSiblingIsAllowed()
    {
        var daily = Path.GetFullPath(Path.Combine("fixture", "Saves"));
        Assert.True(SaveIsolationPatcher.IsWithinDailySaves(daily + Path.DirectorySeparatorChar, daily));
        Assert.True(SaveIsolationPatcher.IsWithinDailySaves(Path.Combine(daily, "farm", "..", "other"), daily));
        Assert.True(SaveIsolationPatcher.IsWithinDailySaves(daily.ToUpperInvariant(), daily));
        Assert.False(SaveIsolationPatcher.IsWithinDailySaves(daily + "-isolated", daily));
    }
}
