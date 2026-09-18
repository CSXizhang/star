using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Navigation;

public enum MapEdgeKind
{
    Warp,
    Door,
    LockedDoorWarp
}

/// <summary>
/// Represents a directed transition from a source map to a target map via a Warp or Door.
/// Contains operational metadata such as operating hours and friendship requirements.
/// </summary>
public sealed record MapEdge(
    string SourceLocation,
    string TargetLocation,
    TileCoordinate SourceTile,
    TileCoordinate TargetTile,
    MapEdgeKind EdgeKind,
    int? OpenTime = null,
    int? CloseTime = null,
    string? NpcName = null,
    int? MinFriendship = null
);
