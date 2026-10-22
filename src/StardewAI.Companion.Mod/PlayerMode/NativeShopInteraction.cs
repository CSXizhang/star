using StardewValley;
using StardewValley.Menus;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.PlayerMode;

internal static class NativeShopInteraction
{
    public static bool TryOpen(IWorldObserver observer, IFarmerActor actor, string shopId, out ShopMenu? menu, out string error)
    {
        menu=null; error="";
        if (!ActorGuards.IsPlayerBound(actor) || actor.GameFarmer is not { } player) {
            error="Native shop interaction requires the bound player.";return false;
        }
        var metadata=observer.GetShopActionMetadata(shopId,actor.LocationName);
        if (metadata.LocationName != actor.LocationName || Game1.activeClickableMenu != null) {
            error="Reach the shop counter with no other menu open first.";return false;
        }
        var counters=metadata.CounterTiles.Where(t=>actor.Tile.IsAdjacentTo(t) || actor.Tile==t).ToArray();
        if (counters.Length==0) { error="The bound player is not beside the native shop counter.";return false; }
        actor.Halt();
        foreach (var counter in counters) {
            // Native Buy/SeedShop action checks time, proprietor and locked days.
            Game1.currentLocation.checkAction(new xTile.Dimensions.Location(counter.X,counter.Y),Game1.viewport,player);
            if (Game1.activeClickableMenu is ShopMenu opened) { menu=opened;return true; }
            if (Game1.activeClickableMenu != null) break; // leave a genuine native explanation visible
        }
        error="Native counter interaction did not open the shop; inspect native UI for its actual explanation.";
        return false;
    }
}
