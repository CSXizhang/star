namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// Character facing direction matching Stardew Valley conventions:
/// 0 = Up, 1 = Right, 2 = Down, 3 = Left.
/// </summary>
public enum FacingDirection
{
    Up = 0,
    Right = 1,
    Down = 2,
    Left = 3
}

public static class FacingDirectionExtensions
{
    public static FacingDirection? FromDelta(int dx, int dy)
    {
        if (dx == 0 && dy < 0) return FacingDirection.Up;
        if (dx > 0 && dy == 0) return FacingDirection.Right;
        if (dx == 0 && dy > 0) return FacingDirection.Down;
        if (dx < 0 && dy == 0) return FacingDirection.Left;
        return null;
    }

    public static FacingDirection? DirectionToAdjacent(TileCoordinate from, TileCoordinate to)
    {
        return FromDelta(to.X - from.X, to.Y - from.Y);
    }
}
