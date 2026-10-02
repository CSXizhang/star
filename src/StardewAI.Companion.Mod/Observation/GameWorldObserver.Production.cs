using StardewValley;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Observation;

public sealed partial class GameWorldObserver
{
    public bool ProductionSignalsTruncated { get; private set; }
    private int _productionSignalTime = -1;
    private List<ProductionSignal> _productionSignals = new();

    private static bool HasActiveMachineInput(StardewValley.Object machine) =>
        // lastInputItem persists after collection and only describes the previous
        // recipe. Current output, processing time or readiness prove occupancy.
        machine.heldObject?.Value is not null || machine.MinutesUntilReady > 0
        || machine.readyForHarvest?.Value == true;

    private static GameLocation RequireProductionLocation(string name)
    {
        var location = Game1.getLocationFromName(name);
        if (location is null || !string.Equals(location.NameOrUniqueName, name, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException($"Requested location '{name}' is not loaded.");
        return location;
    }

    public Dictionary<string, object> InspectMachines(string locationName)
    {
        if (!IsMainThread) throw new InvalidOperationException("Production observation requires the game thread.");
        RequireProductionLocation(locationName);
        var scan = ScanMachines(locationName, 257);
        _productionSignalTime = -1;
        return new() { ["locationId"] = locationName, ["truncated"] = scan.Count > 256,
            ["items"] = scan.Take(256).Select(m => new MachineSnapshot(new(m.Tile.X,m.Tile.Y),m.ItemId,m.Name,m.IsReady,m.MinutesUntilReady,
                m.OutputItemId,m.OutputName,m.OutputStack,m.OutputQuality,m.LastInputItemId,m.HasInput)).ToList(), ["capturedRevision"] = WorldRevision };
    }

    public Dictionary<string, object> InspectProduction(string locationName, IFarmerActor actor)
    {
        if (!IsMainThread) throw new InvalidOperationException("Production observation requires the game thread.");
        var location = RequireProductionLocation(locationName);
        var layer = location.Map?.Layers.FirstOrDefault() ?? throw new InvalidOperationException("Map is not loaded.");
        int width = layer.LayerWidth, height = layer.LayerHeight;
        var center = new TileCoordinate(width / 2, height / 2);
        // Scan the whole requested map, including indoor eggs. Bounded facts, explicit truncation.
        var ground = ScanGroundItems(locationName, center, Math.Max(width,height),257);
        var water = FindWaterRefillTiles(locationName, center, Math.Max(width,height),33);
        var foods = actor.GameFarmer?.Items.OfType<StardewValley.Object>()
            .Where(i => i.Stack > 0 && i.Edibility >= 0 && i.QualifiedItemId != "(O)434" && i.staminaRecoveredOnConsumption() > 0)
            .Select(i => new { itemId=i.QualifiedItemId, name=i.DisplayName, stack=i.Stack,
                staminaRecovered=i.staminaRecoveredOnConsumption(), healthRecovered=i.healthRecoveredOnConsumption() }).ToList();
        return new() { ["locationId"]=locationName, ["capturedRevision"]=WorldRevision,
            ["groundItems"]=ground.Take(256).Select(g=> new GroundItemSnapshot(new(g.Tile.X,g.Tile.Y),g.Kind,g.ItemId,g.Name,g.Stack,g.IsDropped,g.IsWeed,g.CanBeGrabbed,g.ClearTool,g.IsStone,g.IsTwig,g.Quality)).ToList(),
            ["groundItemsTruncated"]=ground.Count>256, ["waterRefillTiles"]=water.Take(32).Select(t=>new TileCoord(t.X,t.Y)).ToList(),
            ["waterRefillTilesTruncated"]=water.Count>32, ["waterRefillScope"]="map", ["waterRefillMapComplete"]=true,
            ["foods"]=(object?)foods ?? Array.Empty<object>(), ["inventoryOwner"]="companion" };
    }

    public void TrackProductionMachine(string locationName, TileCoordinate tile) => _productionSignalTime = -1;

    public IReadOnlyList<ProductionSignal> GetProductionSignals()
    {
        if (!IsMainThread) return _productionSignals;
        int key = Game1.Date.TotalDays * 3000 + Game1.timeOfDay;
        if (key == _productionSignalTime) return _productionSignals;
        var result = new List<ProductionSignal>();
        var seen = new HashSet<GameLocation>();
        void Visit(GameLocation location)
        {
            if (!seen.Add(location)) return;
            foreach (var pair in location.objects.Pairs)
                if (pair.Value.readyForHarvest.Value && pair.Value.heldObject.Value is not null && pair.Value.GetMachineData() is not null)
                    result.Add(new(location.NameOrUniqueName,new((int)pair.Key.X,(int)pair.Key.Y),true));
            foreach (var building in location.buildings)
                if (building.GetIndoors() is { } indoors) Visit(indoors);
        }
        foreach (var location in Game1.locations) Visit(location);
        // Ready flags alone survive reload and are small; no full machine inventories or countdown churn.
        ProductionSignalsTruncated = result.Count > 256;
        _productionSignals = result.OrderBy(s=>s.LocationId,StringComparer.Ordinal).ThenBy(s=>s.Tile.Y).ThenBy(s=>s.Tile.X).Take(256).ToList();
        _productionSignalTime = key;
        return _productionSignals;
    }
}
