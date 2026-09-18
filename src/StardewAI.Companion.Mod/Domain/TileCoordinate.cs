namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// A 2D integer tile coordinate on a game map.
/// </summary>
public readonly record struct TileCoordinate(int X, int Y)
{
    public int ManhattanDistanceTo(TileCoordinate other) =>
        Math.Abs(X - other.X) + Math.Abs(Y - other.Y);

    public bool IsAdjacentTo(TileCoordinate other) =>
        ManhattanDistanceTo(other) == 1;

    public IEnumerable<TileCoordinate> CardinalNeighbors()
    {
        yield return new TileCoordinate(X, Y - 1); // North (0)
        yield return new TileCoordinate(X + 1, Y); // East (1)
        yield return new TileCoordinate(X, Y + 1); // South (2)
        yield return new TileCoordinate(X - 1, Y); // West (3)
    }

    public override string ToString() => $"({X},{Y})";
}
