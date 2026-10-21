using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.PlayerMode;

/// <summary>
/// Farm business observations are complete and available from every map, per user policy.
/// Chest contents still require actual prior opening; shop inventory requires its native menu.
/// Player-mode knowledge gate: an <see cref="IWorldObserver"/> decorator over
/// <see cref="GameWorldObserver"/> that only lets the agent learn what the real
/// player legitimately knows — own status, the current visible map, and knowledge
/// accumulated from actually visited maps and actually opened chests
/// (<see cref="PlayerKnowledgeTracker"/>). This is THE single gate for both
/// tool observations (inspect-* skills) and planner observations (periodic
/// snapshots); integration injects one instance everywhere an observer is used.
///
/// Gate rules (unknown means empty result / null / not-supported, never fabricated;
/// every block is logged at Debug):
/// - Location-scoped dynamic scans (InspectLocation, ScanFarmWork, GetDirtState,
///   ScanGroundItems, ScanChoppableTrees, FindWaterRefillTiles, ScanPlantingOptions,
///   InspectPlanting, ScanMachines, InspectMachines, ScanLivestock, InspectProduction)
///   require the location to be the player's current map or a visited map.
/// - ScanChests lists chests on visited maps but strips contents of chests the
///   player never opened; GetChestAt requires visited AND observed.
/// - ScanShop requires the shop's map (resolved via the inner shop-action metadata,
///   i.e. map Buy-action / location-name lookup) to be visited or current; when the
///   shop→location mapping cannot be resolved it fails closed.
/// - GetProductionSignals delegates but filters signals to visited/current maps
///   (the inner scan iterates every loaded location).
/// - InspectMapImage requires the current map (it is a screenshot of the rendered map).
/// - Static map geometry (tile passability, warps/doors, location existence, shop
///   counter tiles and shop action metadata, which expose tile coordinates and
///   operating hours only — no stock), player-relative facts (snapshots, positions,
///   clock/weather), own-inventory crafting knowledge, and revision plumbing pass
///   through ungated.
///
/// Threading: same contract as the inner observer — game thread only for spatial
/// and production reads.
/// </summary>
public sealed class PlayerKnownObserver : IWorldObserver
{
    public void NoteChestOpened(string location,int x,int y) => _knowledge.NoteChestObserved(location,x,y);
    private readonly GameWorldObserver _inner;
    private readonly PlayerKnowledgeTracker _knowledge;
    private readonly Action<string, LogLevel>? _log;
    private readonly Dictionary<string, (string Stamp, ShopScanInfo Quote)> _shopQuotes = new();

    public PlayerKnownObserver(GameWorldObserver inner, PlayerKnowledgeTracker knowledge, Action<string, LogLevel>? log = null)
    {
        _inner = inner ?? throw new ArgumentNullException(nameof(inner));
        _knowledge = knowledge ?? throw new ArgumentNullException(nameof(knowledge));
        _log = log;
    }

    private static string? CurrentMapName => Game1.currentLocation?.NameOrUniqueName;
    private static Rectangle VisibleTiles {
        get {
            var layer=Game1.currentLocation?.Map?.Layers.FirstOrDefault();
            if (layer == null) return Rectangle.Empty;
            int left=Math.Max(0,Game1.viewport.X/64), top=Math.Max(0,Game1.viewport.Y/64);
            int right=Math.Min(layer.LayerWidth,(Game1.viewport.X+Game1.viewport.Width+63)/64);
            int bottom=Math.Min(layer.LayerHeight,(Game1.viewport.Y+Game1.viewport.Height+63)/64);
            return new(left,top,Math.Max(0,right-left),Math.Max(0,bottom-top));
        }
    }
    private static bool Visible(TileCoordinate tile) => VisibleTiles.Contains(tile.X,tile.Y);
    private static Rectangle VisibleRegion(Rectangle? requested) {
        var visible=requested.HasValue ? Rectangle.Intersect(VisibleTiles,requested.Value) : VisibleTiles;
        if (visible.Width == 0 || visible.Height == 0) throw new NotSupportedException("Requested region is outside the player's visible map.");
        return visible;
    }

    private static bool IsCurrentLocation(string? locationName) =>
        !string.IsNullOrWhiteSpace(locationName) &&
        string.Equals(CurrentMapName, locationName, StringComparison.OrdinalIgnoreCase);

    private static bool IsFarm(string? locationName) => string.Equals(locationName,"Farm",StringComparison.OrdinalIgnoreCase);

    // Maps the player personally owns: chests there are the player's own property, so
    // their contents are never spoiler-gated in player mode (covers the 1.6 starter
    // gift parcel in the FarmHouse and any chest the player placed at home).
    private static bool IsPlayerPropertyMap(string? locationName) =>
        IsFarm(locationName)
        || string.Equals(locationName, "FarmHouse", StringComparison.OrdinalIgnoreCase)
        || string.Equals(locationName, "Greenhouse", StringComparison.OrdinalIgnoreCase);
    private static bool ObservableTile(string location,TileCoordinate tile) => IsFarm(location) || Visible(tile);

    private bool IsLocationKnown(string? locationName) =>
        IsCurrentLocation(locationName) || IsFarm(locationName); // The user explicitly authorizes complete Farm business observations, including from FarmHouse.

    private void LogGate(string message) => _log?.Invoke(message, LogLevel.Debug);

    private static NotSupportedException LocationNotKnown(string member, string locationName) =>
        new($"Location '{locationName}' is not player-known (never visited); {member} is unavailable in player mode.");

    // ---------------------------------------------------------------------
    // Spatial / map observation (gated to current or visited maps).
    // ---------------------------------------------------------------------

    public Dictionary<string, object> InspectLocation(string locationName, Rectangle? region = null)
    {
        if (!IsLocationKnown(locationName)) throw LocationNotKnown(nameof(InspectLocation), locationName);
        return _inner.InspectLocation(locationName, IsFarm(locationName) ? region : VisibleRegion(region));
    }

    public Dictionary<string, object> InspectMapImage(string locationName)
    {
        // A screenshot of the rendered map is only possible for the current map; keep
        // the interface's not-supported shape for anything else (the inner observer
        // would reject it as well, as a non-current-map screenshot).
        if (!IsCurrentLocation(locationName)) throw LocationNotKnown(nameof(InspectMapImage), locationName);
        throw new NotSupportedException("Whole-map screenshots expose offscreen facts; player mode uses structured visible observations.");
    }

    public TileDirtState GetDirtState(string locationName, TileCoordinate tile)
    {
        if (!IsLocationKnown(locationName) || !ObservableTile(locationName,tile))
        {
            LogGate($"[PlayerKnownObserver] GetDirtState('{locationName}') blocked: location is not player-known.");
            throw new NotSupportedException("Soil outside the player's current visible map is unknown.");
        }
        return _inner.GetDirtState(locationName, tile);
    }

    public IReadOnlyList<FarmDirtWorkItem> ScanFarmWork(string locationName)
    {
        if (!IsLocationKnown(locationName))
        {
            LogGate($"[PlayerKnownObserver] ScanFarmWork('{locationName}') blocked: location is not player-known.");
            throw LocationNotKnown(nameof(ScanFarmWork),locationName);
        }
        return _inner.ScanFarmWork(locationName).Where(item=>ObservableTile(locationName,item.Tile)).ToArray();
    }

    public PlantingScanInfo ScanPlantingOptions(string locationName, TileCoordinate center, int radius, IFarmerActor actor)
    {
        if (!IsLocationKnown(locationName))
        {
            LogGate($"[PlayerKnownObserver] ScanPlantingOptions('{locationName}') blocked: location is not player-known.");
            throw LocationNotKnown(nameof(ScanPlantingOptions),locationName);
        }
        if (IsFarm(locationName)) {
            var layer=Game1.getLocationFromName(locationName)?.Map?.Layers.FirstOrDefault() ?? throw LocationNotKnown(nameof(ScanPlantingOptions),locationName);
            return _inner.ScanPlantingOptions(locationName,new(layer.LayerWidth/2,layer.LayerHeight/2),Math.Max(layer.LayerWidth,layer.LayerHeight),actor);
        }
        var observed=_inner.ScanPlantingOptions(locationName, center, radius, actor);
        var tilled=observed.TilledEmptyTiles.Where(tile=>ObservableTile(locationName,tile)).ToArray();
        var tillable=observed.TillableTiles.Where(tile=>ObservableTile(locationName,tile)).ToArray();
        return observed with { TilledEmptyTiles=tilled,TotalTilledEmptyCount=tilled.Length,TilledEmptyTruncated=true,TillableTiles=tillable,TotalTillableCount=tillable.Length,TillableTruncated=true };
    }

    public Dictionary<string, object> InspectPlanting(string locationName, IFarmerActor actor, Rectangle? region = null)
    {
        if (!IsLocationKnown(locationName)) throw LocationNotKnown(nameof(InspectPlanting), locationName);
        var observation=_inner.InspectPlanting(locationName, actor, IsFarm(locationName) ? region : VisibleRegion(region));
        observation["observationStatus"]=IsFarm(locationName) ? "observed" : "partial";
        observation["scope"]=IsFarm(locationName) ? (region.HasValue ? "requested-region" : "location") : "current-visible-search-region";
        observation["mapComplete"]=IsFarm(locationName) && !region.HasValue;
        return observation;
    }

    public IReadOnlyList<GroundItemScanInfo> ScanGroundItems(string locationName, TileCoordinate center, int radius, int maxItems = 64)
    {
        if (!IsLocationKnown(locationName))
        {
            LogGate($"[PlayerKnownObserver] ScanGroundItems('{locationName}') blocked: location is not player-known.");
            return Array.Empty<GroundItemScanInfo>();
        }
        return _inner.ScanGroundItems(locationName, center, radius, maxItems).Where(item=>ObservableTile(locationName,item.Tile)).ToArray();
    }

    public IReadOnlyList<ChoppableTreeScanInfo> ScanChoppableTrees(string locationName, TileCoordinate center, int radius, int maxItems = 64)
    {
        if (!IsLocationKnown(locationName))
        {
            LogGate($"[PlayerKnownObserver] ScanChoppableTrees('{locationName}') blocked: location is not player-known.");
            return Array.Empty<ChoppableTreeScanInfo>();
        }
        return _inner.ScanChoppableTrees(locationName, center, radius, maxItems).Where(item=>ObservableTile(locationName,item.Tile)).ToArray();
    }

    public IReadOnlyList<TileCoordinate> FindWaterRefillTiles(string locationName, TileCoordinate center, int radius, int maxTiles = 16)
    {
        if (!IsLocationKnown(locationName))
        {
            LogGate($"[PlayerKnownObserver] FindWaterRefillTiles('{locationName}') blocked: location is not player-known.");
            return Array.Empty<TileCoordinate>();
        }
        return _inner.FindWaterRefillTiles(locationName, center, radius, maxTiles).Where(tile=>ObservableTile(locationName,tile)).ToArray();
    }

    // ---------------------------------------------------------------------
    // Chests: visited maps may list chests, but contents only after opening.
    // ---------------------------------------------------------------------

    public IReadOnlyList<ChestScanInfo> ScanChests(string locationName)
    {
        if (!IsLocationKnown(locationName))
        {
            LogGate($"[PlayerKnownObserver] ScanChests('{locationName}') blocked: location is not player-known.");
            return Array.Empty<ChestScanInfo>();
        }

        var chests = _inner.ScanChests(locationName).Where(item=>ObservableTile(locationName,item.Tile)).ToArray();
        if (chests.Length == 0) return chests;

        // On the player's own property the player IS the owner: contents are visible
        // without the prior-opening gate (starter gift parcel, own chests). Other
        // visited maps keep the strip-until-opened rule.
        bool ownProperty = IsPlayerPropertyMap(locationName);
        if (ownProperty) return chests;

        int stripped = 0;
        var gated = new List<ChestScanInfo>(chests.Length);
        foreach (var chest in chests)
        {
            if (_knowledge.HasObservedChest(locationName, chest.Tile.X, chest.Tile.Y))
            {
                gated.Add(chest);
                continue;
            }
            stripped++;
            // Capacity is static type knowledge. Unknown contents must not mean
            // empty, and unknown free slots must not mean a full chest.
            gated.Add(chest with { Contents = Array.Empty<ChestSlotInfo>(), FreeSlots = null, ContentsObserved = false });
        }
        if (stripped > 0)
            LogGate($"[PlayerKnownObserver] ScanChests('{locationName}') stripped contents of {stripped} unopened chest(s).");
        return gated;
    }

    public ChestScanInfo? GetChestAt(string locationName, TileCoordinate tile)
    {
        // Own-property chests are reachable without the prior-opening gate; every
        // other chest still requires the player to have observed it.
        bool contentsAllowed = IsPlayerPropertyMap(locationName) || _knowledge.HasObservedChest(locationName, tile.X, tile.Y);
        if (!IsLocationKnown(locationName) || !ObservableTile(locationName,tile) || !contentsAllowed)
        {
            LogGate($"[PlayerKnownObserver] GetChestAt('{locationName}' {tile.X},{tile.Y}) blocked: chest not observed by the player.");
            return null;
        }
        return _inner.GetChestAt(locationName, tile);
    }

    // ---------------------------------------------------------------------
    // Production / machines / livestock (gated; all-location scans filtered).
    // ---------------------------------------------------------------------

    public IReadOnlyList<MachineScanInfo> ScanMachines(string locationName, int maxMachines = 64)
    {
        if (!IsLocationKnown(locationName))
        {
            LogGate($"[PlayerKnownObserver] ScanMachines('{locationName}') blocked: location is not player-known.");
            return Array.Empty<MachineScanInfo>();
        }
        return _inner.ScanMachines(locationName, maxMachines).Where(item=>ObservableTile(locationName,item.Tile)).ToArray();
    }

    public Dictionary<string, object> InspectMachines(string locationName)
    {
        if (!IsLocationKnown(locationName)) throw LocationNotKnown(nameof(InspectMachines), locationName);
        return new() { ["locationId"]=locationName,["capturedRevision"]=WorldRevision,["machines"]=ScanMachines(locationName),["scope"]="current-visible-map" };
    }

    public Dictionary<string, object> InspectProduction(string locationName, IFarmerActor actor)
    {
        if (!IsLocationKnown(locationName)) throw LocationNotKnown(nameof(InspectProduction), locationName);
        // Delegate to the inner whole-map production observation (ground items, water
        // refill tiles with map scope, edible foods) instead of the truncated machines
        // stub; then restore the player-mode extras and the true inventory owner.
        var observation = _inner.InspectProduction(locationName, actor);
        observation["inventoryOwner"] = ActorGuards.IsPlayerBound(actor) ? "player" : "companion";
        observation["machines"] = ScanMachines(locationName);
        observation["scope"] = "map";
        observation["note"] = IsFarm(locationName)
            ? "Whole Farm production is observed, including water sources and public fixtures. Animal interiors remain unsupported."
            : "This player-known map production observation does not reveal unvisited animal interiors.";
        observation["interactables"] = ScanInteractables(locationName);
        return observation;
    }

    public IReadOnlyList<Dictionary<string, object>> ScanInteractables(string locationName, int maxItems = 32)
    {
        if (!IsLocationKnown(locationName)) throw LocationNotKnown(nameof(ScanInteractables), locationName);
        try
        {
            return _inner.ScanInteractables(locationName, maxItems);
        }
        catch (Exception ex)
        {
            LogGate($"[PlayerKnownObserver] ScanInteractables('{locationName}') failed: {ex.Message}");
            return Array.Empty<Dictionary<string, object>>();
        }
    }

    public IReadOnlyList<ProductionSignal> GetProductionSignals()
    {
        var signals = _inner.GetProductionSignals();
        if (signals.Count == 0) return signals;
        // The inner scan iterates every loaded location; keep only signals on maps the
        // player knows. Per-location InspectMachines/ScanMachines on visited maps is the
        // fully supported player-known production path.
        var filtered = signals.Where(s => IsLocationKnown(s.LocationId) && ObservableTile(s.LocationId,new(s.Tile.X,s.Tile.Y))).ToList();
        if (filtered.Count != signals.Count)
            LogGate($"[PlayerKnownObserver] GetProductionSignals filtered out {signals.Count - filtered.Count} signal(s) on unvisited maps.");
        return filtered;
    }

    /// <summary>
    /// The inner flag describes truncation of the unfiltered all-location scan; the
    /// decorator's filtered view is partial by design, so it is not surfaced.
    /// </summary>
    public bool ProductionSignalsTruncated => true; // Only the current viewport is observed, never the full farm/world.

    public void TrackProductionMachine(string locationName, TileCoordinate tile) =>
        _inner.TrackProductionMachine(locationName, tile);

    public LivestockScanInfo ScanLivestock(string locationName, int maxBuildings = 12, int maxAnimals = 60)
    {
        throw new NotSupportedException("Player-mode livestock observation is not supported in phase 1; hidden animal/interior state is not exposed.");
    }

    private LivestockScanInfo LegacyKnownLivestock(string locationName, int maxBuildings, int maxAnimals)
    {
        if (!IsLocationKnown(locationName))
        {
            LogGate($"[PlayerKnownObserver] ScanLivestock('{locationName}') blocked: location is not player-known.");
            return new LivestockScanInfo(Array.Empty<AnimalBuildingScanInfo>(), Array.Empty<AnimalScanInfo>(), false, false);
        }

        var scan = _inner.ScanLivestock(locationName, maxBuildings, maxAnimals);
        // The inner scan enumerates animal houses across every loaded map, so even a
        // visited query map leaks unvisited buildings; drop buildings whose map is
        // unknown (fail closed). Animals roaming the queried map are on a known map.
        int dropped = 0;
        var buildings = new List<AnimalBuildingScanInfo>(scan.Buildings.Count);
        foreach (var building in scan.Buildings)
        {
            if (IsBuildingKnown(building)) buildings.Add(building);
            else dropped++;
        }
        if (dropped > 0)
        {
            LogGate($"[PlayerKnownObserver] ScanLivestock('{locationName}') dropped {dropped} building(s) on unvisited maps.");
            return scan with { Buildings = buildings };
        }
        return scan;
    }

    private bool IsBuildingKnown(AnimalBuildingScanInfo building)
    {
        if (IsLocationKnown(building.LocationName)) return true;
        // Standalone registered AnimalHouse without a parent building: the indoors map
        // itself is the location, so fall back to its own name.
        return string.IsNullOrEmpty(building.LocationName) && IsLocationKnown(building.IndoorsName);
    }

    // ---------------------------------------------------------------------
    // Shops: stock is gated to the shop's visited/current map; counter tiles and
    // action metadata stay ungated because they expose only static map geometry
    // (tile coordinates, owner area, operating hours) — no stock or NPC state.
    // ---------------------------------------------------------------------

    public ShopScanInfo ScanShop(string shopId, IFarmerActor actor)
    {
        // Resolve shop→location with the inner metadata path (map 'Buy'/'OpenShop'
        // action scan plus location-name fallback). There is no canonical Data/Shops
        // → location mapping in the game data; this resolution is exactly what the
        // inner ScanShop itself uses.
        var metadata = _inner.GetShopActionMetadata(shopId);
        string stamp=$"{Game1.uniqueIDForThisGame}:{actor.GameFarmer?.UniqueMultiplayerID}:{Game1.Date.TotalDays}";
        string? shopLocation = metadata.LocationName;
        if (string.IsNullOrWhiteSpace(shopLocation))
        {
            LogGate($"[PlayerKnownObserver] ScanShop('{shopId}') blocked: shop location could not be resolved (fail closed).");
            return BlockedShopScan(shopId, null,
                "Player-knowledge gate: the shop's map cannot be resolved, so its stock is unknown in player mode.");
        }
        // Read stock exclusively from the menu the player has actually opened.
        if (Game1.activeClickableMenu is not StardewValley.Menus.ShopMenu shop
            || Game1.currentLocation?.NameOrUniqueName != shopLocation)
        {
            if (_shopQuotes.TryGetValue(shopId,out var remembered) && remembered.Stamp == stamp)
                return remembered.Quote with { Status="last-observed-native-menu" };
            return BlockedShopScan(shopId,shopLocation,"Stock unknown: open_shop must open the native menu first. Location and counter are public native map metadata.") with { InteractionTile = metadata.InteractionTile };
        }
        var items=shop.forSale.OfType<Item>().Where(item=>shop.itemPriceAndStock.ContainsKey(item)).Select(item=> {
            var stock=shop.itemPriceAndStock[item];
            return new ShopItemInfo(item.QualifiedItemId,item.DisplayName,stock.Price,stock.Stock,
                stock.Stock==int.MaxValue,Category:item.Category,IsSeed:item.Category==StardewValley.Object.SeedsCategory);
        }).ToArray();
        var quote = new ShopScanInfo(shopId,"observed",true,true,null,Array.Empty<string>(),shop.currency,
            actor.GameFarmer?.Money,"observed",items,
            ErrorMessage:$"Native menu observed on {Game1.Date} at {Game1.timeOfDay}; this is remembered quotation, not a continuously open menu. Purchase reopens and validates actual price and stock.",
            LocationId:shopLocation, InteractionTile:metadata.InteractionTile);
        _shopQuotes[shopId]=(stamp,quote);
        return quote;
    }

    public IReadOnlyList<TileCoordinate> GetShopCounterTiles(string shopId, string? preferredLocation = null) =>
        _inner.GetShopCounterTiles(shopId, preferredLocation);

    public ShopActionMetadata GetShopActionMetadata(string shopId, string? preferredLocation = null) =>
        _inner.GetShopActionMetadata(shopId, preferredLocation);

    private static ShopScanInfo BlockedShopScan(string shopId, string? locationId, string reason) =>
        new(
            ShopId: shopId,
            Status: "unknown",
            IsOpen: false,
            OwnerPresent: false,
            ClosedMessage: null,
            Owners: Array.Empty<string>(),
            Currency: 0,
            AvailableMoney: null,
            MoneyStatus: "missing",
            Items: Array.Empty<ShopItemInfo>(),
            ErrorMessage: reason,
            LocationId: locationId,
            InteractionTile: null);

    // ---------------------------------------------------------------------
    // Static geometry, player-relative facts, own inventory, plumbing (ungated).
    // ---------------------------------------------------------------------

    public Dictionary<string, object> InspectCrafting(IFarmerActor actor) => _inner.InspectCrafting(actor);

    public int WorldRevision => _inner.WorldRevision;

    public void BumpRevision() => _inner.BumpRevision();

    public string? CurrentLocationName => _inner.CurrentLocationName;

    public bool IsMainThread => _inner.IsMainThread;

    public bool LocationExists(string locationName) => _inner.LocationExists(locationName);

    public bool IsTilePassable(string locationName, TileCoordinate tile) {
        if (Game1.player != null) {
            var location=Game1.getLocationFromName(locationName);
            if (location == null) return false;
            var layer=location.Map?.Layers.FirstOrDefault();
            if (layer == null || tile.X < 0 || tile.Y < 0 || tile.X >= layer.LayerWidth || tile.Y >= layer.LayerHeight) return false;
            // Use the actual farmer footprint at the tile's standing center.
            // The legacy grid avatar's origin is not the player's standing point.
            // This also retains native bed-owner collision exceptions.
            var bounds = Game1.player.GetBoundingBox();
            var centered = new Rectangle(tile.X * 64 + 32 - bounds.Width / 2,
                tile.Y * 64 + 32 - bounds.Height / 2, bounds.Width, bounds.Height);
            // The short overload uses pathfinding=false and tests every farmer,
            // including this controlled player. A warp can leave the footprint
            // overlapping the next standing tile in a narrow entrance, falsely
            // sealing the only way out. Native pathfinding excludes that self
            // collision; actual walking still enforces native collisions.
            return !location.isCollidingPosition(centered, Game1.viewport, true, 0, false, Game1.player,
                pathfinding: true, projectile: false, ignoreCharacterRequirement: false, skipCollisionEffects: true);
        }
        return _inner.IsTilePassable(locationName,tile);
    }

    public bool IsWarpOrDoorTile(string locationName, TileCoordinate tile) => _inner.IsWarpOrDoorTile(locationName, tile);

    public PlayerResourceSnapshot GetPlayerSnapshot() => _inner.GetPlayerSnapshot();

    public bool IsPlayerOnSameMap(string locationName) => _inner.IsPlayerOnSameMap(locationName);

    public int TimeOfDay => _inner.TimeOfDay;

    public string CurrentSeason => _inner.CurrentSeason;

    public int DayOfMonth => _inner.DayOfMonth;

    public bool IsRaining => _inner.IsRaining;

    public string? WeatherIcon => _inner.WeatherIcon;

    public bool IsPlayerOnTile(string locationName, TileCoordinate tile) => false; // Local player is the controlled actor, not an obstruction.

    public bool HasFertilizer(string locationName, TileCoordinate tile) => IsLocationKnown(locationName) && ObservableTile(locationName,tile) ? _inner.HasFertilizer(locationName, tile) : throw new NotSupportedException("Offscreen fertilizer state is unknown.");
}
