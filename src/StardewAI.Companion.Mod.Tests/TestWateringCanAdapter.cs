using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>
/// In-memory test double for IWateringCanAdapter.
/// Executes realistic watering effects on SimulatedWorldObserver and deductions on IFarmerActor
/// without requiring the Stardew Valley game runtime or Game1.content.
/// </summary>
public sealed class TestWateringCanAdapter : IWateringCanAdapter
{
    private readonly SimulatedWorldObserver _observer;
    private readonly Action<string, TileCoordinate>? _onWatered;

    public int WaterCallCount { get; private set; }
    public List<TileCoordinate> WateredTiles { get; } = new();

    public TestWateringCanAdapter(
        SimulatedWorldObserver observer,
        Action<string, TileCoordinate>? onWatered = null)
    {
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _onWatered = onWatered;
    }

    public WaterTileResult WaterTile(
        IFarmerActor actor,
        string locationName,
        TileCoordinate targetTile)
    {
        ArgumentNullException.ThrowIfNull(actor);

        if (!_observer.IsMainThread)
        {
            return WaterTileResult.Failed("Watering can operation rejected: must execute on main thread.");
        }

        if (!string.Equals(_observer.CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
        {
            return WaterTileResult.Failed($"Location mismatch: current '{_observer.CurrentLocationName}', target '{locationName}'.");
        }

        if (actor.IsWateringCanEmpty || actor.WaterLeft < NormalWateringCanAdapter.BaseWaterCost)
        {
            return WaterTileResult.PreconditionError($"Watering can is empty (water={actor.WaterLeft}).");
        }

        if (actor.Stamina < NormalWateringCanAdapter.BaseStaminaCost)
        {
            return WaterTileResult.PreconditionError($"Stamina exhausted (stamina={actor.Stamina:F1}).");
        }

        if (!actor.Tile.IsAdjacentTo(targetTile))
        {
            return WaterTileResult.PreconditionError($"Actor at {actor.Tile} is not adjacent to target {targetTile}.");
        }

        var dirt = _observer.GetDirtState(locationName, targetTile);
        if (!dirt.IsTilled)
        {
            return WaterTileResult.PreconditionError($"Tile {targetTile} is not tilled.");
        }
        if (dirt.IsWatered)
        {
            return WaterTileResult.PreconditionError($"Tile {targetTile} is already watered.");
        }

        // Deduct actor resources
        actor.Stamina -= NormalWateringCanAdapter.BaseStaminaCost;
        actor.WaterLeft -= NormalWateringCanAdapter.BaseWaterCost;

        // Apply watering effect in simulated world
        _observer.SetDirt(targetTile, TileDirtState.WateredDirt());

        WaterCallCount++;
        WateredTiles.Add(targetTile);
        _onWatered?.Invoke(locationName, targetTile);

        return WaterTileResult.Succeeded(NormalWateringCanAdapter.BaseStaminaCost, NormalWateringCanAdapter.BaseWaterCost);
    }
}
