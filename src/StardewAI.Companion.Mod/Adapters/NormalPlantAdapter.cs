using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.TerrainFeatures;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Production adapter for single-tile seed planting using normal game HoeDirt.plant mechanics.
/// Enforces:
/// 1. Main-thread execution on the companion's loaded map.
/// 2. Adjacency check.
/// 3. Seed availability precheck in companion inventory.
/// 4. Tile must be tilled soil without existing crop or obstacle (CRITICAL: preserve existing crops).
/// 5. Season and location legality via HoeDirt.canPlantThisSeedHere.
/// 6. Real seed deduction from companion inventory (no free creation from air).
///    Player-bound actors instead plant through the full native placementAction path,
///    which consumes the seed natively (reduceActiveItemByOne).
/// 7. Strict human player resource and Game1.player isolation (companion mode only;
///    skipped for player-bound actors, where the actor IS the player).
/// </summary>
public sealed class NormalPlantAdapter : IPlantAdapter
{
    private readonly IWorldObserver _observer;
    private readonly IMonitor _monitor;

    public NormalPlantAdapter(
        IWorldObserver observer,
        IMonitor monitor)
    {
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _monitor = monitor ?? throw new ArgumentNullException(nameof(monitor));
    }

    public PlantTileResult PlantTile(
        IFarmerActor actor,
        string locationName,
        TileCoordinate targetTile,
        string seedItemId)
    {
        ArgumentNullException.ThrowIfNull(actor);
        if (string.IsNullOrWhiteSpace(seedItemId))
        {
            return PlantTileResult.Failed("seedItemId cannot be null or empty.");
        }

        // 1. Thread safety invariant
        if (!_observer.IsMainThread)
        {
            return PlantTileResult.Failed("Plant operation rejected: must execute on the game main thread.");
        }

        // 2. The companion's map is independent of the human player's active map.
        if (!string.Equals(actor.LocationName, locationName, StringComparison.OrdinalIgnoreCase))
        {
            return PlantTileResult.Failed(
                $"Plant target map '{locationName}' does not match companion map '{actor.LocationName}'.");
        }
        if (!_observer.LocationExists(locationName))
        {
            return PlantTileResult.Failed($"Plant target map '{locationName}' is not loaded or does not exist.");
        }

        // 3. Adjacency check
        if (!actor.Tile.IsAdjacentTo(targetTile))
        {
            return PlantTileResult.PreconditionError(
                $"Actor at {actor.Tile} is not adjacent to target tile {targetTile}.");
        }

        // 4. Precheck seed in companion inventory
        int availableSeeds = actor.GetItemCount(seedItemId);
        if (availableSeeds <= 0)
        {
            return PlantTileResult.PreconditionError(
                $"Cannot plant: companion has no seed '{seedItemId}' in inventory.", skipReason: "out-of-seeds");
        }

        // 5. Precheck tile state & crop protection (CRITICAL: preserve existing crops)
        var dirtState = _observer.GetDirtState(locationName, targetTile);
        if (dirtState.HasCrop)
        {
            return PlantTileResult.PreconditionError(
                $"Tile {targetTile} already contains a crop; preserving without alteration.", skipReason: "already-has-crop");
        }
        if (!dirtState.IsTilled)
        {
            return PlantTileResult.PreconditionError(
                $"Tile {targetTile} is not tilled soil.", skipReason: "not-tilled");
        }

        var location = Game1.getLocationFromName(locationName);
        if (location is null)
        {
            return PlantTileResult.Failed($"Game location '{locationName}' not found.");
        }

        var tileVector = new Vector2(targetTile.X, targetTile.Y);
        if (!location.terrainFeatures.TryGetValue(tileVector, out var tf) || tf is not HoeDirt hoeDirt)
        {
            return PlantTileResult.PreconditionError(
                $"Tile {targetTile} does not contain HoeDirt.", skipReason: "not-tilled");
        }

        if (hoeDirt.crop is not null)
        {
            return PlantTileResult.PreconditionError(
                $"Tile {targetTile} already contains a crop; preserving.", skipReason: "already-has-crop");
        }

        if (location.isObjectAtTile(targetTile.X, targetTile.Y))
        {
            return PlantTileResult.PreconditionError(
                $"Tile {targetTile} is occupied by an object.", skipReason: "tile-occupied");
        }

        string cropSeedId = SeedIdNormalizer.ToCropDataId(seedItemId);

        // 6. Game-authoritative legality & season check
        if (!hoeDirt.canPlantThisSeedHere(cropSeedId, false))
        {
            return PlantTileResult.PreconditionError(
                $"Seed '{seedItemId}' cannot be planted on tile {targetTile} (season mismatch or invalid location).",
                skipReason: "invalid-season");
        }

        if (actor.GameFarmer is null)
        {
            return PlantTileResult.Failed("Companion actor has no GameFarmer instance.");
        }

        if (ActorGuards.IsPlayerBound(actor))
        {
            // Player mode: use the full native planting path (placementAction plants
            // into the HoeDirt through the game's own mechanics and the native caller
            // consumes the seed via reduceActiveItemByOne). No manual debit and no
            // human-player invariants: the actor IS the player.
            return PlantViaNativePlacement(actor, location, targetTile, seedItemId);
        }

        // 7. Verify human player baseline isolation BEFORE planting
        var playerBefore = _observer.GetPlayerSnapshot();
        var originalPlayerRef = Game1.player;

        try
        {
            // Execute normal game planting using companion Farmer
            bool planted = hoeDirt.plant(cropSeedId, actor.GameFarmer, isFertilizer: false);
            if (!planted)
            {
                return PlantTileResult.Failed($"hoeDirt.plant returned false for seed '{seedItemId}' on tile {targetTile}.");
            }

            // Real deduction from companion inventory
            bool consumed = actor.TryConsumeItem(seedItemId, 1);
            if (!consumed)
            {
                hoeDirt.destroyCrop(showAnimation: false);
                return PlantTileResult.Failed($"Failed to consume seed '{seedItemId}' from companion inventory after planting.");
            }

            int remainingStack = actor.GetItemCount(seedItemId);

            return PlantTileResult.Succeeded(seedItemId, remainingStack);
        }
        finally
        {
            // 8. Strict Invariant Checks:
            if (!object.ReferenceEquals(originalPlayerRef, Game1.player))
            {
                throw new InvalidOperationException("CRITICAL SAFETY VIOLATION: Game1.player reference was modified during planting!");
            }

            var playerAfter = _observer.GetPlayerSnapshot();
            if (!playerBefore.EqualsPlayerResources(playerAfter))
            {
                throw new InvalidOperationException(
                    $"CRITICAL SAFETY VIOLATION: Human player resources changed during companion planting! Before: {playerBefore}, After: {playerAfter}");
            }
        }
    }

    /// <summary>
    /// Player-mode planting through the full native path: the seed object at the
    /// player's active slot plants into the HoeDirt via
    /// <see cref="StardewValley.Object.placementAction"/> (which performs the same
    /// legality checks and HoeDirt.plant call as a real click), and the native
    /// caller-side consumption (Farmer.reduceActiveItemByOne) deducts exactly one
    /// seed. The previous active slot is restored afterwards.
    /// </summary>
    private static PlantTileResult PlantViaNativePlacement(
        IFarmerActor actor,
        GameLocation location,
        TileCoordinate targetTile,
        string seedItemId)
    {
        var player = actor.GameFarmer!;

        // Resolve the seed slot: seeds are StardewValley.Object with Category -74,
        // matched by the same id rules the companion inventory uses.
        int seedSlot = -1;
        StardewValley.Object? seedObject = null;
        var items = player.Items;
        for (int i = 0; i < items.Count; i++)
        {
            if (items[i] is StardewValley.Object obj && obj.Category == -74 &&
                FarmerMechanicsActor.ItemMatchesId(obj, seedItemId))
            {
                seedSlot = i;
                seedObject = obj;
                break;
            }
        }

        if (seedObject is null || seedSlot < 0)
        {
            return PlantTileResult.PreconditionError(
                $"Cannot plant: player has no seed '{seedItemId}' in inventory.", skipReason: "out-of-seeds");
        }

        int previousToolIndex = player.CurrentToolIndex;
        bool placed;
        try
        {
            // placementAction reads the seed through Farmer.ActiveObject, so point the
            // player's active slot at the seed for the duration of the native call.
            player.CurrentToolIndex = seedSlot;
            placed = seedObject.placementAction(location, targetTile.X * 64, targetTile.Y * 64, player);
            if (placed)
            {
                // Mirror the native caller (Utility.tryToPlaceItem): a successful
                // seed placement consumes exactly one unit of the active item.
                player.reduceActiveItemByOne();
            }
        }
        finally
        {
            player.CurrentToolIndex = previousToolIndex;
        }

        if (!placed)
        {
            return PlantTileResult.Failed(
                $"seedObject.placementAction returned false for seed '{seedItemId}' on tile {targetTile}.");
        }

        int remainingStack = actor.GetItemCount(seedItemId);
        return PlantTileResult.Succeeded(seedItemId, remainingStack);
    }
}
