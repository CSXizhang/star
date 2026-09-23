using System.Collections.Concurrent;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.GameData.Shops;
using StardewValley.Internal;
using StardewValley.TerrainFeatures;
using StardewValley.TokenizableStrings;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Observation;

/// <summary>
/// Production read-only observer implementation integrating with Stardew Valley game APIs.
/// Does not write or mutate any game state.
/// </summary>
public sealed class GameWorldObserver : IWorldObserver
{
    private readonly IMonitor _monitor;
    private readonly int _mainThreadId;
    private int _worldRevision = 1;

    public int WorldRevision => _worldRevision;

    public string? CurrentLocationName =>
        !string.IsNullOrWhiteSpace(Game1.currentLocation?.NameOrUniqueName)
            ? Game1.currentLocation.NameOrUniqueName
            : Game1.currentLocation?.Name;

    public bool IsMainThread => Environment.CurrentManagedThreadId == _mainThreadId;

    public GameWorldObserver(IMonitor monitor)
    {
        _monitor = monitor ?? throw new ArgumentNullException(nameof(monitor));
        _mainThreadId = Environment.CurrentManagedThreadId;
    }

    public void BumpRevision()
    {
        Interlocked.Increment(ref _worldRevision);
    }

    public bool IsTilePassable(string locationName, TileCoordinate tile)
    {
        var loc = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
        if (loc is null)
            return false;

        var v = new Vector2(tile.X, tile.Y);

        // Check if there is an obstructing building or solid object
        if (loc.isObjectAtTile(tile.X, tile.Y))
        {
            var obj = loc.getObjectAtTile(tile.X, tile.Y);
            if (obj != null && !obj.isPassable())
                return false;
        }

        // Check terrain features (e.g. trees, bushes)
        if (loc.terrainFeatures.TryGetValue(v, out var tf) && !tf.isPassable())
        {
            return false;
        }

        // Check map tile impassability
        var tileLoc = new xTile.Dimensions.Location(tile.X, tile.Y);
        return loc.isTilePassable(tileLoc, Game1.viewport);
    }

    public bool IsWarpOrDoorTile(string locationName, TileCoordinate tile)
    {
        var loc = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
        if (loc is null)
            return false;

        var pt = new Point(tile.X, tile.Y);

        if (loc.warps != null)
        {
            foreach (var w in loc.warps)
            {
                if (w != null && w.X == tile.X && w.Y == tile.Y)
                {
                    try
                    {
                        if (w.npcOnly.Value) continue;
                    }
                    catch { }
                    return true;
                }
            }
        }

        if (loc.doors != null)
        {
            try
            {
                if (loc.doors.ContainsKey(pt))
                    return true;
            }
            catch { }
            try
            {
                if (loc.getWarpFromDoor(pt, null) != null)
                    return true;
            }
            catch { }
        }

        if (loc.buildings != null)
        {
            foreach (var b in loc.buildings)
            {
                if (b == null) continue;
                try
                {
                    if (b.daysOfConstructionLeft.Value > 0) continue;
                }
                catch { }

                Point doorPt;
                try
                {
                    doorPt = b.getPointForHumanDoor();
                }
                catch
                {
                    doorPt = new Point(
                        (b.tileX?.Value ?? 0) + (b.humanDoor?.X ?? 0),
                        (b.tileY?.Value ?? 0) + (b.humanDoor?.Y ?? 0)
                    );
                }

                if (doorPt == pt)
                    return true;
            }
        }

        string[] layers = { "Back", "Buildings" };
        foreach (var layer in layers)
        {
            try
            {
                string? touchAction = loc.doesTileHaveProperty(tile.X, tile.Y, "TouchAction", layer);
                if (!string.IsNullOrWhiteSpace(touchAction))
                {
                    var actionName = touchAction.Split(' ', StringSplitOptions.RemoveEmptyEntries)[0];
                    if (actionName.Equals("Warp", StringComparison.OrdinalIgnoreCase) ||
                        actionName.Equals("LockedDoorWarp", StringComparison.OrdinalIgnoreCase) ||
                        actionName.Equals("Door", StringComparison.OrdinalIgnoreCase) ||
                        actionName.Equals("MagicWarp", StringComparison.OrdinalIgnoreCase))
                    {
                        return true;
                    }
                }
            }
            catch { }
        }

        foreach (var layer in layers)
        {
            try
            {
                string? action = loc.doesTileHaveProperty(tile.X, tile.Y, "Action", layer);
                if (!string.IsNullOrWhiteSpace(action))
                {
                    var actionName = action.Split(' ', StringSplitOptions.RemoveEmptyEntries)[0];
                    if (actionName.Equals("Warp", StringComparison.OrdinalIgnoreCase) ||
                        actionName.Equals("LockedDoorWarp", StringComparison.OrdinalIgnoreCase) ||
                        actionName.Equals("Door", StringComparison.OrdinalIgnoreCase) ||
                        actionName.Equals("EnterSewer", StringComparison.OrdinalIgnoreCase) ||
                        actionName.Equals("WarpGreenhouse", StringComparison.OrdinalIgnoreCase))
                    {
                        return true;
                    }
                }
            }
            catch { }
        }

        try
        {
            var rect = new Rectangle(tile.X * 64, tile.Y * 64, 64, 64);
            if (loc.isCollidingWithWarpOrDoor(rect, null) != null)
            {
                return true;
            }
        }
        catch { }

        return false;
    }

    public TileDirtState GetDirtState(string locationName, TileCoordinate tile)
    {
        var loc = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
        if (loc is null)
            return TileDirtState.NotTilled;

        var v = new Vector2(tile.X, tile.Y);
        if (loc.terrainFeatures.TryGetValue(v, out var tf) && tf is HoeDirt dirt)
        {
            bool isWatered = dirt.isWatered();
            bool hasCrop = dirt.crop != null;
            string? cropId = dirt.crop?.indexOfHarvest?.Value;
            bool isHarvestable = dirt.crop != null && dirt.readyForHarvest();
            bool requiresScythe = dirt.crop != null &&
                dirt.crop.GetHarvestMethod() == StardewValley.GameData.Crops.HarvestMethod.Scythe;
            return new TileDirtState
            {
                IsTilled = true,
                IsWatered = isWatered,
                HasCrop = hasCrop,
                CropId = cropId,
                IsHarvestable = isHarvestable,
                RequiresScythe = requiresScythe
            };
        }

        return TileDirtState.NotTilled;
    }

    public IReadOnlyList<FarmDirtWorkItem> ScanFarmWork(string locationName)
    {
        var loc = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
        if (loc is null)
            return Array.Empty<FarmDirtWorkItem>();

        var items = new List<FarmDirtWorkItem>();
        try
        {
            foreach (var pair in loc.terrainFeatures.Pairs)
            {
                if (pair.Value is HoeDirt dirt)
                {
                    var tile = new TileCoordinate((int)pair.Key.X, (int)pair.Key.Y);
                    bool isWatered = dirt.isWatered();
                    bool hasCrop = dirt.crop != null;
                    string? cropId = dirt.crop?.indexOfHarvest?.Value;
                    bool isHarvestable = dirt.crop != null && dirt.readyForHarvest();

                    items.Add(new FarmDirtWorkItem(
                        Tile: tile,
                        IsWatered: isWatered,
                        HasCrop: hasCrop,
                        CropId: cropId,
                        IsHarvestable: isHarvestable
                    ));
                }
            }
        }
        catch (Exception ex)
        {
            _monitor.Log($"Error scanning farm work at '{locationName}': {ex.Message}", LogLevel.Warn);
            return Array.Empty<FarmDirtWorkItem>();
        }

        return items.OrderBy(i => i.Tile.Y).ThenBy(i => i.Tile.X).ToList();
    }

    public IReadOnlyList<ChestScanInfo> ScanChests(string locationName)
    {
        var loc = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
        if (loc is null)
            return Array.Empty<ChestScanInfo>();

        var chests = new List<ChestScanInfo>();
        try
        {
            foreach (var pair in loc.objects.Pairs)
            {
                if (pair.Value is StardewValley.Objects.Chest chest && IsNormalChest(chest))
                {
                    chests.Add(ScanChest(new TileCoordinate((int)pair.Key.X, (int)pair.Key.Y), chest, loc));
                }
            }
        }
        catch (Exception ex)
        {
            _monitor.Log($"Error scanning chests at '{locationName}': {ex.Message}", LogLevel.Warn);
            return Array.Empty<ChestScanInfo>();
        }

        return chests.OrderBy(c => c.Tile.Y).ThenBy(c => c.Tile.X).ToList();
    }

    public ChestScanInfo? GetChestAt(string locationName, TileCoordinate tile)
    {
        var loc = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
        if (loc is null)
            return null;

        try
        {
            var v = new Vector2(tile.X, tile.Y);
            if (loc.objects.TryGetValue(v, out var obj) && obj is StardewValley.Objects.Chest chest && IsNormalChest(chest))
            {
                return ScanChest(tile, chest, loc);
            }
        }
        catch (Exception ex)
        {
            _monitor.Log($"Error reading chest at '{locationName}' {tile}: {ex.Message}", LogLevel.Warn);
        }

        return null;
    }

    /// <summary>
    /// Only normal chests are workable by the companion: the farmhouse fridge and
    /// special chest types (junimo chests, mini shipping bins, auto loaders, etc.) are excluded.
    /// </summary>
    private static bool IsNormalChest(StardewValley.Objects.Chest chest)
    {
        return !chest.fridge.Value &&
            chest.SpecialChestType == StardewValley.Objects.Chest.SpecialChestTypes.None;
    }

    private static ChestScanInfo ScanChest(TileCoordinate tile, StardewValley.Objects.Chest chest, GameLocation? loc = null)
    {
        var contents = new List<ChestSlotInfo>();
        var items = chest.Items;
        for (int slot = 0; slot < items.Count; slot++)
        {
            var item = items[slot];
            if (item is null)
                continue;

            string itemId = item.QualifiedItemId ?? item.ItemId ?? item.Name ?? "";
            string dispName = item.DisplayName ?? item.Name ?? "";
            bool isSeed = false;
            List<string>? seasons = null;

            try
            {
                string? rawCropId = SeedIdNormalizer.ToCropDataId(item.ItemId);
                string cropDataId = !string.IsNullOrEmpty(rawCropId) ? rawCropId : (item.ItemId ?? "");
                string? resolvedSeedId = Crop.ResolveSeedId(cropDataId, loc);
                string seedId = !string.IsNullOrEmpty(resolvedSeedId) ? resolvedSeedId : cropDataId;
                if (!string.IsNullOrEmpty(seedId) && Crop.TryGetData(seedId, out var cropData) && cropData != null)
                {
                    isSeed = true;
                    seasons = cropData.Seasons.Select(s => s.ToString().ToLowerInvariant()).ToList();
                }
            }
            catch
            {
                // Fallback gracefully if crop metadata is unavailable
            }

            contents.Add(new ChestSlotInfo(
                Slot: slot,
                ItemId: itemId,
                Name: item.Name ?? "",
                Stack: item.Stack,
                Quality: item.Quality,
                DisplayName: dispName,
                IsSeed: isSeed,
                Seasons: seasons
            ));
        }

        int capacity = chest.GetActualCapacity();
        return new ChestScanInfo(
            Tile: tile,
            Capacity: capacity,
            FreeSlots: Math.Max(0, capacity - items.Count(i => i is not null)),
            Contents: contents
        );
    }

    public PlayerResourceSnapshot GetPlayerSnapshot()
    {
        if (Game1.player is null)
            return new PlayerResourceSnapshot(0f, 0, 0);

        float stamina = Game1.player.Stamina;
        int water = (Game1.player.CurrentTool as WateringCan)?.WaterLeft ?? 0;
        int itemCount = Game1.player.Items?.Count(i => i != null) ?? 0;
        return new PlayerResourceSnapshot(stamina, water, itemCount);
    }

    public bool IsPlayerOnSameMap(string locationName)
    {
        if (Game1.player?.currentLocation is null)
            return false;
        var pLoc = Game1.player.currentLocation;
        string? pName = !string.IsNullOrWhiteSpace(pLoc.NameOrUniqueName) ? pLoc.NameOrUniqueName : pLoc.Name;
        return string.Equals(pName, locationName, StringComparison.OrdinalIgnoreCase);
    }

    public int TimeOfDay
    {
        get
        {
            try
            {
                return Game1.timeOfDay;
            }
            catch
            {
                return 600;
            }
        }
    }

    public string CurrentSeason
    {
        get
        {
            try
            {
                return Game1.currentSeason ?? "spring";
            }
            catch
            {
                return "spring";
            }
        }
    }

    public int DayOfMonth
    {
        get
        {
            try
            {
                return Game1.dayOfMonth;
            }
            catch
            {
                return 1;
            }
        }
    }

    public bool IsRaining
    {
        get
        {
            try
            {
                return Game1.isRaining;
            }
            catch
            {
                return false;
            }
        }
    }

    /// <summary>
    /// Native weather icon id as a string (Stardew's weather icon), or null when
    /// it cannot be read. The compact context must not infer "clear" from
    /// isRaining=false: snow and storms are not rain.
    /// </summary>
    public string? WeatherIcon
    {
        get
        {
            try
            {
                return ((int)Game1.weatherIcon).ToString();
            }
            catch
            {
                return null;
            }
        }
    }

    public bool IsPlayerOnTile(string locationName, TileCoordinate tile)
    {
        try
        {
            if (Game1.player?.currentLocation is null)
                return false;
            var pLoc = Game1.player.currentLocation;
            string? pName = !string.IsNullOrWhiteSpace(pLoc.NameOrUniqueName) ? pLoc.NameOrUniqueName : pLoc.Name;
            if (!string.Equals(pName, locationName, StringComparison.OrdinalIgnoreCase))
                return false;
            return (int)Game1.player.Tile.X == tile.X && (int)Game1.player.Tile.Y == tile.Y;
        }
        catch
        {
            return false;
        }
    }

    public PlantingScanInfo ScanPlantingOptions(string locationName, TileCoordinate center, int radius, IFarmerActor actor)
    {
        var loc = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
        string season = CurrentSeason;
        int day = DayOfMonth;
        bool hasHoe = actor.Hoe != null;

        if (loc is null)
        {
            return new PlantingScanInfo(
                Season: season,
                DayOfMonth: day,
                CompanionHasHoe: hasHoe,
                Seeds: Array.Empty<SeedItemInfo>(),
                TilledEmptyTiles: Array.Empty<TileCoordinate>(),
                TotalTilledEmptyCount: 0,
                TilledEmptyTruncated: false,
                TillableTiles: Array.Empty<TileCoordinate>(),
                TotalTillableCount: 0,
                TillableTruncated: false,
                SearchCenter: center,
                SearchRadius: radius
            );
        }

        // 1. Scan companion seeds from inventory snapshot
        var seedList = new List<SeedItemInfo>();
        var seenSeedIds = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var inv in actor.GetInventorySnapshot())
        {
            if (string.IsNullOrEmpty(inv.ItemId) || inv.IsTool) continue;
            string cropDataId = SeedIdNormalizer.ToCropDataId(inv.ItemId);
            string seedId = Crop.ResolveSeedId(cropDataId, loc);
            if (string.IsNullOrEmpty(seedId)) seedId = cropDataId;

            if (Crop.TryGetData(seedId, out var cropData) && cropData != null)
            {
                string key = inv.ItemId;
                if (!seenSeedIds.Add(key))
                {
                    var idx = seedList.FindIndex(s => string.Equals(s.ItemId, key, StringComparison.OrdinalIgnoreCase));
                    if (idx >= 0)
                    {
                        var prev = seedList[idx];
                        seedList[idx] = prev with { Stack = prev.Stack + inv.Stack };
                    }
                    continue;
                }

                bool canPlant = cropData.Seasons.Contains(loc.GetSeason()) || !loc.IsOutdoors || loc.SeedsIgnoreSeasonsHere();
                var seasons = cropData.Seasons.Select(s => s.ToString().ToLowerInvariant()).ToList();
                int? growthDays = cropData.DaysInPhase != null && cropData.DaysInPhase.Count > 0 ? cropData.DaysInPhase.Sum() : null;
                bool? regrows = cropData.RegrowDays > 0;
                bool? isRaised = cropData.IsRaised;

                seedList.Add(new SeedItemInfo(
                    ItemId: inv.ItemId,
                    Name: inv.Name,
                    Stack: inv.Stack,
                    CanPlantCurrentSeason: canPlant,
                    Seasons: seasons,
                    GrowthDays: growthDays,
                    Regrows: regrows,
                    IsRaised: isRaised
                ));
            }
        }

        // 2. Scan bounded tiles around center
        radius = Math.Clamp(radius, 1, 30);
        int minX = Math.Max(0, center.X - radius);
        int maxX = center.X + radius;
        int minY = Math.Max(0, center.Y - radius);
        int maxY = center.Y + radius;

        var allTilledEmpty = new List<TileCoordinate>();
        var allTillable = new List<TileCoordinate>();

        for (int y = minY; y <= maxY; y++)
        {
            for (int x = minX; x <= maxX; x++)
            {
                var tile = new TileCoordinate(x, y);
                var v = new Vector2(x, y);

                if (loc.terrainFeatures.TryGetValue(v, out var tf))
                {
                    if (tf is HoeDirt dirt && dirt.crop is null)
                    {
                        if (!loc.isObjectAtTile(x, y) && !IsPlayerOnTile(locationName, tile))
                        {
                            allTilledEmpty.Add(tile);
                        }
                    }
                }
                else
                {
                    if (!loc.isObjectAtTile(x, y) &&
                        !IsPlayerOnTile(locationName, tile) &&
                        loc.doesTileHaveProperty(x, y, "Diggable", "Back") != null &&
                        loc.isTilePassable(new xTile.Dimensions.Location(x, y), Game1.viewport))
                    {
                        allTillable.Add(tile);
                    }
                }
            }
        }

        allTilledEmpty.Sort((a, b) => a.ManhattanDistanceTo(center).CompareTo(b.ManhattanDistanceTo(center)));
        allTillable.Sort((a, b) => a.ManhattanDistanceTo(center).CompareTo(b.ManhattanDistanceTo(center)));

        const int maxReported = 64;
        bool tilledTruncated = allTilledEmpty.Count > maxReported;
        var reportedTilled = tilledTruncated ? allTilledEmpty.Take(maxReported).ToList() : allTilledEmpty;

        bool tillableTruncated = allTillable.Count > maxReported;
        var reportedTillable = tillableTruncated ? allTillable.Take(maxReported).ToList() : allTillable;

        return new PlantingScanInfo(
            Season: season,
            DayOfMonth: day,
            CompanionHasHoe: hasHoe,
            Seeds: seedList,
            TilledEmptyTiles: reportedTilled,
            TotalTilledEmptyCount: allTilledEmpty.Count,
            TilledEmptyTruncated: tilledTruncated,
            TillableTiles: reportedTillable,
            TotalTillableCount: allTillable.Count,
            TillableTruncated: tillableTruncated,
            SearchCenter: center,
            SearchRadius: radius
        );
    }

    public Func<LocalizedContentManager, Dictionary<string, ShopData>>? ShopDataResolver { get; set; }
    public Func<string, ShopData, Dictionary<ISalable, ItemStockInformation>>? ShopStockResolver { get; set; }
    public Func<ShopData, IEnumerable<ShopOwnerData>>? ShopOwnersResolver { get; set; }
    public Func<Farmer, int?>? CompanionMoneyResolver { get; set; }

    public Func<string, GameLocation?>? ShopLocationResolver { get; set; }
    public Func<string, GameLocation?, ShopActionMetadata?>? ShopActionResolver { get; set; }
    public Func<string, GameLocation?, (Rectangle? OwnerArea, int OpenTime, int CloseTime, bool ActionFound)?>? OpenShopActionResolver { get; set; }
    public Func<string, GameLocation?, IReadOnlyList<TileCoordinate>?>? CounterTilesResolver { get; set; }
    public Func<GameLocation?, IEnumerable<ShopNpcInfo>?>? ShopNpcsResolver { get; set; }
    public Func<int>? TimeOfDayResolver { get; set; }

    private readonly ConcurrentDictionary<string, ShopActionMetadata> _shopActionCache = new(StringComparer.OrdinalIgnoreCase);

    /// <summary>
    /// Scans the specified map location's Buildings and Back layers for action tiles whose verb is 'Buy'
    /// (e.g. 'Action Buy General' in SeedShop at (4,18) and (5,18)).
    /// </summary>
    public static bool TryFindBuyActionCounters(
        GameLocation location,
        out IReadOnlyList<TileCoordinate> counterTiles)
    {
        var tiles = new List<TileCoordinate>();
        if (location?.Map?.Layers != null)
        {
            string[] actionLayers = { "Buildings", "Back" };
            foreach (string layerId in actionLayers)
            {
                var layer = location.Map.GetLayer(layerId);
                if (layer == null) continue;

                for (int y = 0; y < layer.LayerHeight; y++)
                {
                    for (int x = 0; x < layer.LayerWidth; x++)
                    {
                        string? actionStr = null;
                        try
                        {
                            actionStr = location.doesTileHaveProperty(x, y, "Action", layerId);
                        }
                        catch
                        {
                            actionStr = null;
                        }

                        if (string.IsNullOrEmpty(actionStr))
                        {
                            var tile = layer.Tiles[x, y];
                            if (tile?.Properties != null && tile.Properties.TryGetValue("Action", out var prop))
                            {
                                actionStr = prop?.ToString();
                            }
                        }

                        if (string.IsNullOrEmpty(actionStr))
                            continue;

                        string[] args = ArgUtility.SplitBySpaceQuoteAware(actionStr);
                        if (args.Length > 0 && string.Equals(args[0], "Buy", StringComparison.OrdinalIgnoreCase))
                        {
                            tiles.Add(new TileCoordinate(x, y));
                        }
                    }
                }
            }
        }

        counterTiles = tiles;
        return tiles.Count > 0;
    }

    /// <summary>
    /// Backward-compatibility helper for OpenShop action scanning if present on custom maps.
    /// </summary>
    public static bool TryFindOpenShopAction(
        GameLocation location,
        string shopId,
        out Rectangle? ownerArea,
        out int openTime,
        out int closeTime,
        out IReadOnlyList<TileCoordinate> counterTiles)
    {
        ownerArea = null;
        openTime = -1;
        closeTime = -1;
        var tiles = new List<TileCoordinate>();

        if (location?.Map?.Layers == null)
        {
            counterTiles = Array.Empty<TileCoordinate>();
            return false;
        }

        string[] actionLayers = { "Buildings", "Back" };
        foreach (string layerId in actionLayers)
        {
            var layer = location.Map.GetLayer(layerId);
            if (layer == null) continue;

            for (int y = 0; y < layer.LayerHeight; y++)
            {
                for (int x = 0; x < layer.LayerWidth; x++)
                {
                    string? actionStr = null;
                    try
                    {
                        actionStr = location.doesTileHaveProperty(x, y, "Action", layerId);
                    }
                    catch
                    {
                        actionStr = null;
                    }

                    if (string.IsNullOrEmpty(actionStr))
                    {
                        var tile = layer.Tiles[x, y];
                        if (tile?.Properties != null && tile.Properties.TryGetValue("Action", out var prop))
                        {
                            actionStr = prop?.ToString();
                        }
                    }

                    if (string.IsNullOrEmpty(actionStr))
                        continue;

                    if (!actionStr.StartsWith("OpenShop", StringComparison.OrdinalIgnoreCase))
                        continue;

                    string[] args = ArgUtility.SplitBySpaceQuoteAware(actionStr);
                    if (args.Length < 2 || !string.Equals(args[1], shopId, StringComparison.OrdinalIgnoreCase))
                        continue;

                    tiles.Add(new TileCoordinate(x, y));

                    if (ownerArea == null)
                    {
                        ArgUtility.TryGetOptionalInt(args, 3, out openTime, out _, -1, "int openTime");
                        ArgUtility.TryGetOptionalInt(args, 4, out closeTime, out _, -1, "int closeTime");
                        ArgUtility.TryGetOptionalInt(args, 5, out int shopAreaX, out _, -1, "int shopAreaX");
                        ArgUtility.TryGetOptionalInt(args, 6, out int shopAreaY, out _, -1, "int shopAreaY");
                        ArgUtility.TryGetOptionalInt(args, 7, out int shopAreaWidth, out _, -1, "int shopAreaWidth");
                        ArgUtility.TryGetOptionalInt(args, 8, out int shopAreaHeight, out _, -1, "int shopAreaHeight");

                        if (shopAreaX != -1 && shopAreaY != -1 && shopAreaWidth != -1 && shopAreaHeight != -1)
                        {
                            ownerArea = new Rectangle(shopAreaX, shopAreaY, shopAreaWidth, shopAreaHeight);
                        }
                    }
                }
            }
        }

        if (!ownerArea.HasValue && tiles.Count > 0)
        {
            ownerArea = new Rectangle(tiles[0].X, tiles[0].Y - 1, 1, 1);
        }

        counterTiles = tiles;
        return tiles.Count > 0;
    }

    public static bool TryFindOpenShopAction(
        GameLocation location,
        string shopId,
        out Rectangle? ownerArea,
        out int openTime,
        out int closeTime)
    {
        return TryFindOpenShopAction(location, shopId, out ownerArea, out openTime, out closeTime, out _);
    }

    private static GameLocation? SafeGetLocationFromName(string? name)
    {
        if (string.IsNullOrWhiteSpace(name))
            return null;
        try
        {
            if (Game1.game1 == null)
                return null;
            return Game1.getLocationFromName(name);
        }
        catch
        {
            return null;
        }
    }

    public ShopActionMetadata GetShopActionMetadata(string shopId, string? preferredLocation = null)
    {
        if (_shopActionCache.TryGetValue(shopId, out var cached) && cached.ActionFound)
        {
            return cached;
        }

        GameLocation? shopLocation = null;
        Rectangle? ownerArea = null;
        int openTime = -1;
        int closeTime = -1;
        bool actionFound = false;
        IReadOnlyList<TileCoordinate> counterTiles = Array.Empty<TileCoordinate>();

        if (ShopActionResolver != null)
        {
            if (ShopLocationResolver != null)
            {
                shopLocation = ShopLocationResolver(shopId);
            }
            else if (!string.IsNullOrWhiteSpace(preferredLocation))
            {
                shopLocation = SafeGetLocationFromName(preferredLocation);
            }
            var customMeta = ShopActionResolver(shopId, shopLocation);
            if (customMeta != null)
            {
                _shopActionCache[shopId] = customMeta;
                return customMeta;
            }
        }

        if (OpenShopActionResolver != null)
        {
            if (ShopLocationResolver != null)
            {
                shopLocation = ShopLocationResolver(shopId);
            }
            else if (!string.IsNullOrWhiteSpace(preferredLocation))
            {
                shopLocation = SafeGetLocationFromName(preferredLocation);
            }

            var actionRes = OpenShopActionResolver(shopId, shopLocation);
            if (actionRes.HasValue)
            {
                ownerArea = actionRes.Value.OwnerArea;
                openTime = actionRes.Value.OpenTime;
                closeTime = actionRes.Value.CloseTime;
                actionFound = actionRes.Value.ActionFound;

                if (CounterTilesResolver != null)
                {
                    counterTiles = CounterTilesResolver(shopId, shopLocation) ?? Array.Empty<TileCoordinate>();
                }
                else if (actionFound && ownerArea.HasValue)
                {
                    counterTiles = new[] { new TileCoordinate(ownerArea.Value.X, ownerArea.Value.Y + 1) };
                }
            }
        }
        else
        {
            try
            {
                // 1. Try preferredLocation if specified
                if (!string.IsNullOrWhiteSpace(preferredLocation))
                {
                    shopLocation = SafeGetLocationFromName(preferredLocation);
                    if (shopLocation != null)
                    {
                        actionFound = TryFindBuyActionCounters(shopLocation, out counterTiles);
                    }
                }

                // 2. Try ShopLocationResolver or shopId as location name (e.g. 'SeedShop')
                if (!actionFound)
                {
                    shopLocation = ShopLocationResolver != null
                        ? ShopLocationResolver(shopId)
                        : SafeGetLocationFromName(shopId);

                    if (shopLocation != null)
                    {
                        actionFound = TryFindBuyActionCounters(shopLocation, out counterTiles);
                    }
                }

                // 3. Fallback across all loaded game locations
                if (!actionFound && ShopLocationResolver == null && Game1.game1 != null && Game1.locations != null)
                {
                    foreach (var loc in Game1.locations)
                    {
                        if (loc?.Map == null) continue;
                        if (TryFindBuyActionCounters(loc, out counterTiles))
                        {
                            shopLocation = loc;
                            actionFound = true;
                            break;
                        }
                    }
                }

                if (CounterTilesResolver != null)
                {
                    var resolvedTiles = CounterTilesResolver(shopId, shopLocation);
                    if (resolvedTiles != null)
                    {
                        counterTiles = resolvedTiles;
                        actionFound = counterTiles.Count > 0;
                    }
                }

                // Owner standing area resolution:
                // For SeedShop: mirror of vanilla Stardew Valley GameLocation.HandleBuyAction IL constant:
                // Utility.TryOpenShopMenu("SeedShop", location, new Rectangle(4, 17, 1, 1), maxOwnerY = clicker.TileY - 1, ...)
                if (string.Equals(shopId, "SeedShop", StringComparison.OrdinalIgnoreCase))
                {
                    ownerArea = new Rectangle(4, 17, 1, 1);
                    if (counterTiles.Count == 0)
                    {
                        counterTiles = new[] { new TileCoordinate(4, 18), new TileCoordinate(5, 18) };
                    }
                    actionFound = true;
                }
                else if (actionFound)
                {
                    // Other shops without verified IL mirror or owner standing data:
                    // Remain null to explicitly trigger contract 'unknown' semantics rather than guessing.
                    ownerArea = null;
                }
            }
            catch (Exception ex)
            {
                _monitor.Log($"Failed resolving shop location and counter actions for '{shopId}': {ex.Message}", LogLevel.Warn);
            }
        }

        TileCoordinate? interactionTile = null;
        if (counterTiles.Count > 0)
        {
            interactionTile = new TileCoordinate(counterTiles[0].X, counterTiles[0].Y + 1);
        }

        var meta = new ShopActionMetadata(
            LocationName: shopLocation?.Name ?? preferredLocation ?? (string.Equals(shopId, "SeedShop", StringComparison.OrdinalIgnoreCase) ? "SeedShop" : null),
            OwnerArea: ownerArea,
            OpenTime: openTime,
            CloseTime: closeTime,
            ActionFound: actionFound,
            CounterTiles: counterTiles,
            InteractionTile: interactionTile
        );

        if (actionFound)
        {
            _shopActionCache[shopId] = meta;
        }
        return meta;
    }

    public IReadOnlyList<TileCoordinate> GetShopCounterTiles(string shopId, string? preferredLocation = null)
    {
        return GetShopActionMetadata(shopId, preferredLocation).CounterTiles;
    }

    private static string ParseClosedMessage(string rawMessage)
    {
        try
        {
            return TokenParser.ParseText(rawMessage);
        }
        catch
        {
            return rawMessage;
        }
    }

    public ShopScanInfo ScanShop(string shopId, IFarmerActor actor)
    {
        // 1. Available money resolution for companion (strictly read-only, separate wallet aware)
        int? availableMoney = null;
        string moneyStatus = "missing";

        if (actor?.GameFarmer != null)
        {
            if (CompanionMoneyResolver != null)
            {
                availableMoney = CompanionMoneyResolver(actor.GameFarmer);
                moneyStatus = availableMoney.HasValue ? "ok" : "missing";
            }
            else
            {
                try
                {
                    if (Game1.player?.team != null)
                    {
                        availableMoney = Game1.player.team.GetMoney(actor.GameFarmer).Value;
                        moneyStatus = "ok";
                    }
                    else
                    {
                        moneyStatus = "missing";
                    }
                }
                catch (Exception ex)
                {
                    _monitor.Log($"Failed to read companion money: {ex.Message}", LogLevel.Warn);
                    availableMoney = null;
                    moneyStatus = "missing";
                }
            }
        }
        else
        {
            moneyStatus = "missing";
        }

        // 2. Load shop definitions from DataLoader.Shops
        Dictionary<string, ShopData>? shops = null;
        try
        {
            shops = ShopDataResolver != null
                ? ShopDataResolver(Game1.content)
                : (Game1.content != null ? DataLoader.Shops(Game1.content) : null);
        }
        catch (Exception ex)
        {
            _monitor.Log($"Failed to load shop definitions: {ex.Message}", LogLevel.Warn);
            return new ShopScanInfo(
                ShopId: shopId,
                Status: "error",
                IsOpen: false,
                OwnerPresent: false,
                ClosedMessage: null,
                Owners: Array.Empty<string>(),
                Currency: 0,
                AvailableMoney: availableMoney,
                MoneyStatus: moneyStatus,
                Items: Array.Empty<ShopItemInfo>(),
                ErrorMessage: $"Failed to load game shop data: {ex.Message}"
            );
        }

        if (shops == null)
        {
            return new ShopScanInfo(
                ShopId: shopId,
                Status: "error",
                IsOpen: false,
                OwnerPresent: false,
                ClosedMessage: null,
                Owners: Array.Empty<string>(),
                Currency: 0,
                AvailableMoney: availableMoney,
                MoneyStatus: moneyStatus,
                Items: Array.Empty<ShopItemInfo>(),
                ErrorMessage: "Game content manager is not initialized."
            );
        }

        // 3. Verify shopId exists in game data without hardcoding
        if (!shops.TryGetValue(shopId, out var shopData) || shopData == null)
        {
            var knownIds = string.Join(", ", shops.Keys.OrderBy(k => k));
            return new ShopScanInfo(
                ShopId: shopId,
                Status: "unknown",
                IsOpen: false,
                OwnerPresent: false,
                ClosedMessage: null,
                Owners: Array.Empty<string>(),
                Currency: 0,
                AvailableMoney: availableMoney,
                MoneyStatus: moneyStatus,
                Items: Array.Empty<ShopItemInfo>(),
                ErrorMessage: $"Unknown shopId '{shopId}'. Available game shops: [{knownIds}]."
            );
        }

        // 4. Operating status: evaluate active owners via GameStateQuery / conditions & physical counter presence
        List<ShopOwnerData> currentOwners;
        try
        {
            var ownersEnum = ShopOwnersResolver != null
                ? ShopOwnersResolver(shopData)
                : ShopBuilder.GetCurrentOwners(shopData);
            currentOwners = ownersEnum?.ToList() ?? new List<ShopOwnerData>();
        }
        catch (Exception ex)
        {
            _monitor.Log($"Failed evaluating shop owners for '{shopId}': {ex.Message}", LogLevel.Warn);
            currentOwners = new List<ShopOwnerData>();
        }

        bool isOpen = false;
        bool ownerPresent = false;
        string? closedMessage = null;
        string status = "ok";
        string? errorMessage = null;

        var reportedOwners = currentOwners.Count > 0
            ? currentOwners.Select(o => !string.IsNullOrEmpty(o.Name) ? o.Name : o.Id).ToList()
            : (shopData.Owners?.Select(o => !string.IsNullOrEmpty(o.Name) ? o.Name : o.Id).ToList() ?? new List<string>());

        var meta = GetShopActionMetadata(shopId);
        string? shopLocationId = meta.LocationName ?? (string.Equals(shopId, "SeedShop", StringComparison.OrdinalIgnoreCase) ? "SeedShop" : null);
        TileCoordinate? interactionTile = meta.InteractionTile;

        if (currentOwners.Count == 0)
        {
            isOpen = false;
            ownerPresent = false;
            if (shopData.Owners != null)
            {
                var rawMsg = shopData.Owners.FirstOrDefault(o => !string.IsNullOrEmpty(o.ClosedMessage))?.ClosedMessage;
                closedMessage = rawMsg != null ? ParseClosedMessage(rawMsg) : null;
            }
        }
        else
        {
            // Check if shop has genuinely unmanned owner (AnyOrNone or None without ClosedMessage and no NamedNpc owners): does not require physical NPC
            var unmannedOwner = currentOwners.FirstOrDefault(o =>
                string.IsNullOrEmpty(o.ClosedMessage) &&
                (o.Type == ShopOwnerType.AnyOrNone ||
                 (o.Type == ShopOwnerType.None && !(shopData.Owners?.Any(allO => allO.Type == ShopOwnerType.NamedNpc || allO.Type == ShopOwnerType.Any) ?? false))));
            if (unmannedOwner != null)
            {
                isOpen = true;
                ownerPresent = true;
                closedMessage = null;
                _monitor.Log($"[GameWorldObserver] Shop '{shopId}' opened via unmanned owner '{unmannedOwner.Id}' (Type={unmannedOwner.Type}).", LogLevel.Info);
            }
            else
            {
                // If ownerArea cannot be reliably determined (other shops without verified IL/data mirror):
                // Return unknown semantics per contract (do not pretend/guess open or closed).
                if (!meta.ActionFound || !meta.OwnerArea.HasValue)
                {
                    status = "unknown";
                    isOpen = false;
                    ownerPresent = false;
                    errorMessage = !meta.ActionFound
                        ? $"Shop counter 'Buy' action not found for shop '{shopId}'."
                        : $"Cannot reliably determine owner standing area for shop '{shopId}'.";
                }
                else
                {
                    GameLocation? shopLocation = ShopLocationResolver != null
                        ? ShopLocationResolver(shopId)
                        : (meta.LocationName != null ? SafeGetLocationFromName(meta.LocationName) : SafeGetLocationFromName(shopId));

                    try
                    {
                        if (shopLocation == null && meta.LocationName != null && Game1.game1 != null && Game1.locations != null)
                        {
                            shopLocation = Game1.locations.FirstOrDefault(l => string.Equals(l?.Name, meta.LocationName, StringComparison.OrdinalIgnoreCase));
                        }
                    }
                    catch
                    {
                        // Ignore Game1.locations NRE in test environment
                    }

                    Rectangle ownerArea = meta.OwnerArea.Value;
                    int openTime = meta.OpenTime;
                    int closeTime = meta.CloseTime;

                    int currentTime = TimeOfDayResolver != null ? TimeOfDayResolver() : Game1.timeOfDay;
                    bool withinHours = true;
                    if (openTime >= 0 && currentTime < openTime)
                        withinHours = false;
                    if (closeTime >= 0 && currentTime >= closeTime)
                        withinHours = false;

                    IEnumerable<ShopNpcInfo>? npcs = null;
                    try
                    {
                        if (ShopNpcsResolver != null)
                        {
                            npcs = ShopNpcsResolver(shopLocation);
                        }
                        else if (shopLocation != null)
                        {
                            var rawNpcs = shopLocation.currentEvent?.actors ?? (IEnumerable<NPC>?)shopLocation.characters;
                            if (rawNpcs != null)
                            {
                                var list = new List<ShopNpcInfo>();
                                foreach (var n in rawNpcs)
                                {
                                    if (n != null && !string.IsNullOrEmpty(n.Name))
                                    {
                                        list.Add(new ShopNpcInfo(n.Name, n.TilePoint));
                                    }
                                }
                                npcs = list;
                            }
                        }
                    }
                    catch (Exception ex)
                    {
                        _monitor.Log($"Failed resolving NPCs for shop '{shopId}': {ex.Message}", LogLevel.Warn);
                    }

                    ShopOwnerData? chosenOwner = null;
                    ShopNpcInfo? ownerNpc = null;

                    // Aligned with GameLocation.HandleBuyAction and Utility.TryOpenShopMenu:
                    // present = (ownerArea == null || ownerArea.Contains(npc.TilePoint)) && (maxOwnerY == null || npc.TilePoint.Y <= maxOwnerY)
                    bool isAtCounter = actor != null
                        && string.Equals(actor.LocationName, meta.LocationName ?? shopLocation?.Name, StringComparison.OrdinalIgnoreCase)
                        && meta.CounterTiles.Any(c => actor.Tile.IsAdjacentTo(c));
                    int? maxOwnerY = isAtCounter ? (int?)(actor!.Tile.Y - 1) : null;

                    if (npcs != null)
                    {
                        foreach (var ownerData in currentOwners)
                        {
                            foreach (var npc in npcs)
                            {
                                if (npc == null) continue;
                                bool valid = ownerData.IsValid(npc.Name);
                                if (!valid) continue;

                                var tile = npc.TilePoint;
                                bool inArea = ownerArea.Contains(tile) && (!maxOwnerY.HasValue || tile.Y <= maxOwnerY.Value);
                                if (inArea)
                                {
                                    chosenOwner = ownerData;
                                    ownerNpc = npc;
                                    break;
                                }
                            }
                            if (chosenOwner != null)
                                break;
                        }
                    }

                    if (chosenOwner != null)
                    {
                        ownerPresent = true;

                        if (!withinHours)
                        {
                            isOpen = false;
                            string? rawMsg = !string.IsNullOrEmpty(chosenOwner.ClosedMessage)
                                ? chosenOwner.ClosedMessage
                                : shopData.Owners?.FirstOrDefault(o => !string.IsNullOrEmpty(o.ClosedMessage))?.ClosedMessage;
                            closedMessage = rawMsg != null ? ParseClosedMessage(rawMsg) : null;
                        }
                        else if (!string.IsNullOrEmpty(chosenOwner.ClosedMessage))
                        {
                            isOpen = false;
                            closedMessage = ParseClosedMessage(chosenOwner.ClosedMessage);
                        }
                        else
                        {
                            isOpen = true;
                            closedMessage = null;
                        }

                        _monitor.Log($"[GameWorldObserver] Shop '{shopId}' matched owner '{chosenOwner.Id}' ({ownerNpc?.Name}) at TilePoint ({ownerNpc?.TilePoint.X},{ownerNpc?.TilePoint.Y}), ownerArea=({ownerArea.X},{ownerArea.Y},{ownerArea.Width},{ownerArea.Height}), maxOwnerY={maxOwnerY} => withinHours={withinHours}, isOpen={isOpen}.", LogLevel.Info);
                    }
                    else
                    {
                        isOpen = false;
                        ownerPresent = false;
                        string? rawMsg = currentOwners.FirstOrDefault(o => !string.IsNullOrEmpty(o.ClosedMessage))?.ClosedMessage
                            ?? shopData.Owners?.FirstOrDefault(o => !string.IsNullOrEmpty(o.ClosedMessage))?.ClosedMessage;
                        if (rawMsg != null)
                        {
                            closedMessage = ParseClosedMessage(rawMsg);
                        }
                        else
                        {
                            closedMessage = "Come back when Pierre's tending the shop.";
                        }

                        var candidates = npcs != null ? string.Join(", ", npcs.Select(n => $"{n.Name}@({n.TilePoint.X},{n.TilePoint.Y})")) : "none";
                        _monitor.Log($"[GameWorldObserver] Shop '{shopId}' no owner in standing area ({ownerArea.X},{ownerArea.Y},{ownerArea.Width},{ownerArea.Height}), maxOwnerY={maxOwnerY}. Candidates: [{candidates}] => isOpen=false.", LogLevel.Info);
                    }
                }
            }
        }

        // 5. Query shop stock and prices dynamically via ShopBuilder
        var itemList = new List<ShopItemInfo>();
        try
        {
            var stock = ShopStockResolver != null
                ? ShopStockResolver(shopId, shopData)
                : ShopBuilder.GetShopStock(shopId, shopData);

            if (stock != null)
            {
                foreach (var (salable, stockInfo) in stock)
                {
                    if (salable == null || stockInfo == null) continue;

                    string itemId = salable.QualifiedItemId;
                    string name = !string.IsNullOrEmpty(salable.DisplayName) ? salable.DisplayName : (salable.Name ?? "");
                    int price = stockInfo.Price;
                    int stockVal = stockInfo.Stock;
                    bool isInfiniteStock = stockVal == int.MaxValue || salable.IsInfiniteStock();
                    string? tradeItem = stockInfo.TradeItem;
                    int? tradeItemCount = stockInfo.TradeItemCount;
                    string? limitedMode = stockInfo.LimitedStockMode != LimitedStockMode.None
                        ? stockInfo.LimitedStockMode.ToString()
                        : null;
                    IReadOnlyList<string>? actions = stockInfo.ActionsOnPurchase != null && stockInfo.ActionsOnPurchase.Count > 0
                        ? stockInfo.ActionsOnPurchase.ToList()
                        : null;
                    bool isSeed = false;
                    try
                    {
                        string cropId = SeedIdNormalizer.ToCropDataId(salable.QualifiedItemId);
                        string resolved = Crop.ResolveSeedId(cropId, null);
                        isSeed = !string.IsNullOrEmpty(resolved) && Crop.TryGetData(resolved, out var cropData) && cropData != null;
                    }
                    catch { }

                    itemList.Add(new ShopItemInfo(
                        ItemId: itemId,
                        Name: name,
                        Price: price,
                        Stock: stockVal,
                        IsInfiniteStock: isInfiniteStock,
                        TradeItem: tradeItem,
                        TradeItemCount: tradeItemCount,
                        LimitedStockMode: limitedMode,
                        ActionsOnPurchase: actions,
                        Category: (salable as Item)?.Category,
                        IsSeed: isSeed
                    ));
                }
            }
        }
        catch (Exception ex)
        {
            _monitor.Log($"Failed retrieving shop stock for '{shopId}': {ex.Message}", LogLevel.Warn);
            return new ShopScanInfo(
                ShopId: shopId,
                Status: "error",
                IsOpen: isOpen,
                OwnerPresent: ownerPresent,
                ClosedMessage: closedMessage,
                Owners: reportedOwners,
                Currency: shopData.Currency,
                AvailableMoney: availableMoney,
                MoneyStatus: moneyStatus,
                Items: Array.Empty<ShopItemInfo>(),
                ErrorMessage: $"Failed retrieving stock for shop '{shopId}': {ex.Message}",
                LocationId: shopLocationId,
                InteractionTile: interactionTile
            );
        }

        return new ShopScanInfo(
            ShopId: shopId,
            Status: status,
            IsOpen: isOpen,
            OwnerPresent: ownerPresent,
            ClosedMessage: closedMessage,
            Owners: reportedOwners,
            Currency: shopData.Currency,
            AvailableMoney: availableMoney,
            MoneyStatus: moneyStatus,
            Items: itemList,
            ErrorMessage: errorMessage,
            LocationId: shopLocationId,
            InteractionTile: interactionTile
        );
    }

    // ---------------------------------------------------------------------
    // Agricultural / husbandry observation (read-only).
    // ---------------------------------------------------------------------

    public IReadOnlyList<MachineScanInfo> ScanMachines(string locationName, int maxMachines = 64)
    {
        var loc = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
        if (loc is null)
            return Array.Empty<MachineScanInfo>();

        var machines = new List<MachineScanInfo>();
        try
        {
            foreach (var pair in loc.objects.Pairs)
            {
                if (pair.Value is not StardewValley.Object obj)
                    continue;

                StardewValley.GameData.Machines.MachineData? data;
                try { data = obj.GetMachineData(); }
                catch { data = null; }
                if (data is null)
                    continue;

                var held = obj.heldObject?.Value;
                int minutes = obj.MinutesUntilReady;
                machines.Add(new MachineScanInfo(
                    Tile: new TileCoordinate((int)pair.Key.X, (int)pair.Key.Y),
                    ItemId: obj.QualifiedItemId ?? obj.ItemId ?? "",
                    Name: obj.DisplayName ?? obj.Name ?? "",
                    IsMachine: true,
                    IsReady: obj.readyForHarvest?.Value == true,
                    MinutesUntilReady: minutes,
                    MinutesUntilReadyRemaining: minutes > 0 ? minutes : 0,
                    OutputItemId: held?.QualifiedItemId,
                    OutputName: held?.DisplayName,
                    OutputStack: held?.Stack ?? 0,
                    OutputQuality: held?.Quality ?? 0,
                    LastInputItemId: obj.lastInputItem?.Value?.QualifiedItemId,
                    HasInput: obj.lastInputItem?.Value is not null,
                    LocationName: locationName
                ));

                if (machines.Count >= maxMachines)
                    break;
            }
        }
        catch (Exception ex)
        {
            _monitor.Log($"Error scanning machines at '{locationName}': {ex.Message}", LogLevel.Warn);
        }

        return machines.OrderBy(m => m.Tile.Y).ThenBy(m => m.Tile.X).ToList();
    }

    public LivestockScanInfo ScanLivestock(string locationName, int maxBuildings = 12, int maxAnimals = 60)
    {
        var buildings = new List<AnimalBuildingScanInfo>();
        var roaming = new List<AnimalScanInfo>();
        bool buildingsTruncated = false;
        bool animalsTruncated = false;
        int animalBudget = maxAnimals;
        var seenIndoors = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        try
        {
            foreach (var house in EnumerateAnimalHouses())
            {
                string indoorsName = house.NameOrUniqueName ?? house.Name ?? "";
                if (!seenIndoors.Add(indoorsName))
                    continue;

                if (buildings.Count >= maxBuildings)
                {
                    buildingsTruncated = true;
                    break;
                }

                var animals = new List<AnimalScanInfo>();
                List<FarmAnimal> live;
                // getAllFarmAnimals also includes building interiors; only the native
                // residents of this map may be labelled with this interior's location.
                try { live = house.animals.Values.ToList(); }
                catch { live = new List<FarmAnimal>(); }

                foreach (var animal in live)
                {
                    if (animalBudget <= 0)
                    {
                        animalsTruncated = true;
                        break;
                    }
                    animalBudget--;
                    animals.Add(DescribeAnimal(animal, indoorsName));
                }

                var parent = house.ParentBuilding;
                // In 1.6 the real trough supply is the hay objects sitting on the native
                // Trough/Back tiles; AnimalHouse.piecesOfHay is a legacy field the game
                // never uses for feeding, so it is NOT reported as hay.
                int hay = CountTroughHay(house);
                int hayCapacity = 0;
                try { hayCapacity = house.GetHayCapacity(); } catch { hayCapacity = 0; }
                int siloHay = SumSiloHay();
                int limit = 0;
                try { limit = house.animalLimit?.Value ?? 0; } catch { limit = 0; }

                buildings.Add(new AnimalBuildingScanInfo(
                    BuildingType: parent?.buildingType?.Value ?? house.Name ?? "unknown",
                    IndoorsName: indoorsName,
                    LocationName: parent?.parentLocationName?.Value ?? "",
                    Tile: parent is not null
                        ? new TileCoordinate(parent.tileX?.Value ?? 0, parent.tileY?.Value ?? 0)
                        : new TileCoordinate(-1, -1),
                    DoorTile: parent is not null
                        ? new TileCoordinate(
                            (parent.tileX?.Value ?? 0) + (parent.animalDoor?.X ?? 0),
                            (parent.tileY?.Value ?? 0) + (parent.animalDoor?.Y ?? 0))
                        : new TileCoordinate(-1, -1),
                    AnimalDoorOpen: parent?.animalDoorOpen?.Value == true,
                    DoorStateKnown: parent is not null,
                    AnimalCount: animals.Count,
                    AnimalLimit: limit,
                    HayCount: hay,
                    HayCapacity: hayCapacity,
                    SiloHayCount: siloHay,
                    Animals: animals
                ));
            }

            // Animals roaming the queried map (outdoors) are reported separately.
            var activeLocation = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
            if (activeLocation is not null && activeLocation is not AnimalHouse)
            {
                List<FarmAnimal> here;
                // Do not flatten building interiors into the outdoor map: that would
                // duplicate indoor animals and assign their indoor tiles to this map.
                try { here = activeLocation.animals.Values.ToList(); }
                catch { here = new List<FarmAnimal>(); }
                foreach (var animal in here)
                {
                    if (animalBudget <= 0)
                    {
                        animalsTruncated = true;
                        break;
                    }
                    animalBudget--;
                    roaming.Add(DescribeAnimal(animal, activeLocation.NameOrUniqueName ?? locationName));
                }
            }
        }
        catch (Exception ex)
        {
            _monitor.Log($"Error scanning livestock: {ex.Message}", LogLevel.Warn);
        }

        return new LivestockScanInfo(buildings, roaming, buildingsTruncated, animalsTruncated);
    }

    private IEnumerable<AnimalHouse> EnumerateAnimalHouses()
    {
        var visited = new HashSet<GameLocation>();

        if (Game1.locations is not null)
        {
            // 1. First collect all building real indoors (recursively)
            foreach (var loc in Game1.locations)
            {
                if (loc is null)
                    continue;

                foreach (var house in TraverseBuildingIndoors(loc, visited))
                {
                    yield return house;
                }
            }

            // 2. Standalone registered AnimalHouse in Game1.locations fallback
            foreach (var loc in Game1.locations)
            {
                if (loc is AnimalHouse house && visited.Add(house))
                {
                    yield return house;
                }
            }
        }
    }

    private IEnumerable<AnimalHouse> TraverseBuildingIndoors(GameLocation location, HashSet<GameLocation> visited)
    {
        if (location?.buildings is null)
            yield break;

        foreach (var building in location.buildings)
        {
            if (building is null)
                continue;

            GameLocation? indoors = null;
            try
            {
                indoors = building.GetIndoors();
            }
            catch (Exception ex)
            {
                _monitor.Log($"Error getting building indoors: {ex.Message}", LogLevel.Trace);
            }

            if (indoors is null || !visited.Add(indoors))
                continue;

            if (indoors is AnimalHouse house)
            {
                yield return house;
            }

            foreach (var nested in TraverseBuildingIndoors(indoors, visited))
            {
                yield return nested;
            }
        }
    }

    private static AnimalScanInfo DescribeAnimal(FarmAnimal animal, string locationName)
    {
        StardewValley.GameData.FarmAnimals.FarmAnimalData? data = null;
        try { data = animal.GetAnimalData(); }
        catch { data = null; }

        string harvestType = "";
        try
        {
            var ht = animal.GetHarvestType();
            harvestType = ht?.ToString() ?? "";
        }
        catch { harvestType = ""; }

        return new AnimalScanInfo(
            Name: animal.Name ?? "",
            AnimalType: animal.type?.Value ?? "",
            DisplayType: animal.displayType ?? animal.type?.Value ?? "",
            Age: animal.age?.Value ?? 0,
            Happiness: animal.happiness?.Value ?? 0,
            Fullness: animal.fullness?.Value ?? 0,
            Friendship: animal.friendshipTowardFarmer?.Value ?? 0,
            ProduceReady: !string.IsNullOrWhiteSpace(animal.currentProduce?.Value),
            CurrentProduceId: string.IsNullOrWhiteSpace(animal.currentProduce?.Value) ? null : animal.currentProduce!.Value,
            HarvestType: string.IsNullOrWhiteSpace(harvestType) ? null : harvestType,
            RequiredTool: string.IsNullOrWhiteSpace(data?.HarvestTool) ? null : data!.HarvestTool,
            BuildingType: animal.buildingTypeILiveIn?.Value,
            LocationName: locationName,
            Tile: new TileCoordinate((int)animal.Tile.X, (int)animal.Tile.Y),
            WasPetToday: animal.wasPet?.Value == true,
            WasAutoPetToday: animal.wasAutoPet?.Value == true
        );
    }

    /// <summary>
    /// Real trough hay: hay objects on the building's native Trough/Back tiles. Mirrors
    /// <c>AnimalHouse.feedAllAnimals</c> / the feed-hopper action, which both work with hay
    /// objects and never use the legacy <c>AnimalHouse.piecesOfHay</c> field.
    /// </summary>
    private static int CountTroughHay(AnimalHouse house)
    {
        int count = 0;
        try
        {
            var layers = house.Map?.Layers;
            if (layers is null || layers.Count == 0)
                return 0;
            var layer = layers[0];
            for (int y = 0; y < layer.LayerHeight; y++)
            {
                for (int x = 0; x < layer.LayerWidth; x++)
                {
                    bool isTrough;
                    try { isTrough = house.doesTileHaveProperty(x, y, "Trough", "Back", false) is not null; }
                    catch { isTrough = false; }
                    if (!isTrough)
                        continue;

                    var tile = new Vector2(x, y);
                    if (house.objects.TryGetValue(tile, out var obj) && obj is not null
                        && FarmerMechanicsActor.ItemIdMatches(obj.QualifiedItemId ?? "", "(O)178"))
                        count++;
                }
            }
        }
        catch (Exception)
        {
            // Observation only; an unreadable map yields 0 rather than a fabricated count.
        }
        return count;
    }

    /// <summary>
    /// Hay stored in the real silos (the locations <c>GetHayFromAnySilo</c> searches). The
    /// vestigial <c>AnimalHouse.piecesOfHay</c> is excluded so a building's own counter can
    /// never be mistaken for silo supply.
    /// </summary>
    private static int SumSiloHay()
    {
        int total = 0;
        var seen = new HashSet<GameLocation>();
        try
        {
            foreach (var location in Game1.locations)
            {
                if (location is null || location is AnimalHouse || !seen.Add(location))
                    continue;
                total += location.piecesOfHay?.Value ?? 0;
            }
        }
        catch (Exception)
        {
            // Observation only.
        }
        return total;
    }

    public IReadOnlyList<TileCoordinate> FindWaterRefillTiles(
        string locationName,
        TileCoordinate center,
        int radius,
        int maxTiles = 16)
    {
        var loc = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
        if (loc is null)
            return Array.Empty<TileCoordinate>();

        radius = Math.Clamp(radius, 0, 64);
        var result = new List<TileCoordinate>();
        try
        {
            for (int dy = -radius; dy <= radius; dy++)
            {
                for (int dx = -radius; dx <= radius; dx++)
                {
                    int tx = center.X + dx;
                    int ty = center.Y + dy;
                    if (tx < 0 || ty < 0)
                        continue;
                    bool canRefill;
                    try { canRefill = loc.CanRefillWateringCanOnTile(tx, ty); }
                    catch { canRefill = false; }
                    if (canRefill)
                        result.Add(new TileCoordinate(tx, ty));
                }
            }
        }
        catch (Exception ex)
        {
            _monitor.Log($"Error scanning water refill tiles at '{locationName}': {ex.Message}", LogLevel.Warn);
        }

        // Select nearby native sources before truncation, so a large pond cannot
        // crowd the adjacent shoreline out of the companion's observation.
        return result.OrderBy(t => Math.Abs(t.X - center.X) + Math.Abs(t.Y - center.Y))
            .ThenBy(t => t.Y).ThenBy(t => t.X).Take(Math.Max(0, maxTiles)).ToList();
    }

    public bool HasFertilizer(string locationName, TileCoordinate tile)
    {
        var loc = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
        if (loc is null)
            return false;

        try
        {
            var v = new Vector2(tile.X, tile.Y);
            if (loc.terrainFeatures.TryGetValue(v, out var tf) && tf is HoeDirt dirt)
                return dirt.HasFertilizer();
        }
        catch (Exception ex)
        {
            _monitor.Log($"Error reading fertilizer at '{locationName}' {tile}: {ex.Message}", LogLevel.Warn);
        }

        return false;
    }

    public IReadOnlyList<GroundItemScanInfo> ScanGroundItems(
        string locationName,
        TileCoordinate center,
        int radius,
        int maxItems = 64)
    {
        var loc = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
        if (loc is null)
            return Array.Empty<GroundItemScanInfo>();

        radius = Math.Clamp(radius, 0, 64);
        var items = new List<GroundItemScanInfo>();

        bool InRange(int x, int y) =>
            Math.Abs(x - center.X) <= radius && Math.Abs(y - center.Y) <= radius;

        try
        {
            foreach (var pair in loc.objects.Pairs)
            {
                if (pair.Value is not StardewValley.Object obj)
                    continue;
                int ox = (int)pair.Key.X;
                int oy = (int)pair.Key.Y;
                if (!InRange(ox, oy))
                    continue;

                bool isWeed = false;
                try { isWeed = obj.IsWeeds(); }
                catch { isWeed = false; }
                // Native obstacle classification: only real game identities are used,
                // never a guessed item id. Stones are cleared with the Pickaxe, twigs
                // (and weeds) with the Axe/Hoe exactly like the player does.
                bool isStone = false;
                bool isTwig = false;
                try
                {
                    if (!isWeed)
                    {
                        isStone = obj.IsBreakableStone();
                        isTwig = !isStone && obj.IsTwig();
                    }
                }
                catch { isStone = false; isTwig = false; }

                bool canGrab = obj.CanBeGrabbed;
                string kind = isWeed ? "weed" : (isStone ? "stone" : (isTwig ? "twig" : (canGrab ? "object" : "obstacle")));
                string? clearTool = isWeed ? "hoe" : (isStone ? "pickaxe" : (isTwig ? "axe" : null));
                items.Add(new GroundItemScanInfo(
                    Tile: new TileCoordinate(ox, oy),
                    Kind: kind,
                    ItemId: obj.QualifiedItemId ?? obj.ItemId ?? "",
                    Name: obj.DisplayName ?? obj.Name ?? "",
                    Stack: obj.Stack,
                    IsDropped: false,
                    IsWeed: isWeed,
                    CanBeGrabbed: canGrab,
                    ClearTool: clearTool,
                    IsStone: isStone,
                    IsTwig: isTwig
                ));
            }

            foreach (var debris in loc.debris)
            {
                if (debris is null || !StardewAI.Companion.Mod.Adapters.NativeAnimalHarvestInventoryScope.IsOrdinaryDebris(debris)) continue;
                foreach (var group in debris.Chunks.GroupBy(c => new TileCoordinate((int)(c.position.Value.X / 64), (int)(c.position.Value.Y / 64))))
                {
                    if (!InRange(group.Key.X, group.Key.Y)) continue;
                    items.Add(new GroundItemScanInfo(
                        Tile: group.Key, Kind: "dropped",
                        ItemId: debris.item?.QualifiedItemId ?? debris.itemId.Value,
                        Name: debris.item?.DisplayName ?? "Dropped item",
                        Stack: debris.item?.Stack ?? group.Count(), IsDropped: true,
                        IsWeed: false, CanBeGrabbed: true, ClearTool: null));
                }
            }
        }
        catch (Exception ex)
        {
            _monitor.Log($"Error scanning ground items at '{locationName}': {ex.Message}", LogLevel.Warn);
        }

        return items.OrderBy(i => Math.Abs(i.Tile.X - center.X) + Math.Abs(i.Tile.Y - center.Y))
            .ThenBy(i => i.Tile.Y).ThenBy(i => i.Tile.X).Take(maxItems).ToList();
    }

    public IReadOnlyList<ChoppableTreeScanInfo> ScanChoppableTrees(
        string locationName,
        TileCoordinate center,
        int radius,
        int maxItems = 64)
    {
        var loc = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
        if (loc is null)
            return Array.Empty<ChoppableTreeScanInfo>();

        radius = Math.Clamp(radius, 0, 64);
        var items = new List<ChoppableTreeScanInfo>();

        bool InRange(int x, int y) =>
            Math.Abs(x - center.X) <= radius && Math.Abs(y - center.Y) <= radius;

        try
        {
            foreach (var pair in loc.terrainFeatures.Pairs)
            {
                // Wild trees only: fruit trees are long-term player investments and
                // are never offered as chopping targets.
                if (pair.Value is not StardewValley.TerrainFeatures.Tree tree)
                    continue;
                int x = (int)pair.Key.X;
                int y = (int)pair.Key.Y;
                if (!InRange(x, y))
                    continue;

                int stage;
                try { stage = tree.growthStage?.Value ?? -1; } catch { stage = -1; }
                bool tapped;
                try { tapped = tree.tapped?.Value == true; } catch { tapped = false; }
                bool stump;
                try { stump = tree.stump?.Value == true; } catch { stump = false; }

                items.Add(new ChoppableTreeScanInfo(
                    Tile: new TileCoordinate(x, y),
                    Kind: stump ? "stump" : "tree",
                    GrowthStage: stage,
                    Tapped: tapped));
            }

            foreach (var clump in loc.resourceClumps)
            {
                if (clump is null)
                    continue;
                // Giant stump (600) and hollow log (602) are the choppable clumps;
                // boulders and meteorites are pickaxe targets, never listed here.
                int sheetIndex;
                try { sheetIndex = clump.parentSheetIndex?.Value ?? -1; } catch { continue; }
                if (sheetIndex != 600 && sheetIndex != 602)
                    continue;

                int cx, cy, cw, ch;
                try
                {
                    var tile = clump.Tile;
                    cx = (int)tile.X;
                    cy = (int)tile.Y;
                    cw = clump.width?.Value ?? 1;
                    ch = clump.height?.Value ?? 1;
                }
                catch { continue; }
                if (!InRange(cx, cy))
                    continue;

                items.Add(new ChoppableTreeScanInfo(
                    Tile: new TileCoordinate(cx, cy),
                    Kind: sheetIndex == 600 ? "stump" : "log",
                    GrowthStage: -1,
                    Tapped: false,
                    Width: cw,
                    Height: ch));
            }
        }
        catch (Exception ex)
        {
            _monitor.Log($"Error scanning choppable trees at '{locationName}': {ex.Message}", LogLevel.Warn);
        }

        return items.OrderBy(i => Math.Abs(i.Tile.X - center.X) + Math.Abs(i.Tile.Y - center.Y))
            .ThenBy(i => i.Tile.Y).ThenBy(i => i.Tile.X).Take(maxItems).ToList();
    }
}
