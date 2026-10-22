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
    public void EscapeDoesNotInterceptNonF8QuestionsOrEvents(bool f8Active, bool eventActive)
    {
        var question = new object();
        Assert.False(CompanionDialogueController.CanCloseF8Root(f8Active, eventActive, question, question, question));
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void EscapeClosesOwnedSpeechOrInputFromF8OrDirectInteraction(bool f8Active)
    {
        var speechOrInput = new object();
        var previousQuestion = new object();
        Assert.True(CompanionDialogueController.CanCloseDialogue(f8Active, false,
            speechOrInput, speechOrInput, previousQuestion, speechOrInput: true));
    }

    [Fact]
    public void EscapeLeavesUnrelatedMenusAndEventSpeechToTheGame()
    {
        var owned = new object();
        var unrelated = new object();
        Assert.False(CompanionDialogueController.CanCloseDialogue(true, false,
            unrelated, owned, owned, speechOrInput: true));
        Assert.False(CompanionDialogueController.CanCloseDialogue(true, true,
            owned, owned, owned, speechOrInput: true));
        Assert.False(CompanionDialogueController.CanCloseDialogue(true, false,
            null, owned, owned, speechOrInput: true));
        Assert.False(CompanionDialogueController.CanCloseDialogue(true, false,
            owned, owned, unrelated, speechOrInput: false));
    }
}
