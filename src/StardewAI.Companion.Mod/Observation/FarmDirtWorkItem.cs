using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Observation;

/// <summary>
/// Structured observation of a hoe-dirt tile on the farm.
/// </summary>
public sealed record FarmDirtWorkItem(
    TileCoordinate Tile,
    bool IsWatered,
    bool HasCrop,
    string? CropId,
    bool IsHarvestable
);
