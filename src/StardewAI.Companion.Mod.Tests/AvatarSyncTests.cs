using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class AvatarSyncTests
{
    [Fact]
    public void Avatar_InitializesFromActor()
    {
        var initialPose = new AuthoritativePose("Farm", 64, 15, FacingDirection.Down);
        var actor = new MechanicsActor("companion-1", initialPose: initialPose);
        var avatar = new CompanionAvatar(actor);

        Assert.Equal("companion-1", avatar.CompanionId);
        Assert.False(avatar.IsDesynced);
        Assert.True(avatar.IsVisible);
        Assert.Equal("idle", avatar.CurrentAnimation);
    }

    [Fact]
    public void Avatar_SyncToActor_SucceedsWhenSynchronized()
    {
        var initialPose = new AuthoritativePose("Farm", 64, 15, FacingDirection.Down);
        var actor = new MechanicsActor("companion-1", initialPose: initialPose);
        var avatar = new CompanionAvatar(actor);

        bool synced = avatar.SyncToActor(out string? deviationReason);
        Assert.True(synced);
        Assert.Null(deviationReason);
        Assert.False(avatar.IsDesynced);
    }

    [Fact]
    public void Avatar_SetAnimation_UpdatesCurrentAnimation()
    {
        var actor = new MechanicsActor("companion-1");
        var avatar = new CompanionAvatar(actor);

        avatar.SetAnimation("watering");
        Assert.Equal("watering", avatar.CurrentAnimation);

        avatar.SetAnimation("idle");
        Assert.Equal("idle", avatar.CurrentAnimation);
    }

    [Fact]
    public void Avatar_Draw_WhenGameFarmerIsNull_DoesNotThrow()
    {
        var actor = new MechanicsActor("companion-1");
        var avatar = new CompanionAvatar(actor);

        // Draw with null SpriteBatch should not throw when GameFarmer is null
        avatar.Draw(null!);
    }

    [Fact]
    public void Avatar_Draw_WhenInvisible_SkipsDrawingSafely()
    {
        var actor = new MechanicsActor("companion-1");
        var avatar = new CompanionAvatar(actor) { IsVisible = false };

        avatar.Draw(null!);
        Assert.False(avatar.IsVisible);
    }
}
