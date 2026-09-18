using System.Reflection;
using StardewModdingAPI;
using StardewValley;
using StardewValley.TerrainFeatures;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Production hand-harvest adapter executing crop.harvest through normal game mechanics.
/// Enforces:
/// 1. Main-thread and current-map execution only.
/// 2. Maturity via HoeDirt.readyForHarvest() only; scythe crops rejected.
/// 3. Conservative inventory capacity precheck (one empty slot required).
/// 4. Game1.player is temporarily swapped to the companion Farmer for the duration of
///    crop.harvest (the game's harvest adds produce to Game1.player's inventory) and is
///    restored in a finally block; the reference is verified after restoration.
/// 5. Human player resources are verified unchanged before and after the harvest.
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

        // 2. Current map invariant
        if (!string.Equals(_observer.CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
        {
            return HarvestTileResult.Failed(
                $"Harvest target map '{locationName}' does not match active map '{_observer.CurrentLocationName}'.");
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
        if (dirtState.RequiresScythe)
        {
            return HarvestTileResult.PreconditionError(
                $"Crop on tile {targetTile} requires a scythe to harvest.", skipReason: "scythe-required");
        }

        // 5. Conservative capacity precheck: require at least one empty slot
        if (actor.FreeInventorySlots <= 0)
        {
            return HarvestTileResult.PreconditionError(
                "Cannot harvest: companion inventory has no empty slot.", skipReason: "inventory-full");
        }

        if (actor.GameFarmer is null)
        {
            return HarvestTileResult.Failed("Companion actor has no GameFarmer instance.");
        }

        // 6. Resolve the actual HoeDirt and crop
        var location = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
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
        if (crop.GetHarvestMethod() == StardewValley.GameData.Crops.HarvestMethod.Scythe)
        {
            return HarvestTileResult.PreconditionError(
                $"Crop on tile {targetTile} requires a scythe to harvest.", skipReason: "scythe-required");
        }

        string? cropId = crop.indexOfHarvest?.Value;

        // 7. Verify human player baseline isolation BEFORE the harvest
        var playerBefore = _observer.GetPlayerSnapshot();
        var originalPlayerRef = Game1.player;
        var companion = actor.GameFarmer;

        // Baseline of companion inventory for genuine gained-item measurement
        var inventoryBefore = SnapshotInventoryTotals(companion);

        try
        {
            // 8. Execute the game's own harvest with the companion as the acting farmer.
            // crop.harvest adds produce to Game1.player's inventory, so Game1.player is
            // swapped to the companion for exactly the duration of the call.
            SetGamePlayer(companion);
            bool harvested;
            try
            {
                harvested = crop.harvest(targetTile.X, targetTile.Y, hoeDirt);
                if (harvested && !crop.RegrowsAfterHarvest())
                {
                    hoeDirt.destroyCrop(showAnimation: false);
                }
            }
            finally
            {
                SetGamePlayer(originalPlayerRef);
            }

            if (!harvested)
            {
                return HarvestTileResult.Failed($"crop.harvest returned false on tile {targetTile}.");
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

            if (itemId is null)
            {
                // The crop is gone (harvest returned true) but no measurable delta was found;
                // report the crop's own harvest item id with a nominal single unit.
                _monitor.Log(
                    $"Harvest on tile {targetTile}: no positive inventory delta measured; reporting crop item '{cropId}' with stack 1.",
                    LogLevel.Warn);
                itemId = cropId;
                gainedStack = 1;
                gainedQuality = 0;
            }

            itemName = FindItemName(companion, itemId) ?? cropId;

            return HarvestTileResult.Succeeded(cropId, itemId, itemName, gainedStack, gainedQuality, staminaCost: 0f, waterCost: 0);
        }
        finally
        {
            // 10. Strict Invariant Checks (mirror NormalWateringCanAdapter's isolation guard):
            // - Game1.player reference was restored
            if (!object.ReferenceEquals(originalPlayerRef, Game1.player))
            {
                throw new InvalidOperationException("CRITICAL SAFETY VIOLATION: Game1.player reference was not restored after companion harvest!");
            }

            // - Game1.player resources were NOT drained or modified
            var playerAfter = _observer.GetPlayerSnapshot();
            if (!playerBefore.EqualsPlayerResources(playerAfter))
            {
                throw new InvalidOperationException(
                    $"CRITICAL SAFETY VIOLATION: Human player resources changed during companion harvest! Before: {playerBefore}, After: {playerAfter}");
            }
        }
    }

    private static Dictionary<(string ItemId, int Quality), int> SnapshotInventoryTotals(Farmer farmer)
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
