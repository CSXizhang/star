using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Result of an atomic seed planting operation on a single tile.
/// </summary>
public sealed class PlantTileResult
{
    public bool Success { get; }
    public string? ErrorMessage { get; }
    public bool PreconditionFailed { get; }

    /// <summary>
    /// Set when the tile is skipped:
    /// "not-tilled", "already-has-crop", "tile-occupied", "invalid-season", "out-of-seeds".
    /// </summary>
    public string? SkipReason { get; }

    public string? PlantedSeedId { get; }
    public int RemainingSeedStack { get; }

    private PlantTileResult(
        bool success,
        string? errorMessage,
        bool preconditionFailed,
        string? skipReason,
        string? plantedSeedId,
        int remainingSeedStack)
    {
        Success = success;
        ErrorMessage = errorMessage;
        PreconditionFailed = preconditionFailed;
        SkipReason = skipReason;
        PlantedSeedId = plantedSeedId;
        RemainingSeedStack = remainingSeedStack;
    }

    public static PlantTileResult Succeeded(string seedId, int remainingStack) =>
        new(true, null, false, null, seedId, remainingStack);

    public static PlantTileResult PreconditionError(string reason, string? skipReason = null) =>
        new(false, reason, true, skipReason, null, 0);

    public static PlantTileResult Failed(string reason) =>
        new(false, reason, false, null, null, 0);
}

/// <summary>
/// Adapter contract for executing a single-tile seed planting operation through normal game mechanics.
/// </summary>
public interface IPlantAdapter
{
    /// <summary>
    /// Plants the specified seed onto the target tilled tile.
    /// Enforces:
    /// - Current-map and main-thread execution.
    /// - Adjacency check.
    /// - Companion must have the seed item in inventory with stack >= 1.
    /// - Tile must be tilled soil without an existing crop or obstacle.
    /// - Seed must be plantable here (season and location checks via HoeDirt.canPlantThisSeedHere).
    /// - Deducts seed from companion's inventory genuinely.
    /// - Human player resources and Game1.player reference are strictly preserved.
    /// </summary>
    PlantTileResult PlantTile(
        IFarmerActor actor,
        string locationName,
        TileCoordinate targetTile,
        string seedItemId);
}
