using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Observation;

/// <summary>
/// Read-only observation of one non-empty slot inside a chest.
/// </summary>
public sealed record ChestSlotInfo(
    int Slot,
    string ItemId,
    string Name,
    int Stack,
    int Quality,
    string? DisplayName = null,
    bool IsSeed = false,
    IReadOnlyList<string>? Seasons = null
);

/// <summary>
/// Read-only observation of a normal (non-fridge, non-special) chest on a map.
/// </summary>
public sealed record ChestScanInfo(
    TileCoordinate Tile,
    int Capacity,
    int FreeSlots,
    IReadOnlyList<ChestSlotInfo> Contents
);
