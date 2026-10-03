using StardewAI.Companion.Mod.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class CompanionRootEscapeTests
{
    [Fact]
    public void EscapeClosesOnlyTheCurrentF8RootQuestion()
    {
        var root = new object();
        var child = new object();
        var inventory = new object();
        Assert.True(CompanionDialogueController.CanCloseF8Root(true, false, root, root, root));
        Assert.False(CompanionDialogueController.CanCloseF8Root(true, false, inventory, root, root));
        Assert.False(CompanionDialogueController.CanCloseF8Root(true, false, child, child, root));
        Assert.False(CompanionDialogueController.CanCloseF8Root(true, false, null, root, root));
        Assert.False(CompanionDialogueController.CanCloseF8Root(true, false, null, null, null));
    }

    [Theory]
    [InlineData(false, false)]
    [InlineData(false, true)]
    [InlineData(true, true)]
    public void EscapeDoesNotInterceptLegacyConversationOrEvents(bool f8Active, bool eventActive)
    {
        var question = new object();
        Assert.False(CompanionDialogueController.CanCloseF8Root(f8Active, eventActive, question, question, question));
    }
}
