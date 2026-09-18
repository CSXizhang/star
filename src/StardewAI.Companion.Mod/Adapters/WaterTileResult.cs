namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Result of an atomic watering operation on a single tile.
/// </summary>
public sealed class WaterTileResult
{
    public bool Success { get; }
    public string? ErrorMessage { get; }
    public float StaminaCost { get; }
    public int WaterCost { get; }
    public bool PreconditionFailed { get; }

    private WaterTileResult(bool success, string? errorMessage, float staminaCost, int waterCost, bool preconditionFailed)
    {
        Success = success;
        ErrorMessage = errorMessage;
        StaminaCost = staminaCost;
        WaterCost = waterCost;
        PreconditionFailed = preconditionFailed;
    }

    public static WaterTileResult Succeeded(float staminaCost = 2.0f, int waterCost = 1) =>
        new(true, null, staminaCost, waterCost, false);

    public static WaterTileResult PreconditionError(string reason) =>
        new(false, reason, 0f, 0, true);

    public static WaterTileResult Failed(string reason, float staminaCost = 0f, int waterCost = 0) =>
        new(false, reason, staminaCost, waterCost, false);
}
