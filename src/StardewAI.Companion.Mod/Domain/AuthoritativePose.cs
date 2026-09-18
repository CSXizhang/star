namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// Authoritative physical pose owned by the Mechanics Actor.
/// </summary>
public sealed record AuthoritativePose
{
    public string LocationName { get; init; } = "";
    public TileCoordinate Tile { get; init; } = new(0, 0);
    public FacingDirection Facing { get; init; } = FacingDirection.Down;

    public AuthoritativePose() { }

    public AuthoritativePose(string locationName, TileCoordinate tile, FacingDirection facing)
    {
        LocationName = locationName;
        Tile = tile;
        Facing = facing;
    }

    public AuthoritativePose(string locationName, int x, int y, FacingDirection facing)
        : this(locationName, new TileCoordinate(x, y), facing) { }
}
