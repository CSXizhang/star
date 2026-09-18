namespace StardewAI.Companion.Mod.Observation;

/// <summary>
/// Immutable snapshot of the human player's resources to verify
/// the companion actor never drains or modifies player stamina, water, or inventory.
/// </summary>
public sealed record PlayerResourceSnapshot
{
    public float Stamina { get; init; }
    public int WaterLeft { get; init; }
    public int ItemCount { get; init; }

    public PlayerResourceSnapshot(float stamina, int waterLeft, int itemCount)
    {
        Stamina = stamina;
        WaterLeft = waterLeft;
        ItemCount = itemCount;
    }

    public bool EqualsPlayerResources(PlayerResourceSnapshot other, float staminaTolerance = 0.001f)
    {
        return Math.Abs(Stamina - other.Stamina) <= staminaTolerance
            && WaterLeft == other.WaterLeft
            && ItemCount == other.ItemCount;
    }
}
