using System.Reflection;
using StardewModdingAPI;
using StardewValley;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Production harvest adapter using crop.harvest or the crop's native scythe callback.
/// Enforces:
/// 1. Main-thread execution on the companion's loaded map.
/// 2. Maturity via HoeDirt.readyForHarvest() only; scythe crops require an owned scythe.
/// 3. Conservative inventory capacity precheck (one empty slot required).
/// 4. Game1.player is temporarily swapped to the companion Farmer for the duration of
///    crop.harvest (the game's harvest adds produce to Game1.player's inventory) and is
///    restored in a finally block; the reference is verified after restoration.
///    Player-bound actors skip the swap: the actor already IS Game1.player and the
///    harvest runs natively against the player.
/// 5. Human player resources are verified unchanged before and after the harvest
///    (companion mode only; skipped for player-bound actors).
/// 6. No direct world mutation: the crop is removed only by the game's own harvest logic,
///    and gained items are measured from the companion's own inventory delta.
/// </summary>
public sealed class NormalHarvestAdapter : IHarvestAdapter
{
    private static readonly Action<Farmer> SetGamePlayer = CreatePlayerSetter();

    private static Action<Farmer> CreatePlayerSetter()
    {
        var prop = typeof(Game1).GetProperty(nameof(Game1.player), BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
        var setter = prop?.GetSetMethod(nonPublic: true);
        if (setter != null)
        {
            return (Action<Farmer>)Delegate.CreateDelegate(typeof(Action<Farmer>), setter);
        }
        var field = typeof(Game1).GetField("_player", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
        if (field != null)
        {
            return f => field.SetValue(null, f);
        }
        throw new InvalidOperationException("Failed to locate Game1.player setter or backing field.");
    }

    private readonly IWorldObserver _observer;
    private readonly IMonitor _monitor;

    public NormalHarvestAdapter(
        IWorldObserver observer,
        IMonitor monitor)
    {
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _monitor = monitor ?? throw new ArgumentNullException(nameof(monitor));
    }

    public HarvestTileResult HarvestTile(
        IFarmerActor actor,
        string locationName,
        TileCoordinate targetTile)
    {
        ArgumentNullException.ThrowIfNull(actor);

        // 1. Thread safety invariant
        if (!_observer.IsMainThread)
        {
            return HarvestTileResult.Failed("Harvest operation rejected: must execute on the game main thread.");
        }

        // 2. The companion's map is independent of the human player's active map.
        if (!string.Equals(actor.LocationName, locationName, StringComparison.OrdinalIgnoreCase))
        {
            return HarvestTileResult.Failed(
                $"Harvest target map '{locationName}' does not match companion map '{actor.LocationName}'.");
        }
        if (!_observer.LocationExists(locationName))
        {
            return HarvestTileResult.Failed($"Harvest target map '{locationName}' is not loaded or does not exist.");
        }

        // 3. Precheck adjacency
        if (!actor.Tile.IsAdjacentTo(targetTile))
        {
            return HarvestTileResult.PreconditionError(
                $"Actor at {actor.Tile} is not adjacent to target tile {targetTile}.");
        }

        // 4. Precheck crop state (sole maturity criterion: readyForHarvest, surfaced via observer)
        var dirtState = _observer.GetDirtState(locationName, targetTile);
        if (!dirtState.IsTilled || !dirtState.HasCrop || !dirtState.IsHarvestable)
        {
            return HarvestTileResult.PreconditionError(
                $"Tile {targetTile} does not contain a mature crop.", skipReason: "not-ready");
        }
        var scythe = actor.GameFarmer?.Items.OfType<MeleeWeapon>().FirstOrDefault(tool => tool.isScythe());
        if (dirtState.RequiresScythe && scythe is null)
        {
            return HarvestTileResult.PreconditionError(
                $"Crop on tile {targetTile} requires an owned scythe.", skipReason: "missing-tool:Scythe");
        }

        // Companion isolation retains its conservative capacity check. The native player
        // may stack a harvest into an occupied slot; crop.harvest decides capacity.
        bool playerBound = ActorGuards.IsPlayerBound(actor);
        if (!playerBound && actor.FreeInventorySlots <= 0)
        {
            return HarvestTileResult.PreconditionError(
                "Cannot harvest: companion inventory has no empty slot.", skipReason: "inventory-full");
        }

        if (actor.GameFarmer is null)
        {
            return HarvestTileResult.Failed("Companion actor has no GameFarmer instance.");
        }

        // 6. Resolve the actual HoeDirt and crop
        var location = Game1.getLocationFromName(locationName);
        if (location is null)
        {
            return HarvestTileResult.Failed($"Game location '{locationName}' not found.");
        }

        var tileVector = new Microsoft.Xna.Framework.Vector2(targetTile.X, targetTile.Y);
        if (!location.terrainFeatures.TryGetValue(tileVector, out var terrainFeature) ||
            terrainFeature is not HoeDirt hoeDirt || hoeDirt.crop is null)
        {
            return HarvestTileResult.PreconditionError(
                $"Tile {targetTile} does not contain a crop.", skipReason: "not-ready");
        }

        var crop = hoeDirt.crop;
        bool requiresScythe = crop.GetHarvestMethod() == StardewValley.GameData.Crops.HarvestMethod.Scythe;
        if (requiresScythe && scythe is null)
        {
            return HarvestTileResult.PreconditionError(
                $"Crop on tile {targetTile} requires an owned scythe.", skipReason: "missing-tool:Scythe");
        }

        string? cropId = crop.indexOfHarvest?.Value;

        // 7. Verify human player baseline isolation BEFORE the harvest
        // (skipped entirely in player mode: the actor IS Game1.player, so the
        // resource changes below are the intended effect of the harvest).
        PlayerResourceSnapshot? playerBefore = null;
        Farmer? originalPlayerRef = null;
        if (!playerBound)
        {
            playerBefore = _observer.GetPlayerSnapshot();
            originalPlayerRef = Game1.player;
        }
        var companion = actor.GameFarmer;

        // Baseline of companion inventory for genuine gained-item measurement
        // (actor-scoped; kept in both modes for the gained-item report).
        var inventoryBefore = SnapshotInventoryTotals(companion);

        try
        {
            // 8. Execute the game's own harvest with the companion as the acting farmer.
            // crop.harvest adds produce to Game1.player's inventory, so in companion
            // mode Game1.player is swapped to the companion for exactly the duration
            // of the call; in player mode the actor already IS Game1.player and the
            // harvest runs natively without any swap.
            // Crop.harvest's bool means the crop should be removed. A successful
            // regrowing harvest instead resets its growth state and returns false.
            bool removeCrop;
            if (playerBound)
            {
                removeCrop = crop.harvest(targetTile.X, targetTile.Y, hoeDirt);
                if (removeCrop && !crop.RegrowsAfterHarvest())
                {
                    hoeDirt.destroyCrop(showAnimation: false);
                }
            }
            else
            {
                SetGamePlayer(companion);
                try
                {
                    if (requiresScythe)
                    {
                        HarvestWithScythe(companion, location, hoeDirt, scythe!, tileVector);
                        removeCrop = hoeDirt.crop is null;
                    }
                    else
                    {
                        removeCrop = crop.harvest(targetTile.X, targetTile.Y, hoeDirt);
                        if (removeCrop && !crop.RegrowsAfterHarvest())
                            hoeDirt.destroyCrop(showAnimation: false);
                    }
                }
                finally
                {
                    SetGamePlayer(originalPlayerRef!);
                }
            }

            bool cropAdvanced = hoeDirt.crop is null ||
                (ReferenceEquals(hoeDirt.crop, crop) && !crop.dead.Value && !hoeDirt.readyForHarvest());
            if (!removeCrop && !cropAdvanced)
            {
                return playerBound
                    ? HarvestTileResult.PreconditionError($"Native crop.harvest left tile {targetTile} unchanged.",
                        skipReason: actor.FreeInventorySlots <= 0 ? "inventory-full" : "native-harvest-declined")
                    : HarvestTileResult.Failed($"crop.harvest returned false on tile {targetTile}.");
            }

            // 9. Measure gained items from the companion's own inventory delta
            var inventoryAfter = SnapshotInventoryTotals(companion);
            string? itemId = null;
            string? itemName = null;
            int gainedStack = 0;
            int gainedQuality = 0;
            foreach (var (key, afterStack) in inventoryAfter)
            {
                inventoryBefore.TryGetValue(key, out int beforeStack);
                int delta = afterStack - beforeStack;
                if (delta > 0)
                {
                    itemId = key.ItemId;
                    gainedStack = delta;
                    gainedQuality = key.Quality;
                    break;
                }
            }

            if (itemId is null && !playerBound)
            {
                _monitor.Log(
                    $"Harvest on tile {targetTile}: crop changed but no inventory gain measured; reporting zero items. Inspect remaining native drops.",
                    LogLevel.Warn);
            }

            // A successful native crop action without an inventory delta does not prove
            // one unit entered the bag (e.g. native debris). Keep the measured zero.
            itemName = itemId is null ? null : FindItemName(companion, itemId) ?? cropId;

            return HarvestTileResult.Succeeded(cropId, itemId, itemName, gainedStack, gainedQuality, staminaCost: 0f, waterCost: 0);
        }
        finally
        {
            // 10. Strict Invariant Checks (mirror NormalWateringCanAdapter's isolation guard):
            // skipped entirely in player mode.
            if (!playerBound)
            {
                // - Game1.player reference was restored
                if (!object.ReferenceEquals(originalPlayerRef, Game1.player))
                {
                    throw new InvalidOperationException("CRITICAL SAFETY VIOLATION: Game1.player reference was not restored after companion harvest!");
                }

                // - Game1.player resources were NOT drained or modified
                var playerAfter = _observer.GetPlayerSnapshot();
                if (!playerBefore!.EqualsPlayerResources(playerAfter))
                {
                    throw new InvalidOperationException(
                        $"CRITICAL SAFETY VIOLATION: Human player resources changed during companion harvest! Before: {playerBefore}, After: {playerAfter}");
                }
            }
        }
    }

    private static void HarvestWithScythe(Farmer farmer, GameLocation location, HoeDirt dirt,
        MeleeWeapon scythe, Microsoft.Xna.Framework.Vector2 tile)
    {
        // Crop.harvest creates native drops on Game1.currentLocation. The player may
        // be indoors: scope the backing field for this synchronous call only, without
        // firing player location-change events. Game1.player is already actor-scoped.
        var flags = BindingFlags.Public | BindingFlags.NonPublic;
        var game = typeof(Game1).GetField("game1", flags | BindingFlags.Static)?.GetValue(null);
        var locationField = typeof(Game1).GetField("instanceGameLocation", flags | BindingFlags.Instance);
        if (game is null || locationField is null)
            throw new InvalidOperationException("Cannot scope native crop harvest location.");
        var previousLocation = locationField.GetValue(game);
        var previousDebris = location.debris.ToHashSet();
        try
        {
            locationField.SetValue(game, location);
            // DoFunction binds the real tool's last user. The native HoeDirt callback
            // harvests just this authorized tile, including native wheat/hay yields.
            // Do not invoke a wide weapon arc which could cut neighboring crops.
            scythe.DoFunction(location, (int)tile.X * 64 + 32, (int)tile.Y * 64 + 32, 0, farmer);
            if (!ReferenceEquals(scythe.getLastFarmerToUse(), farmer))
                throw new InvalidOperationException("Scythe user did not bind to companion.");
            dirt.performToolAction(scythe, 0, tile);
            // Collect only drops generated by this exact native action, through the
            // game's inventory rules. Rejected drops remain on the ground.
            foreach (var debris in location.debris.Where(d => !previousDebris.Contains(d)
                         && NativeAnimalHarvestInventoryScope.IsOrdinaryDebris(d)).ToList())
            {
                for (int i = debris.Chunks.Count - 1; i >= 0; i--)
                    if (debris.collect(farmer, debris.Chunks[i])) debris.Chunks.RemoveAt(i);
                if (debris.Chunks.Count == 0) location.debris.Remove(debris);
            }
        }
        finally
        {
            locationField.SetValue(game, previousLocation);
        }
    }

    internal static Dictionary<(string ItemId, int Quality), int> SnapshotInventoryTotals(Farmer farmer)
    {
        var totals = new Dictionary<(string, int), int>();
        foreach (var item in farmer.Items)
        {
            if (item is null) continue;
            var key = (item.QualifiedItemId ?? item.ItemId ?? item.Name, item.Quality);
            totals.TryGetValue(key, out int current);
            totals[key] = current + item.Stack;
        }
        return totals;
    }

    private static string? FindItemName(Farmer farmer, string? itemId)
    {
        if (itemId is null) return null;
        foreach (var item in farmer.Items)
        {
            if (item is null) continue;
            string id = item.QualifiedItemId ?? item.ItemId ?? item.Name;
            if (string.Equals(id, itemId, StringComparison.Ordinal))
                return item.Name;
        }
        return null;
    }
}
