using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>
/// In-memory controllable world observer for pure logic unit testing.
/// </summary>
public sealed class SimulatedWorldObserver : IWorldObserver
{
    private readonly Dictionary<TileCoordinate, bool> _passability = new();
    private readonly Dictionary<TileCoordinate, TileDirtState> _dirtMap = new();
    private PlayerResourceSnapshot _playerSnapshot = new(270f, 40, 10);
    private int _worldRevision = 1;

    public int WorldRevision => _worldRevision;

    public void BumpRevision() => _worldRevision++;
    public string? CurrentLocationName { get; set; } = "Farm";
    public bool IsMainThread { get; set; } = true;
    public bool PlayerOnSameMap { get; set; } = true;

    private readonly HashSet<TileCoordinate> _warpOrDoorTiles = new();

    public void SetPassable(TileCoordinate tile, bool passable) => _passability[tile] = passable;

    public void SetWarpOrDoor(TileCoordinate tile, bool isWarpOrDoor)
    {
        if (isWarpOrDoor) _warpOrDoorTiles.Add(tile);
        else _warpOrDoorTiles.Remove(tile);
    }

    public void SetDirt(TileCoordinate tile, TileDirtState state)
    {
        _dirtMap[tile] = state;
        _worldRevision++;
    }

    public void SetPlayerSnapshot(PlayerResourceSnapshot snapshot) => _playerSnapshot = snapshot;

    public bool IsTilePassable(string locationName, TileCoordinate tile)
    {
        if (!string.Equals(CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
            return false;
        return _passability.TryGetValue(tile, out bool passable) ? passable : true;
    }

    public bool IsWarpOrDoorTile(string locationName, TileCoordinate tile)
    {
        if (!string.Equals(CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
            return false;
        return _warpOrDoorTiles.Contains(tile);
    }

    public TileDirtState GetDirtState(string locationName, TileCoordinate tile)
    {
        return _dirtMap.TryGetValue(tile, out var state) ? state : TileDirtState.NotTilled;
    }

    public IReadOnlyList<FarmDirtWorkItem> ScanFarmWork(string locationName)
    {
        if (!string.Equals(CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
            return Array.Empty<FarmDirtWorkItem>();

        var items = new List<FarmDirtWorkItem>();
        foreach (var (tile, state) in _dirtMap)
        {
            if (state.IsTilled)
            {
                items.Add(new FarmDirtWorkItem(
                    Tile: tile,
                    IsWatered: state.IsWatered,
                    HasCrop: state.HasCrop,
                    CropId: state.CropId,
                    IsHarvestable: state.IsHarvestable
                ));
            }
        }

        return items.OrderBy(i => i.Tile.Y).ThenBy(i => i.Tile.X).ToList();
    }

    private readonly Dictionary<TileCoordinate, ChestScanInfo> _chests = new();

    public void SetChest(TileCoordinate tile, ChestScanInfo chest)
    {
        _chests[tile] = chest;
        _worldRevision++;
    }

    public void RemoveChest(TileCoordinate tile)
    {
        if (_chests.Remove(tile))
            _worldRevision++;
    }

    public IReadOnlyList<ChestScanInfo> ScanChests(string locationName)
    {
        if (!string.Equals(CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
            return Array.Empty<ChestScanInfo>();

        return _chests.Values.OrderBy(c => c.Tile.Y).ThenBy(c => c.Tile.X).ToList();
    }

    public ChestScanInfo? GetChestAt(string locationName, TileCoordinate tile)
    {
        if (!string.Equals(CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
            return null;

        return _chests.TryGetValue(tile, out var chest) ? chest : null;
    }

    public PlayerResourceSnapshot GetPlayerSnapshot() => _playerSnapshot;

    public bool IsPlayerOnSameMap(string locationName) =>
        PlayerOnSameMap && string.Equals(CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase);

    public int TimeOfDay { get; set; } = 600;
    public string CurrentSeason { get; set; } = "spring";
    public int DayOfMonth { get; set; } = 1;
    public bool IsRaining { get; set; } = false;
    public string? WeatherIcon { get; set; } = "0";

    public TileCoordinate? PlayerTile { get; set; }

    public bool IsPlayerOnTile(string locationName, TileCoordinate tile)
    {
        if (!IsPlayerOnSameMap(locationName)) return false;
        return PlayerTile.HasValue && PlayerTile.Value == tile;
    }

    private readonly HashSet<TileCoordinate> _tillableTiles = new();

    public void SetTillable(TileCoordinate tile, bool tillable)
    {
        if (tillable) _tillableTiles.Add(tile);
        else _tillableTiles.Remove(tile);
    }

    public PlantingScanInfo ScanPlantingOptions(string locationName, TileCoordinate center, int radius, IFarmerActor actor)
    {
        bool hasHoe = actor.Hoe != null || actor.GetInventorySnapshot().Any(i => i.IsTool && (i.ItemId.Contains("Hoe") || i.Name.Contains("Hoe")));

        var seeds = new List<SeedItemInfo>();
        foreach (var inv in actor.GetInventorySnapshot())
        {
            if (string.IsNullOrEmpty(inv.ItemId) || inv.IsTool) continue;
            if (inv.ItemId.Contains("Seed") || inv.Name.Contains("Seed") || inv.Category.Contains("Seed"))
            {
                bool canPlant = inv.ItemId.Contains(CurrentSeason, StringComparison.OrdinalIgnoreCase) ||
                                inv.Name.Contains(CurrentSeason, StringComparison.OrdinalIgnoreCase) ||
                                string.Equals(CurrentSeason, "spring", StringComparison.OrdinalIgnoreCase);
                seeds.Add(new SeedItemInfo(
                    ItemId: inv.ItemId,
                    Name: inv.Name,
                    Stack: inv.Stack,
                    CanPlantCurrentSeason: canPlant,
                    Seasons: new[] { CurrentSeason },
                    GrowthDays: 4,
                    Regrows: false,
                    IsRaised: false
                ));
            }
        }

        var tilledEmpty = new List<TileCoordinate>();
        foreach (var (tile, dirt) in _dirtMap)
        {
            if (dirt.IsTilled && !dirt.HasCrop)
            {
                if (Math.Abs(tile.X - center.X) <= radius && Math.Abs(tile.Y - center.Y) <= radius)
                {
                    tilledEmpty.Add(tile);
                }
            }
        }

        var tillable = new List<TileCoordinate>();
        foreach (var tile in _tillableTiles)
        {
            if (Math.Abs(tile.X - center.X) <= radius && Math.Abs(tile.Y - center.Y) <= radius)
            {
                if (!_dirtMap.TryGetValue(tile, out var dirt) || !dirt.IsTilled)
                {
                    tillable.Add(tile);
                }
            }
        }

        tilledEmpty.Sort((a, b) => a.ManhattanDistanceTo(center).CompareTo(b.ManhattanDistanceTo(center)));
        tillable.Sort((a, b) => a.ManhattanDistanceTo(center).CompareTo(b.ManhattanDistanceTo(center)));

        return new PlantingScanInfo(
            Season: CurrentSeason,
            DayOfMonth: DayOfMonth,
            CompanionHasHoe: hasHoe,
            Seeds: seeds,
            TilledEmptyTiles: tilledEmpty,
            TotalTilledEmptyCount: tilledEmpty.Count,
            TilledEmptyTruncated: false,
            TillableTiles: tillable,
            TotalTillableCount: tillable.Count,
            TillableTruncated: false,
            SearchCenter: center,
            SearchRadius: radius
        );
    }

    private readonly Dictionary<string, ShopScanInfo> _shops = new(StringComparer.OrdinalIgnoreCase);

    public void SetShop(string shopId, ShopScanInfo shop)
    {
        _shops[shopId] = shop;
        _worldRevision++;
    }

    public ShopScanInfo ScanShop(string shopId, IFarmerActor actor)
    {
        if (_shops.TryGetValue(shopId, out var shop))
        {
            return shop;
        }

        var available = _shops.Count > 0 ? string.Join(", ", _shops.Keys.OrderBy(k => k)) : "none";
        int? money = actor?.GameFarmer != null ? 1000 : null;
        string moneyStatus = actor?.GameFarmer != null ? "ok" : "missing";

        return new ShopScanInfo(
            ShopId: shopId,
            Status: "unknown",
            IsOpen: false,
            OwnerPresent: false,
            ClosedMessage: null,
            Owners: Array.Empty<string>(),
            Currency: 0,
            AvailableMoney: money,
            MoneyStatus: moneyStatus,
            Items: Array.Empty<ShopItemInfo>(),
            ErrorMessage: $"Unknown shopId '{shopId}'. Available game shops: [{available}]."
        );
    }

    private readonly Dictionary<string, IReadOnlyList<TileCoordinate>> _shopCounters = new(StringComparer.OrdinalIgnoreCase);

    public void SetShopCounterTiles(string shopId, IEnumerable<TileCoordinate> tiles)
    {
        _shopCounters[shopId] = tiles.ToList();
        _worldRevision++;
    }

    public IReadOnlyList<TileCoordinate> GetShopCounterTiles(string shopId, string? preferredLocation = null)
    {
        if (_shopCounters.TryGetValue(shopId, out var tiles))
        {
            return tiles;
        }
        return Array.Empty<TileCoordinate>();
    }

    public ShopActionMetadata GetShopActionMetadata(string shopId, string? preferredLocation = null)
    {
        var tiles = GetShopCounterTiles(shopId, preferredLocation);
        bool found = tiles.Count > 0;
        return new ShopActionMetadata(
            LocationName: preferredLocation ?? CurrentLocationName,
            OwnerArea: found ? new Microsoft.Xna.Framework.Rectangle(tiles[0].X, tiles[0].Y - 1, 1, 1) : null,
            OpenTime: 900,
            CloseTime: 1700,
            ActionFound: found,
            CounterTiles: tiles
        );
    }

    // ------------------------------------------------------------------
    // Agricultural / husbandry observation (controllable test data).
    // ------------------------------------------------------------------

    private readonly List<MachineScanInfo> _machines = new();
    private readonly List<GroundItemScanInfo> _groundItems = new();
    private readonly List<TileCoordinate> _refillTiles = new();
    private readonly HashSet<TileCoordinate> _fertilized = new();
    private LivestockScanInfo _livestock = new(
        Array.Empty<AnimalBuildingScanInfo>(),
        Array.Empty<AnimalScanInfo>(),
        false,
        false
    );

    public void SetMachines(IEnumerable<MachineScanInfo> machines)
    {
        _machines.Clear();
        _machines.AddRange(machines);
        _worldRevision++;
    }

    public void SetGroundItems(IEnumerable<GroundItemScanInfo> items)
    {
        _groundItems.Clear();
        _groundItems.AddRange(items);
        _worldRevision++;
    }

    public void SetRefillTiles(IEnumerable<TileCoordinate> tiles)
    {
        _refillTiles.Clear();
        _refillTiles.AddRange(tiles);
        _worldRevision++;
    }

    public void SetFertilized(TileCoordinate tile, bool fertilized)
    {
        if (fertilized) _fertilized.Add(tile);
        else _fertilized.Remove(tile);
        _worldRevision++;
    }

    public void SetLivestock(LivestockScanInfo livestock)
    {
        _livestock = livestock;
        _worldRevision++;
    }

    public IReadOnlyList<MachineScanInfo> ScanMachines(string locationName, int maxMachines = 64)
    {
        if (!string.Equals(CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
            return Array.Empty<MachineScanInfo>();
        return _machines.Take(maxMachines).ToList();
    }

    public LivestockScanInfo ScanLivestock(string locationName, int maxBuildings = 12, int maxAnimals = 60) => _livestock;

    public IReadOnlyList<TileCoordinate> FindWaterRefillTiles(
        string locationName,
        TileCoordinate center,
        int radius,
        int maxTiles = 16)
    {
        if (!string.Equals(CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
            return Array.Empty<TileCoordinate>();
        return _refillTiles
            .Where(t => Math.Abs(t.X - center.X) <= radius && Math.Abs(t.Y - center.Y) <= radius)
            .Take(maxTiles)
            .ToList();
    }

    public bool HasFertilizer(string locationName, TileCoordinate tile) => _fertilized.Contains(tile);

    public IReadOnlyList<GroundItemScanInfo> ScanGroundItems(
        string locationName,
        TileCoordinate center,
        int radius,
        int maxItems = 64)
    {
        if (!string.Equals(CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
            return Array.Empty<GroundItemScanInfo>();
        return _groundItems
            .Where(i => Math.Abs(i.Tile.X - center.X) <= radius && Math.Abs(i.Tile.Y - center.Y) <= radius)
            .Take(maxItems)
            .ToList();
    }

    public List<ChoppableTreeScanInfo> ChoppableTrees { get; } = new();

    public IReadOnlyList<ChoppableTreeScanInfo> ScanChoppableTrees(
        string locationName,
        TileCoordinate center,
        int radius,
        int maxItems = 64)
    {
        if (!string.Equals(CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
            return Array.Empty<ChoppableTreeScanInfo>();
        return ChoppableTrees
            .Where(i => Math.Abs(i.Tile.X - center.X) <= radius && Math.Abs(i.Tile.Y - center.Y) <= radius)
            .Take(maxItems)
            .ToList();
    }
}

