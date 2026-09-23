using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Observation;

/// <summary>
/// One choppable native target found by a read-only scan: a wild tree terrain
/// feature, a giant stump or a hollow log resource clump. Fruit trees are
/// deliberately excluded (they are long-term player investments).
/// </summary>
public sealed record ChoppableTreeScanInfo(
    TileCoordinate Tile,
    string Kind,
    int GrowthStage,
    bool Tapped,
    int Width = 1,
    int Height = 1
);
