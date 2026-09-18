using StardewAI.Companion.Mod.Observation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class SeedIdNormalizerTests
{
    [Theory]
    [InlineData("(O)472", "472")]
    [InlineData("472", "472")]
    [InlineData("(O)770", "770")]
    [InlineData("770", "770")]
    [InlineData("(O)474", "474")]
    public void ToCropDataId_NormalizesKnownSeeds_ToLocalItemId(string input, string expected)
    {
        string actual = SeedIdNormalizer.ToCropDataId(input);
        Assert.Equal(expected, actual);
    }

    [Fact]
    public void ToCropDataId_NullInput_ReturnsNull()
    {
        string? result = SeedIdNormalizer.ToCropDataId(null);
        Assert.Null(result);
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("NonExistentItem_12345")]
    public void ToCropDataId_EmptyOrUnknownInput_FallsBackToOriginal(string input)
    {
        string actual = SeedIdNormalizer.ToCropDataId(input);
        Assert.Equal(input, actual);
    }
}
