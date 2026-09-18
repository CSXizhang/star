using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Observation;

/// <summary>
/// Read-only observation of a ground item the companion could act on:
/// harvest debris, a spawned/forage object, or a weed.
/// </summary>
public sealed record GroundItemScanInfo(
    TileCoordinate Tile,
    string Kind,
    string ItemId,
    string Name,
    int Stack,
    bool IsDropped,
    bool IsWeed,
    bool CanBeGrabbed,
    string? ClearTool,
    /// <summary>True for a native stone obstacle (cleared with the native Pickaxe).</summary>
    bool IsStone = false,
    /// <summary>True for a native twig obstacle (cleared with the native Axe).</summary>
    bool IsTwig = false
);
