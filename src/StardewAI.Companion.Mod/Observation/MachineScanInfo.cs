using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Observation;

/// <summary>
/// Read-only observation of a placed machine (an Object whose native MachineData
/// exists), including the authoritative in-game processing state.
/// </summary>
public sealed record MachineScanInfo(
    TileCoordinate Tile,
    string ItemId,
    string Name,
    bool IsMachine,
    bool IsReady,
    int MinutesUntilReady,
    int? MinutesUntilReadyRemaining,
    string? OutputItemId,
    string? OutputName,
    int OutputStack,
    int OutputQuality,
    string? LastInputItemId,
    bool HasInput,
    string LocationName
);
