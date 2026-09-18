using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>
/// In-memory test double for IHoeAdapter.
/// Executes realistic hoe effects on SimulatedWorldObserver and deductions on IFarmerActor
/// without requiring the Stardew Valley game runtime.
/// </summary>
public sealed class TestHoeAdapter : IHoeAdapter
{
    private readonly SimulatedWorldObserver _observer;
    private readonly Action<string, TileCoordinate>? _onHoed;

    public int HoeCallCount { get; private set; }
    public List<TileCoordinate> HoedTiles { get; } = new();
    public float StaminaCostPerTile { get; set; } = 1.8f;
    public bool SimulateDoFunctionFailure { get; set; }

    public TestHoeAdapter(
        SimulatedWorldObserver observer,
        Action<string, TileCoordinate>? onHoed = null)
    {
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _onHoed = onHoed;
    }

    public HoeTileResult HoeTile(
        IFarmerActor actor,
        string locationName,
        TileCoordinate targetTile)
    {
        ArgumentNullException.ThrowIfNull(actor);

        if (!_observer.IsMainThread)
        {
            return HoeTileResult.Failed("Hoe operation rejected: must execute on main thread.");
        }

        if (!string.Equals(_observer.CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
        {
            return HoeTileResult.Failed($"Location mismatch: current '{_observer.CurrentLocationName}', target '{locationName}'.");
        }

        if (actor.Hoe is null)
        {
            return HoeTileResult.PreconditionError("Cannot hoe tile: companion does not possess a Hoe tool.", skipReason: "missing-tool");
        }

        // Exhaustion check only (no hardcoded fixed 2-stamina gate)
        if (actor.IsExhausted || actor.Stamina <= 0f)
        {
            return HoeTileResult.PreconditionError($"Stamina exhausted (stamina={actor.Stamina:F1}).", skipReason: "stamina-exhausted");
        }

        if (!actor.Tile.IsAdjacentTo(targetTile))
        {
            return HoeTileResult.PreconditionError($"Actor at {actor.Tile} is not adjacent to target {targetTile}.");
        }

        var dirt = _observer.GetDirtState(locationName, targetTile);
        if (dirt.HasCrop)
        {
            return HoeTileResult.PreconditionError($"Tile {targetTile} already contains a crop; preserving.", skipReason: "has-crop");
        }
        if (dirt.IsTilled)
        {
            return HoeTileResult.PreconditionError($"Tile {targetTile} is already tilled.", skipReason: "already-tilled");
        }

        if (SimulateDoFunctionFailure)
        {
            return HoeTileResult.Failed($"Hoe.DoFunction simulated failure on {targetTile}.");
        }

        // Deduct stamina dynamically
        actor.Stamina = Math.Max(0f, actor.Stamina - StaminaCostPerTile);

        // Turn ground into tilled soil in simulated world
        _observer.SetDirt(targetTile, TileDirtState.DryDirt());

        HoeCallCount++;
        HoedTiles.Add(targetTile);
        _onHoed?.Invoke(locationName, targetTile);

        return HoeTileResult.Succeeded(StaminaCostPerTile);
    }
}
