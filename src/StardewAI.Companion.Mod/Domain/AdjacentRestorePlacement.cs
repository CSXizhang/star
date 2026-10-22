namespace StardewAI.Companion.Mod.Domain;

/// <summary>One-cell recovery for a verified ordinary, collectible native object.</summary>
public static class AdjacentRestorePlacement
{
    public static TileCoordinate? Find(TileCoordinate origin, bool collectibleOrdinaryObject,
        bool originOtherwiseLegal, Func<TileCoordinate, bool> eligible)
    {
        if (!collectibleOrdinaryObject || !originOtherwiseLegal) return null;
        foreach (var tile in new[] { new TileCoordinate(origin.X, origin.Y + 1),
                     new TileCoordinate(origin.X + 1, origin.Y), new TileCoordinate(origin.X, origin.Y - 1),
                     new TileCoordinate(origin.X - 1, origin.Y) })
            if (eligible(tile)) return tile;
        return null;
    }
}
