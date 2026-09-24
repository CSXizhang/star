using Microsoft.Xna.Framework;
using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Observation;

/// <summary>
/// Authoritative metadata for an OpenShop action parsed from map tile properties.
/// </summary>
public sealed record ShopActionMetadata(
    string? LocationName,
    Rectangle? OwnerArea,
    int OpenTime,
    int CloseTime,
    bool ActionFound,
    IReadOnlyList<TileCoordinate> CounterTiles,
    TileCoordinate? InteractionTile = null
);

/// <summary>
/// Read-only observer interface for game world inspection without side-effects.
/// </summary>
public interface IWorldObserver
{
    /// <summary>
    /// Monotonically increasing revision counter incremented when observable world changes.
    /// </summary>
    int WorldRevision { get; }

    /// <summary>Increments the world revision after a state-changing execution completes.</summary>
    void BumpRevision();

    /// <summary>
    /// Current map / location name where execution is hosted.
    /// </summary>
    string? CurrentLocationName { get; }

    /// <summary>
    /// Checks if execution is currently on the game's main thread.
    /// </summary>
    bool IsMainThread { get; }

    /// <summary>
    /// Checks whether a named location is loaded and can host companion work. The
    /// companion works on its own logical map, which may differ from the player's
    /// active map (<see cref="CurrentLocationName"/>).
    /// </summary>
    bool LocationExists(string locationName);

    /// <summary>
    /// Evaluates if a given tile is passable without collision.
    /// </summary>
    bool IsTilePassable(string locationName, TileCoordinate tile);

    /// <summary>
    /// Checks whether a tile on the given location is a door, warp, or transition trigger tile.
    /// </summary>
    bool IsWarpOrDoorTile(string locationName, TileCoordinate tile);

    /// <summary>
    /// Reads soil / hoe-dirt state at the specified tile.
    /// </summary>
    TileDirtState GetDirtState(string locationName, TileCoordinate tile);

    /// <summary>
    /// Scans all hoe-dirt terrain features at the specified location and returns their work states.
    /// </summary>
    IReadOnlyList<FarmDirtWorkItem> ScanFarmWork(string locationName);

    /// <summary>
    /// Scans all normal chests (excludes fridge and special chest types such as
    /// junimo chests and mini shipping bins) at the specified location,
    /// sorted by tile Y then X.
    /// </summary>
    IReadOnlyList<ChestScanInfo> ScanChests(string locationName);

    /// <summary>
    /// Returns the normal chest at the specified tile, or null when no normal chest exists there.
    /// </summary>
    ChestScanInfo? GetChestAt(string locationName, TileCoordinate tile);

    /// <summary>
    /// Captures a snapshot of the human player's resources.
    /// </summary>
    PlayerResourceSnapshot GetPlayerSnapshot();

    /// <summary>
    /// Checks whether the human player is on the specified map.
    /// </summary>
    bool IsPlayerOnSameMap(string locationName);

    /// <summary>
    /// In-game clock time of day (e.g. 600 for 6:00 AM, 1200 for 12:00 PM).
    /// </summary>
    int TimeOfDay { get; }

    /// <summary>
    /// Current season ("spring", "summer", "fall", "winter").
    /// </summary>
    string CurrentSeason { get; }

    /// <summary>
    /// Current day of month (1-28).
    /// </summary>
    int DayOfMonth { get; }

    /// <summary>
    /// Whether it is currently raining.
    /// </summary>
    bool IsRaining { get; }

    /// <summary>
    /// Native weather icon id as a string, or null when it cannot be read.
    /// Snow/storm days are not rain, so the compact context must read the actual
    /// weather rather than inferring "clear" from <see cref="IsRaining"/>.
    /// </summary>
    string? WeatherIcon { get; }

    /// <summary>
    /// Checks whether the human player is occupying the specified tile.
    /// </summary>
    bool IsPlayerOnTile(string locationName, TileCoordinate tile);

    /// <summary>
    /// Scans planting options within the given radius around the center tile:
    /// tilled empty soil (ready to plant), tillable clear ground (ready to hoe),
    /// and companion seeds with seasonal compatibility.
    /// </summary>
    PlantingScanInfo ScanPlantingOptions(string locationName, TileCoordinate center, int radius, IFarmerActor actor);

    /// <summary>
    /// Scans real-time stock, pricing, operating status, and available companion funds for the given shop.
    /// Read-only inspection; unknown shops report missing/unknown status.
    /// </summary>
    ShopScanInfo ScanShop(string shopId, IFarmerActor actor);

    /// <summary>
    /// Gets physical counter tile coordinates for the specified shop.
    /// Dynamic discovery with location fallback and metadata caching.
    /// </summary>
    IReadOnlyList<TileCoordinate> GetShopCounterTiles(string shopId, string? preferredLocation = null);

    /// <summary>
    /// Retrieves authoritative shop action metadata (location, counter tiles, owner area, operating hours)
    /// for the specified shop.
    /// </summary>
    ShopActionMetadata GetShopActionMetadata(string shopId, string? preferredLocation = null);

    /// <summary>
    /// Scans placed machines (Objects with native MachineData) at the specified location:
    /// idle/processing/ready output state. Read-only.
    /// </summary>
    IReadOnlyList<MachineScanInfo> ScanMachines(string locationName, int maxMachines = 64);

    /// <summary>
    /// Scans livestock: animal buildings (coop/barn) with their feed situation, plus
    /// animals roaming the queried map. Read-only; native FarmAnimal/AnimalHouse facts only.
    /// </summary>
    LivestockScanInfo ScanLivestock(string locationName, int maxBuildings = 12, int maxAnimals = 60);

    /// <summary>
    /// Finds tiles where the native game allows refilling a watering can
    /// (<c>GameLocation.CanRefillWateringCanOnTile</c>) within radius of the center.
    /// </summary>
    IReadOnlyList<TileCoordinate> FindWaterRefillTiles(
        string locationName,
        TileCoordinate center,
        int radius,
        int maxTiles = 16);

    /// <summary>
    /// True when the given tilled tile already carries native fertilizer.
    /// </summary>
    bool HasFertilizer(string locationName, TileCoordinate tile);

    /// <summary>
    /// Scans ground items (harvest debris, spawned/forage objects, weeds) within radius
    /// so the model can choose explicit tiles instead of guessing.
    /// </summary>
    IReadOnlyList<GroundItemScanInfo> ScanGroundItems(
        string locationName,
        TileCoordinate center,
        int radius,
        int maxItems = 64);

    /// <summary>
    /// Scans choppable native targets (wild trees, giant stumps, hollow logs) within
    /// radius so the model can choose explicit tiles. Fruit trees are never listed.
    /// </summary>
    IReadOnlyList<ChoppableTreeScanInfo> ScanChoppableTrees(
        string locationName,
        TileCoordinate center,
        int radius,
        int maxItems = 64);

    /// <summary>
    /// Computes elapsed in-game minutes between two time-of-day integer representations.
    /// </summary>
    public static int CalculateGameMinutesElapsed(int startClock, int currentClock)
    {
        if (currentClock <= startClock) return 0;
        int startH = startClock / 100;
        int startM = startClock % 100;
        int currH = currentClock / 100;
        int currM = currentClock % 100;
        return (currH - startH) * 60 + (currM - startM);
    }
}


