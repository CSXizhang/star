using StardewValley;

namespace StardewAI.Companion.Mod.Domain;

internal static class NativeCompanionAnimation
{
    // These are the native walking animation indexes from Farmer.updateMovementAnimation.
    public static int WalkingIndex(FacingDirection facing) => facing switch
    {
        FacingDirection.Up => 16,
        FacingDirection.Right => 8,
        FacingDirection.Down => 0,
        FacingDirection.Left => 24,
        _ => 0
    };

    public static int MovementIndex(FacingDirection facing, bool running) =>
        WalkingIndex(facing) + (running ? 32 : 0);

    public static void Stand(FarmerSprite sprite, FacingDirection facing)
    {
        sprite.PauseForSingleAnimation = false;
        sprite.StopAnimation();
        // Native walking groups start with a raised foot; offset 1 is the
        // neutral pose (including the native left-facing flip).
        sprite.setCurrentFrame(WalkingIndex(facing), 1);
        sprite.UpdateSourceRect();
    }

    public static void SleepPose(Farmer farmer)
    {
        farmer.FarmerSprite.PauseForSingleAnimation = false;
        farmer.FarmerSprite.StopAnimation();
        farmer.faceDirection(2);
        Stand(farmer.FarmerSprite, FacingDirection.Down);
        // Same sleeping-eye values as Farmer.updateCommon's bed block.
        farmer.currentEyes = 1;
        farmer.blinkTimer = -10;
    }
}
