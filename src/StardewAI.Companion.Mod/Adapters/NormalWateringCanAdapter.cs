using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Normal Watering Can Adapter implementing production tool execution.
/// Enforces:
/// 1. Main-thread and current-map execution only.
/// 2. Precheck empty can before invoking tool logic (prevents accessing Game1.player empty-can path).
/// 3. Human player resources are verified unchanged before and after tool use.
/// 4. Game1.player is never swapped.
/// 5. No direct soil-state mutation (executes through actual WateringCan tool lifecycle).
/// 6. Independent Mechanics Actor pays actual stamina and water costs.
/// </summary>
public sealed class NormalWateringCanAdapter : IWateringCanAdapter
{
    public const float BaseStaminaCost = 2.0f;
    public const int BaseWaterCost = 1;

    private readonly IWorldObserver _observer;
    private readonly IMonitor _monitor;

    public NormalWateringCanAdapter(
        IWorldObserver observer,
        IMonitor monitor)
    {
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _monitor = monitor ?? throw new ArgumentNullException(nameof(monitor));
    }

    public WaterTileResult WaterTile(
        IFarmerActor actor,
        string locationName,
        TileCoordinate targetTile)
    {
        ArgumentNullException.ThrowIfNull(actor);

        // 1. Thread safety invariant
        if (!_observer.IsMainThread)
        {
            return WaterTileResult.Failed("Watering can operation rejected: must execute on the game main thread.");
        }

        // 2. Target map must be loaded. The companion works on its own logical map,
        // which may differ from the player's active map (e.g. the morning after a
        // pass-out the player wakes in the FarmHouse while the companion is on the Farm).
        if (!_observer.LocationExists(locationName))
        {
            return WaterTileResult.Failed($"Watering target map '{locationName}' is not loaded or does not exist.");
        }

        // 3. Precheck empty can (CRITICAL: prevent invoking game empty-can path which touches Game1.player)
        if (actor.IsWateringCanEmpty || actor.WaterLeft < BaseWaterCost)
        {
            return WaterTileResult.PreconditionError(
                $"Cannot water tile: companion watering can is empty (water={actor.WaterLeft}).");
        }

        // 4. Precheck actor stamina
        if (actor.Stamina < BaseStaminaCost)
        {
            return WaterTileResult.PreconditionError(
                $"Cannot water tile: companion stamina is exhausted (stamina={actor.Stamina:F1} < {BaseStaminaCost:F1}).");
        }

        // 5. Precheck adjacency
        if (!actor.Tile.IsAdjacentTo(targetTile))
        {
            return WaterTileResult.PreconditionError(
                $"Actor at {actor.Tile} is not adjacent to target tile {targetTile}.");
        }

        // 6. Precheck soil state
        var dirtState = _observer.GetDirtState(locationName, targetTile);
        if (!dirtState.IsTilled)
        {
            return WaterTileResult.PreconditionError($"Tile {targetTile} does not contain tilled soil.");
        }
        if (dirtState.IsWatered)
        {
            return WaterTileResult.PreconditionError($"Tile {targetTile} is already watered.");
        }

        // 7. Verify human player baseline isolation BEFORE tool use
        var playerBefore = _observer.GetPlayerSnapshot();
        var originalPlayerRef = Game1.player;

        try
        {
            // 8. Execute tool action through actual WateringCan lifecycle
            var location = Game1.getLocationFromName(locationName);
            if (location is null)
            {
                return WaterTileResult.Failed($"Game location '{locationName}' not found.");
            }

            var can = actor.WateringCan;
            if (can is null)
            {
                return WaterTileResult.Failed("Companion actor has no WateringCan equipped.");
            }

            if (actor.GameFarmer is null)
            {
                return WaterTileResult.Failed("Companion actor has no GameFarmer instance.");
            }

            int pixelX = targetTile.X * 64 + 32;
            int pixelY = targetTile.Y * 64 + 32;

            float companionStaminaBefore = actor.Stamina;
            int companionWaterBefore = actor.WaterLeft;

            // Call normal game tool execution using the companion's independent Farmer
            can.DoFunction(location, pixelX, pixelY, power: 1, who: actor.GameFarmer);

            // Capture before/after actual costs directly from game execution:
            float measuredStaminaCost = Math.Max(0f, companionStaminaBefore - actor.Stamina);
            int measuredWaterCost = Math.Max(0, companionWaterBefore - actor.WaterLeft);

            return WaterTileResult.Succeeded(measuredStaminaCost, measuredWaterCost);
        }
        finally
        {
            // 9. Strict Invariant Checks:
            // - Game1.player was NOT swapped
            if (!object.ReferenceEquals(originalPlayerRef, Game1.player))
            {
                throw new InvalidOperationException("CRITICAL SAFETY VIOLATION: Game1.player reference was modified!");
            }

            // - Game1.player resources were NOT drained or modified
            var playerAfter = _observer.GetPlayerSnapshot();
            if (!playerBefore.EqualsPlayerResources(playerAfter))
            {
                throw new InvalidOperationException(
                    $"CRITICAL SAFETY VIOLATION: Human player resources changed during companion tool action! Before: {playerBefore}, After: {playerAfter}");
            }
        }
    }
}
