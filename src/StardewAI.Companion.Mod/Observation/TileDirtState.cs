namespace StardewAI.Companion.Mod.Observation;

/// <summary>
/// Read-only observation of soil and crop status on a tile.
/// </summary>
public sealed record TileDirtState
{
    public bool IsTilled { get; init; }
    public bool IsWatered { get; init; }
    public bool HasCrop { get; init; }
    public string? CropId { get; init; }
    public bool IsHarvestable { get; init; }

    /// <summary>
    /// True when the crop on this tile requires a scythe to harvest
    /// (crop.GetHarvestMethod() == HarvestMethod.Scythe). Hand-harvest skills skip these.
    /// </summary>
    public bool RequiresScythe { get; init; }

    public static TileDirtState NotTilled => new() { IsTilled = false, IsWatered = false, HasCrop = false };
    public static TileDirtState DryDirt(bool hasCrop = false, string? cropId = null) =>
        new() { IsTilled = true, IsWatered = false, HasCrop = hasCrop, CropId = cropId };
    public static TileDirtState WateredDirt(bool hasCrop = false, string? cropId = null) =>
        new() { IsTilled = true, IsWatered = true, HasCrop = hasCrop, CropId = cropId };
}
