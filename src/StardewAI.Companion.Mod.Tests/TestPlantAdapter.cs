using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>
/// In-memory test double for IPlantAdapter.
/// Simulates seed planting, season checks, and strict item deduction verification.
/// </summary>
public sealed class TestPlantAdapter : IPlantAdapter
{
    private readonly SimulatedWorldObserver _observer;
    private readonly Action<string, TileCoordinate, string>? _onPlanted;

    public int PlantCallCount { get; private set; }
    public List<TileCoordinate> PlantedTiles { get; } = new();
    public HashSet<string> AllowedSeasons { get; } = new(StringComparer.OrdinalIgnoreCase) { "spring" };
    public bool SimulateConsumeItemFailure { get; set; }

    public TestPlantAdapter(
        SimulatedWorldObserver observer,
        Action<string, TileCoordinate, string>? onPlanted = null)
    {
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _onPlanted = onPlanted;
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

        if (!_observer.IsMainThread)
        {
            return PlantTileResult.Failed("Plant operation rejected: must execute on main thread.");
        }

        if (!string.Equals(_observer.CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
        {
            return PlantTileResult.Failed($"Location mismatch: current '{_observer.CurrentLocationName}', target '{locationName}'.");
        }

        if (!actor.Tile.IsAdjacentTo(targetTile))
        {
            return PlantTileResult.PreconditionError($"Actor at {actor.Tile} is not adjacent to target {targetTile}.");
        }

        int available = actor.GetItemCount(seedItemId);
        if (available <= 0)
        {
            return PlantTileResult.PreconditionError($"No seed '{seedItemId}' in inventory.", skipReason: "out-of-seeds");
        }

        var dirt = _observer.GetDirtState(locationName, targetTile);
        if (dirt.HasCrop)
        {
            return PlantTileResult.PreconditionError($"Tile {targetTile} already has crop; preserving.", skipReason: "already-has-crop");
        }
        if (!dirt.IsTilled)
        {
            return PlantTileResult.PreconditionError($"Tile {targetTile} is not tilled.", skipReason: "not-tilled");
        }

        if (!AllowedSeasons.Contains(_observer.CurrentSeason))
        {
            return PlantTileResult.PreconditionError($"Seed '{seedItemId}' not suitable for season '{_observer.CurrentSeason}'.", skipReason: "invalid-season");
        }

        if (SimulateConsumeItemFailure)
        {
            // Simulate TryConsumeItem failure (e.g. race condition or discrepancy)
            return PlantTileResult.Failed($"Failed to consume seed '{seedItemId}' from companion inventory.");
        }

        // Real deduction from companion actor
        bool consumed = actor.TryConsumeItem(seedItemId, 1);
        if (!consumed)
        {
            return PlantTileResult.Failed($"Failed to consume seed '{seedItemId}' from companion inventory after planting.");
        }

        int remainingStack = actor.GetItemCount(seedItemId);

        // Place crop in simulated dirt
        _observer.SetDirt(targetTile, TileDirtState.DryDirt(hasCrop: true, cropId: seedItemId));

        PlantCallCount++;
        PlantedTiles.Add(targetTile);
        _onPlanted?.Invoke(locationName, targetTile, seedItemId);

        return PlantTileResult.Succeeded(seedItemId, remainingStack);
    }
}
