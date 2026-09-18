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
/// 1. Main-thread and current-map execution.
/// 2. Adjacency check.
/// 3. Seed availability precheck in companion inventory.
/// 4. Tile must be tilled soil without existing crop or obstacle (CRITICAL: preserve existing crops).
/// 5. Season and location legality via HoeDirt.canPlantThisSeedHere.
/// 6. Real seed deduction from companion inventory (no free creation from air).
/// 7. Strict human player resource and Game1.player isolation.
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

        // 2. Current map invariant
        if (!string.Equals(_observer.CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
        {
            return PlantTileResult.Failed(
                $"Plant target map '{locationName}' does not match active map '{_observer.CurrentLocationName}'.");
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

        var location = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
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
}
