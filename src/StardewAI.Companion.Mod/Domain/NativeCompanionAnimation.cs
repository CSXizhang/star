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

    public static void SleepPose(Farmer farmer)
    {
        farmer.FarmerSprite.PauseForSingleAnimation = false;
        farmer.FarmerSprite.StopAnimation();
        farmer.faceDirection(2);
        farmer.FarmerSprite.setCurrentFrame(WalkingIndex(FacingDirection.Down));
        farmer.FarmerSprite.UpdateSourceRect();
        // Same sleeping-eye values as Farmer.updateCommon's bed block.
        farmer.currentEyes = 1;
        farmer.blinkTimer = -10;
    }
}
