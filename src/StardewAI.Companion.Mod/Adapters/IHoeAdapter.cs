using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Result of an atomic hoe operation on a single tile.
/// </summary>
public sealed class HoeTileResult
{
    public bool Success { get; }
    public string? ErrorMessage { get; }
    public bool PreconditionFailed { get; }

    /// <summary>
    /// Set when the tile is skipped rather than failed:
    /// "already-tilled", "has-crop", "tile-occupied", "not-diggable", "missing-tool", or "stamina-exhausted".
    /// </summary>
    public string? SkipReason { get; }

    public float StaminaCost { get; }

    private HoeTileResult(
        bool success,
        string? errorMessage,
        bool preconditionFailed,
        string? skipReason,
        float staminaCost)
    {
        Success = success;
        ErrorMessage = errorMessage;
        PreconditionFailed = preconditionFailed;
        SkipReason = skipReason;
        StaminaCost = staminaCost;
    }

    public static HoeTileResult Succeeded(float staminaCost = 0f) =>
        new(true, null, false, null, staminaCost);

    public static HoeTileResult PreconditionError(string reason, string? skipReason = null) =>
        new(false, reason, true, skipReason, 0f);

    public static HoeTileResult Failed(string reason) =>
        new(false, reason, false, null, 0f);
}

/// <summary>
/// Adapter contract for executing a single-tile hoe operation through normal game mechanics.
/// </summary>
public interface IHoeAdapter
{
    /// <summary>
    /// Uses the companion's Hoe on the target tile to create tilled dirt.
    /// Enforces:
    /// - Current-map and main-thread execution.
    /// - Companion must possess a Hoe tool.
    /// - Companion pays actual stamina cost through Hoe.DoFunction.
    /// - Protects existing crops and placed objects: tiles with crops or obstacles are skipped.
    /// - Human player resources and Game1.player reference are strictly preserved.
    /// </summary>
    HoeTileResult HoeTile(
        IFarmerActor actor,
        string locationName,
        TileCoordinate targetTile);
}
