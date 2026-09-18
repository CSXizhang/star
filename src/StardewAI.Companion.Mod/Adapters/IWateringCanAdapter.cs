using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Adapter contract for executing a single tile watering action through normal game mechanics.
/// </summary>
public interface IWateringCanAdapter
{
    /// <summary>
    /// Executes watering of a target tile using the Mechanics Actor's independent Farmer and WateringCan.
    /// Enforces:
    /// - Current-map and main-thread execution.
    /// - Prechecking empty can before game tool invocation.
    /// - Human player resources untouched.
    /// - No swapping Game1.player.
    /// - No direct soil-state mutation (executes through WateringCan.DoFunction).
    /// </summary>
    WaterTileResult WaterTile(
        IFarmerActor actor,
        string locationName,
        TileCoordinate targetTile);
}
