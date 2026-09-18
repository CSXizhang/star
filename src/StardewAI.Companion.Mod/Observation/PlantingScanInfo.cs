using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Observation;

public sealed record SeedItemInfo(
    string ItemId,
    string Name,
    int Stack,
    bool CanPlantCurrentSeason,
    IReadOnlyList<string> Seasons,
    int? GrowthDays = null,
    bool? Regrows = null,
    bool? IsRaised = null
);

public sealed record PlantingScanInfo(
    string Season,
    int DayOfMonth,
    bool CompanionHasHoe,
    IReadOnlyList<SeedItemInfo> Seeds,
    IReadOnlyList<TileCoordinate> TilledEmptyTiles,
    int TotalTilledEmptyCount,
    bool TilledEmptyTruncated,
    IReadOnlyList<TileCoordinate> TillableTiles,
    int TotalTillableCount,
    bool TillableTruncated,
    TileCoordinate SearchCenter,
    int SearchRadius
);
