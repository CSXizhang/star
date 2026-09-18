using System.Diagnostics.CodeAnalysis;
using StardewValley;

namespace StardewAI.Companion.Mod.Observation;

/// <summary>
/// Normalizes item identifiers to crop data IDs (unqualified LocalItemId).
/// Game crop APIs (Crop.ResolveSeedId, Crop.TryGetData, HoeDirt.canPlantThisSeedHere, HoeDirt.plant)
/// require unqualified LocalItemIds (e.g. "472"), whereas external APIs and inventory snapshots
/// preserve qualified IDs (e.g. "(O)472").
/// </summary>
public static class SeedIdNormalizer
{
    /// <summary>
    /// Converts a qualified or unqualified itemId into an unqualified LocalItemId suitable for crop data lookup and planting.
    /// Falls back to the original string if metadata is unavailable or input is null/empty.
    /// </summary>
    [return: NotNullIfNotNull("itemId")]
    public static string? ToCropDataId(string? itemId)
    {
        if (string.IsNullOrWhiteSpace(itemId))
        {
            return itemId;
        }

        try
        {
            var metadata = ItemRegistry.GetMetadata(itemId);
            var localId = metadata?.LocalItemId;
            return !string.IsNullOrEmpty(localId) ? localId : itemId;
        }
        catch
        {
            return itemId;
        }
    }
}
