using StardewValley;
using Microsoft.Xna.Framework;

namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// Player-mode binding guard. An actor is "player-bound" only when its
/// <see cref="IFarmerActor.GameFarmer"/> IS the native <see cref="Game1.player"/>
/// instance, established by explicit reference identity.
/// Anti-substitution guarantee: this guard never silently substitutes the global
/// <see cref="Game1.player"/> for another actor. Binding is explicit
/// (ReferenceEquals), so a companion Farmer that merely mirrors the player's state
/// is never treated as the player, and player-mode native paths never touch an
/// actor that is not provably the real local player.
/// </summary>
public static class ActorGuards
{
    /// <summary>Final short waypoint step uses the same collision check as walking.</summary>
    public static bool TryFinishWalkingStep(IFarmerActor actor, Vector2 target)
    {
        var delta = target - actor.PixelPosition;
        actor.MovePixels(delta.X, delta.Y);
        return Vector2.DistanceSquared(actor.PixelPosition, target) < 0.01f;
    }

    // Festivals expose native free-roam while eventUp remains true. Cutscenes
    // and scripted festival phases still retain exclusive movement control.
    public static bool FestivalFreeRoam => Game1.eventUp
        && Game1.CurrentEvent?.isFestival == true
        && Game1.CurrentEvent.playerControlSequence;

    /// <summary>Pixel target whose native standing point is centered on the tile.</summary>
    public static Vector2 WalkingTargetPixel(IFarmerActor actor, TileCoordinate tile)
    {
        var origin = new Vector2(tile.X * 64, tile.Y * 64);
        if (!IsPlayerBound(actor)) return origin;
        var farmer = actor.GameFarmer!;
        var center = farmer.GetBoundingBox().Center;
        var offset = new Vector2(center.X, center.Y) - actor.PixelPosition;
        return origin + new Vector2(32, 32) - offset;
    }

    public static bool HasReachedWalkingTile(IFarmerActor actor, TileCoordinate tile)
    {
        if (!IsPlayerBound(actor)) return false;
        var delta = actor.PixelPosition - WalkingTargetPixel(actor, tile);
        // Native cardinal movement settles each axis separately. A (3,3)
        // residual is within one step on both axes but outside a radius of 4;
        // requiring that circle can oscillate forever around a reached tile.
        return Math.Abs(delta.X) <= 4f && Math.Abs(delta.Y) <= 4f;
    }

    /// <summary>
    /// True iff <paramref name="actor"/> is non-null, carries a GameFarmer, and that
    /// farmer is reference-identical to <see cref="Game1.player"/>.
    /// </summary>
    public static bool IsPlayerBound(IFarmerActor? actor)
    {
        return actor?.GameFarmer != null && ReferenceEquals(actor.GameFarmer, Game1.player);
    }
}
