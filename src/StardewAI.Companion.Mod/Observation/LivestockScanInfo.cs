using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Observation;

/// <summary>
/// Read-only observation of one farm animal (native FarmAnimal facts only).
/// </summary>
public sealed record AnimalScanInfo(
    string Name,
    string AnimalType,
    string DisplayType,
    int Age,
    int Happiness,
    int Fullness,
    int Friendship,
    bool ProduceReady,
    string? CurrentProduceId,
    string? HarvestType,
    string? RequiredTool,
    string? BuildingType,
    string LocationName,
    TileCoordinate Tile,
    bool WasPetToday,
    bool WasAutoPetToday
);

/// <summary>
/// Read-only observation of one animal building (coop/barn) and its feed situation.
/// </summary>
public sealed record AnimalBuildingScanInfo(
    string BuildingType,
    string IndoorsName,
    string LocationName,
    TileCoordinate Tile,
    TileCoordinate DoorTile,
    bool AnimalDoorOpen,
    bool DoorStateKnown,
    int AnimalCount,
    int AnimalLimit,
    int HayCount,
    int HayCapacity,
    int SiloHayCount,
    IReadOnlyList<AnimalScanInfo> Animals
);

/// <summary>
/// Livestock observation result: buildings plus animals roaming the queried map.
/// </summary>
public sealed record LivestockScanInfo(
    IReadOnlyList<AnimalBuildingScanInfo> Buildings,
    IReadOnlyList<AnimalScanInfo> RoamingAnimals,
    bool BuildingsTruncated,
    bool AnimalsTruncated
);
