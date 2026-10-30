using StardewValley;
using HarmonyLib;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>Nearby ordinary drops use the game's inventory insertion and capacity rules.</summary>
internal static class CompanionDebrisPickup
{
    private static readonly AccessTools.FieldRef<Debris, int> ReturnDelay =
        AccessTools.FieldRefAccess<Debris, int>("timeBeforeReturnToDroppingPlayer");

    internal static bool CollectNearby(Farmer? farmer)
    {
        var location = farmer?.currentLocation;
        if (farmer is null || location is null || ReferenceEquals(farmer, Game1.player)) return false;
        bool changed = false;
        var standing = farmer.StandingPixel;
        int radius = farmer.GetAppliedMagneticRadius();
        foreach (var debris in location.debris.ToList())
        {
            if (!NativeAnimalHarvestInventoryScope.IsOrdinaryDebris(debris)
                || !debris.chunksMoveTowardPlayer
                || debris.player.Value is not null
                || (ReturnDelay(debris) > 0
                    && debris.DroppedByPlayerID.Value == farmer.UniqueMultiplayerID)) continue;
            for (int i = debris.Chunks.Count - 1; i >= 0; i--)
            {
                var chunk = debris.Chunks[i];
                var position = chunk.position.Value;
                if (Math.Abs(position.X + 32 - standing.X) > radius
                    || Math.Abs(position.Y + 32 - standing.Y) > radius) continue;
                // Check capacity first so a rejected stack remains intact on the ground.
                var item = debris.item ?? ItemRegistry.Create(debris.itemId.Value, 1, debris.itemQuality);
                if (!farmer.couldInventoryAcceptThisItem(item)) continue;
                bool accepted;
                using (new NativeAnimalHarvestInventoryScope(farmer, location, debris, chunk))
                    accepted = debris.collect(farmer, chunk);
                if (!accepted) continue;
                debris.Chunks.RemoveAt(i);
                changed = true;
            }
            if (debris.Chunks.Count == 0) location.debris.Remove(debris);
        }
        return changed;
    }
}
