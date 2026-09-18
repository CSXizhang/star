using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Result of an atomic hand-harvest operation on a single crop tile.
/// </summary>
public sealed class HarvestTileResult
{
    public bool Success { get; }
    public string? ErrorMessage { get; }
    public bool PreconditionFailed { get; }

    /// <summary>
    /// Set when the tile must be skipped rather than failed:
    /// "not-ready", "scythe-required", or "inventory-full".
    /// </summary>
    public string? SkipReason { get; }

    public string? CropId { get; }
    public string? ItemId { get; }
    public string? ItemName { get; }
    public int Stack { get; }
    public int Quality { get; }
    public float StaminaCost { get; }
    public int WaterCost { get; }

    private HarvestTileResult(
        bool success,
        string? errorMessage,
        bool preconditionFailed,
        string? skipReason,
        string? cropId,
        string? itemId,
        string? itemName,
        int stack,
        int quality,
        float staminaCost,
        int waterCost)
    {
        Success = success;
        ErrorMessage = errorMessage;
        PreconditionFailed = preconditionFailed;
        SkipReason = skipReason;
        CropId = cropId;
        ItemId = itemId;
        ItemName = itemName;
        Stack = stack;
        Quality = quality;
        StaminaCost = staminaCost;
        WaterCost = waterCost;
    }

    public static HarvestTileResult Succeeded(
        string? cropId, string? itemId, string? itemName, int stack, int quality,
        float staminaCost = 0f, int waterCost = 0) =>
        new(true, null, false, null, cropId, itemId, itemName, stack, quality, staminaCost, waterCost);

    public static HarvestTileResult PreconditionError(string reason, string? skipReason = null) =>
        new(false, reason, true, skipReason, null, null, null, 0, 0, 0f, 0);

    public static HarvestTileResult Failed(string reason) =>
        new(false, reason, false, null, null, null, null, 0, 0, 0f, 0);
}

/// <summary>
/// Adapter contract for executing a single-tile hand harvest through normal game mechanics.
/// </summary>
public interface IHarvestAdapter
{
    /// <summary>
    /// Harvests a mature crop on the target tile into the companion's inventory.
    /// Enforces:
    /// - Current-map and main-thread execution.
    /// - Maturity judged only by HoeDirt.readyForHarvest() (via observer state).
    /// - Scythe-harvest crops are rejected (skip reason "scythe-required").
    /// - Conservative capacity precheck: no empty inventory slot -> skip "inventory-full".
    /// - Game1.player is swapped to the companion farmer only for the duration of
    ///   crop.harvest and restored in a finally block with reference verification.
    /// - Human player resources are verified unchanged after the operation.
    /// </summary>
    HarvestTileResult HarvestTile(
        IFarmerActor actor,
        string locationName,
        TileCoordinate targetTile);
}
