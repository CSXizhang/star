using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Production adapter for single-tile hoe operation using the companion's Hoe tool.
/// Enforces:
/// 1. Main-thread and current-map execution.
/// 2. Adjacency check.
/// 3. Companion must possess a Hoe and sufficient stamina.
/// 4. Strict preservation of existing crops and placed objects:
///    tiles with crops, objects, or non-soil features are skipped, NEVER destroyed.
/// 5. Executes through normal game tool lifecycle (Hoe.DoFunction).
/// 6. Independent Mechanics Actor pays stamina cost determined by game mechanics.
/// 7. Human player resources and Game1.player reference are strictly preserved.
/// </summary>
public sealed class NormalHoeAdapter : IHoeAdapter
{
    private readonly IWorldObserver _observer;
    private readonly IMonitor _monitor;

    public NormalHoeAdapter(
        IWorldObserver observer,
        IMonitor monitor)
    {
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _monitor = monitor ?? throw new ArgumentNullException(nameof(monitor));
    }

    public HoeTileResult HoeTile(
        IFarmerActor actor,
        string locationName,
        TileCoordinate targetTile)
    {
        ArgumentNullException.ThrowIfNull(actor);

        // 1. Thread safety invariant
        if (!_observer.IsMainThread)
        {
            return HoeTileResult.Failed("Hoe operation rejected: must execute on the game main thread.");
        }

        // 2. Current map invariant
        if (!string.Equals(_observer.CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
        {
            return HoeTileResult.Failed(
                $"Hoe target map '{locationName}' does not match active map '{_observer.CurrentLocationName}'.");
        }

        // 3. Adjacency check
        if (!actor.Tile.IsAdjacentTo(targetTile))
        {
            return HoeTileResult.PreconditionError(
                $"Actor at {actor.Tile} is not adjacent to target tile {targetTile}.");
        }

        // 4. Precheck companion Hoe tool
        var hoe = actor.Hoe;
        if (hoe is null)
        {
            return HoeTileResult.PreconditionError(
                "Cannot hoe tile: companion does not possess a Hoe tool.", skipReason: "missing-tool");
        }

        // 5. Precheck stamina (exhaustion only; no hardcoded per-tile cost threshold)
        if (actor.IsExhausted || actor.Stamina <= 0f)
        {
            return HoeTileResult.PreconditionError(
                $"Cannot hoe tile: companion is exhausted (stamina={actor.Stamina:F1}).",
                skipReason: "stamina-exhausted");
        }

        // 6. Precheck tile state & crop protection (CRITICAL: preserve existing crops & objects)
        var dirtState = _observer.GetDirtState(locationName, targetTile);
        if (dirtState.HasCrop)
        {
            return HoeTileResult.PreconditionError(
                $"Tile {targetTile} already contains a crop; preserving without alteration.", skipReason: "has-crop");
        }
        if (dirtState.IsTilled)
        {
            return HoeTileResult.PreconditionError(
                $"Tile {targetTile} is already tilled soil.", skipReason: "already-tilled");
        }

        var location = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
        if (location is null)
        {
            return HoeTileResult.Failed($"Game location '{locationName}' not found.");
        }

        // Check for placed objects (chests, machines, stones, weeds, twigs)
        if (location.isObjectAtTile(targetTile.X, targetTile.Y))
        {
            return HoeTileResult.PreconditionError(
                $"Tile {targetTile} is occupied by an object; preserving without clearing.", skipReason: "tile-occupied");
        }

        // Check for terrain features (trees, bushes, etc.)
        var tileVector = new Vector2(targetTile.X, targetTile.Y);
        if (location.terrainFeatures.TryGetValue(tileVector, out var tf) && tf is not HoeDirt)
        {
            return HoeTileResult.PreconditionError(
                $"Tile {targetTile} contains a terrain feature ({tf.GetType().Name}); preserving.", skipReason: "tile-occupied");
        }

        // Check map diggability & passability
        if (location.doesTileHaveProperty(targetTile.X, targetTile.Y, "Diggable", "Back") is null)
        {
            return HoeTileResult.PreconditionError(
                $"Tile {targetTile} is not diggable soil.", skipReason: "not-diggable");
        }
        if (!location.isTilePassable(new xTile.Dimensions.Location(targetTile.X, targetTile.Y), Game1.viewport))
        {
            return HoeTileResult.PreconditionError(
                $"Tile {targetTile} is impassable.", skipReason: "not-diggable");
        }

        if (actor.GameFarmer is null)
        {
            return HoeTileResult.Failed("Companion actor has no GameFarmer instance.");
        }

        // 7. Verify human player baseline isolation BEFORE tool use
        var playerBefore = _observer.GetPlayerSnapshot();
        var originalPlayerRef = Game1.player;

        try
        {
            int pixelX = targetTile.X * 64 + 32;
            int pixelY = targetTile.Y * 64 + 32;
            float companionStaminaBefore = actor.Stamina;

            // Execute normal game tool execution using the companion's independent Farmer
            hoe.DoFunction(location, pixelX, pixelY, power: 1, who: actor.GameFarmer);

            // Nuclear check: verify tile ACTUALLY became HoeDirt in world state
            bool isNowDirt = location.terrainFeatures.TryGetValue(tileVector, out var newTf) && newTf is HoeDirt;
            if (!isNowDirt)
            {
                return HoeTileResult.Failed(
                    $"Hoe.DoFunction invoked but tile {targetTile} was not converted to HoeDirt.");
            }

            float measuredStaminaCost = Math.Max(0f, companionStaminaBefore - actor.Stamina);
            return HoeTileResult.Succeeded(measuredStaminaCost);
        }
        finally
        {
            // 8. Strict Invariant Checks:
            if (!object.ReferenceEquals(originalPlayerRef, Game1.player))
            {
                throw new InvalidOperationException("CRITICAL SAFETY VIOLATION: Game1.player reference was modified during hoe action!");
            }

            var playerAfter = _observer.GetPlayerSnapshot();
            if (!playerBefore.EqualsPlayerResources(playerAfter))
            {
                throw new InvalidOperationException(
                    $"CRITICAL SAFETY VIOLATION: Human player resources changed during companion hoe action! Before: {playerBefore}, After: {playerAfter}");
            }
        }
    }
}
