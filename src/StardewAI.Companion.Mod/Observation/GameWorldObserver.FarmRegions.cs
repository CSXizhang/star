using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Objects;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Observation;

public sealed partial class GameWorldObserver
{
    // On demand only: whole-farm geometry is never put in routine model context.
    public Dictionary<string, object> InspectPlanting(string locationName, IFarmerActor actor, Rectangle? requested = null)
    {
        if (!IsMainThread) throw new InvalidOperationException("Planting inspection requires the game thread.");
        var loc = Game1.getLocationFromName(locationName)
            ?? throw new InvalidOperationException($"Location '{locationName}' is not loaded.");
        var layer = loc.Map?.Layers.FirstOrDefault() ?? throw new InvalidOperationException("Map unavailable.");
        int width = layer.LayerWidth, height = layer.LayerHeight;
        bool playerVisibleOnly=ActorGuards.IsPlayerBound(actor) && requested.HasValue;
        bool InObservedRegion(int x,int y) => !playerVisibleOnly || requested!.Value.Contains(x,y);
        if (requested is { } r && (r.X < 0 || r.Y < 0 || r.Width < 1 || r.Height < 1 || r.Right > width || r.Bottom > height))
            throw new ArgumentException("Farm region must be inside the map.");
        var passable = new HashSet<TileCoordinate>();
        var reserved = new HashSet<TileCoordinate>();
        var cells = new Dictionary<TileCoordinate, string>();
        var serviceTiles = new List<TileCoordinate>();
        for (int y = 0; y < height; y++)
        for (int x = 0; x < width; x++)
        {
            var t = new TileCoordinate(x, y);
            if (IsTilePassable(locationName, t)) passable.Add(t);
            if (IsWarpOrDoorTile(locationName, t))
            {
                serviceTiles.Add(t);
                reserved.Add(t);
                foreach (var n in FarmRegionPlanner.Neighbors(t)) reserved.Add(n);
            }
        }
        foreach (var b in loc.buildings)
        {
            var door = b.getPointForHumanDoor();
            var t = new TileCoordinate(door.X, door.Y);
            serviceTiles.Add(t);
            reserved.Add(t);
            reserved.Add(new TileCoordinate(door.X, door.Y + 1));
        }
        var chests = loc.objects.Pairs.Where(p => p.Value is Chest)
            .Select(p => new TileCoordinate((int)p.Key.X, (int)p.Key.Y)).ToList();
        var water = FindWaterRefillTiles(locationName, new TileCoordinate(width / 2, height / 2), Math.Max(width, height), width * height).ToArray();
        // Indoor farm planning must begin at the actual native exit into this
        // map, not whichever distant warp happened to be enumerated first.
        var directEntries = actor.GameFarmer?.currentLocation?.warps
            .Where(w => w.TargetName == locationName)
            .OrderBy(w => new TileCoordinate(w.X, w.Y).ManhattanDistanceTo(actor.Tile))
            .SelectMany(w => FarmRegionPlanner.Neighbors(new TileCoordinate(w.TargetX, w.TargetY))
                .Prepend(new TileCoordinate(w.TargetX, w.TargetY))).ToList() ?? new List<TileCoordinate>();
        bool atOriginMap = actor.LocationName == locationName && passable.Contains(actor.Tile);
        bool hasDirectEntry = !atOriginMap && directEntries.Any(passable.Contains);
        var origin = atOriginMap ? actor.Tile : hasDirectEntry
            ? directEntries.First(passable.Contains) : serviceTiles.FirstOrDefault(passable.Contains);
        if (!passable.Contains(origin)) throw new InvalidOperationException("No observed reachable farm entrance.");
        var fromOrigin = FarmRegionPlanner.Distances(passable, new[] { origin });
        var layoutOrigin = serviceTiles.FirstOrDefault(passable.Contains);
        if (!passable.Contains(layoutOrigin)) layoutOrigin = origin;
        var fromLayout = FarmRegionPlanner.Distances(passable, new[] { layoutOrigin });
        // Preserve actual approach paths to entrances and storage. Existing crops
        // remain observed assets even where a reserved path crosses old soil.
        foreach (var target in serviceTiles.Concat(chests))
        {
            var approach = FarmRegionPlanner.Neighbors(target).Append(target)
                .Where(fromLayout.ContainsKey).OrderBy(t => fromLayout[t]).ToList();
            if (approach.Count == 0) continue;
            var t = approach[0];
            while (fromLayout[t] > 0)
            {
                reserved.Add(t);
                t = FarmRegionPlanner.Neighbors(t).First(n => fromLayout.TryGetValue(n, out var d) && d == fromLayout[t] - 1);
            }
        }
        bool hasScythe = actor.GameFarmer?.Items.OfType<MeleeWeapon>().Any(t => t.isScythe()) == true;
        bool hasAxe = actor.GameFarmer?.Items.OfType<Axe>().Any() == true;
        bool hasPickaxe = actor.GameFarmer?.Items.OfType<Pickaxe>().Any() == true;
        bool hasHoe = actor.GameFarmer?.Items.OfType<Hoe>().Any() == true;
        for (int y = 0; y < height; y++)
        for (int x = 0; x < width; x++)
        {
            if (!InObservedRegion(x,y)) continue;
            var t = new TileCoordinate(x, y);
            if (!FarmRegionPlanner.Neighbors(t).Any(fromOrigin.ContainsKey)) continue;
            var v = new Vector2(x, y);
            bool playerOccupied = ActorGuards.IsPlayerBound(actor)
                ? Game1.getOnlineFarmers().Any(p => !ReferenceEquals(p, actor.GameFarmer)
                    && ReferenceEquals(p.currentLocation, loc) && (int)p.Tile.X == x && (int)p.Tile.Y == y)
                : IsPlayerOnTile(locationName, t);
            if (playerOccupied) continue; // The controlled farmer can move; other farmers remain reserved.
            if (loc.objects.TryGetValue(v, out var obj))
            {
                // Small removable debris is preparation, not a reason to hide
                // near-home land. Chests, forage, machines and clumps stay assets.
                if (obj.IsWeeds() && hasHoe) cells[t] = "weed";
                else if (obj.IsBreakableStone() && hasPickaxe) cells[t] = "stone";
                else if (obj.IsTwig() && hasAxe) cells[t] = "twig";
                continue;
            }
            if (loc.terrainFeatures.TryGetValue(v, out var f))
            {
                if (f is HoeDirt dirt)
                    cells[t] = dirt.crop is null ? "tilledEmpty" : dirt.crop.dead.Value ? "deadCrop" : "liveCrop";
                else if (f is Grass && hasScythe && passable.Contains(t) && loc.doesTileHaveProperty(x, y, "Diggable", "Back") != null) cells[t] = "grass";
            }
            else if (passable.Contains(t) && loc.doesTileHaveProperty(x, y, "Diggable", "Back") != null)
                cells[t] = "tillable";
        }
        var fromWater = FarmRegionPlanner.Distances(passable, water.SelectMany(t => FarmRegionPlanner.Neighbors(t)).Where(passable.Contains));
        var fromChest = FarmRegionPlanner.Distances(passable, chests.SelectMany(t => FarmRegionPlanner.Neighbors(t)).Where(passable.Contains));
        // The current actor/first enumerated warp is not necessarily the home
        // door. Publish native home access separately, without fixed coordinates.
        var homeEntrances = loc.warps.Where(w => w.TargetName == "FarmHouse")
            .Select(w => new TileCoordinate(w.X, w.Y))
            .Concat(Game1.getLocationFromName("FarmHouse")?.warps
                .Where(w => w.TargetName == locationName)
                .Select(w => new TileCoordinate(w.TargetX, w.TargetY)) ?? Enumerable.Empty<TileCoordinate>())
            .Distinct().ToArray();
        var fromHome = FarmRegionPlanner.Distances(passable,
            homeEntrances.SelectMany(t => FarmRegionPlanner.Neighbors(t).Append(t)).Where(passable.Contains));
        var candidates = FarmRegionPlanner.Candidates(cells, reserved, fromOrigin, requested, fromHome, fromWater);
        var scan = ScanPlantingOptions(locationName, origin, 1, actor);
        var regions = candidates.Select(bounds =>
        {
            var inside = cells.Where(p => bounds.Contains(p.Key.X, p.Key.Y)).ToList();
            var available = inside.Where(p => !reserved.Contains(p.Key) && p.Value != "liveCrop" &&
                (hasScythe || p.Value is not ("deadCrop" or "grass"))).ToList();
            // Fill holes nearest existing soil, then extend continuously outward.
            var old = inside.Where(p => p.Value is "tilledEmpty" or "deadCrop" or "liveCrop").Select(p => p.Key).ToList();
            var ordered = available.OrderBy(p => p.Value == "tilledEmpty" ? 0 : 1)
                .ThenBy(p => old.Count == 0 ? p.Key.ManhattanDistanceTo(new TileCoordinate(bounds.Center.X, bounds.Center.Y))
                    : old.Min(t => t.ManhattanDistanceTo(p.Key)))
                .ThenBy(p => p.Key.Y).ThenBy(p => p.Key.X).ToList();
            int? Distance(Dictionary<TileCoordinate, int> distances) => inside.SelectMany(p => FarmRegionPlanner.Neighbors(p.Key))
                .Where(distances.ContainsKey).Select(t => (int?)distances[t]).Min();
            object Tiles(IEnumerable<KeyValuePair<TileCoordinate, string>> rows) => rows.Select(p => new { x = p.Key.X, y = p.Key.Y }).ToList();
            return new Dictionary<string, object>
            {
                ["locationId"] = locationName,
                ["bounds"] = new { x = bounds.X, y = bounds.Y, width = bounds.Width, height = bounds.Height },
                ["usableCapacity"] = available.Count,
                ["tillableCount"] = ordered.Count(p => p.Value == "tillable"),
                ["tilledEmptyCount"] = ordered.Count(p => p.Value == "tilledEmpty"),
                ["requiresClearingCount"] = ordered.Count(p => p.Value is "deadCrop" or "grass" or "weed" or "stone" or "twig"),
                ["liveCropCount"] = inside.Count(p => p.Value == "liveCrop"),
                ["deadCropCount"] = inside.Count(p => p.Value == "deadCrop"),
                ["existingSoilCount"] = old.Count,
                ["plantingTiles"] = Tiles(ordered.Where(p => p.Value is "tilledEmpty" or "tillable")),
                ["tilledEmptyTiles"] = Tiles(ordered.Where(p => p.Value == "tilledEmpty")),
                ["tillableTiles"] = Tiles(ordered.Where(p => p.Value == "tillable")),
                ["clearWithScytheTiles"] = Tiles(ordered.Where(p => p.Value is "deadCrop" or "grass")),
                ["clearWithBasicToolsTiles"] = Tiles(ordered.Where(p => p.Value is "weed" or "stone" or "twig")),
                ["hoeAfterClearingTiles"] = Tiles(ordered.Where(p => p.Value is "grass" or "weed" or "stone" or "twig")),
                ["clearingAction"] = new { operation = ordered.Any(p => p.Value is "weed" or "stone" or "twig") ? "clear_debris" : "cut_grass", location_id = locationName,
                    tilesField = ordered.Any(p => p.Value is "weed" or "stone" or "twig") ? "clearWithBasicToolsTiles" : "clearWithScytheTiles", reobserveAfter = true },
                ["clearingActions"] = new object[] {
                    new { operation = "cut_grass", location_id = locationName, tilesField = "clearWithScytheTiles", reobserveAfter = true },
                    new { operation = "clear_debris", location_id = locationName, tilesField = "clearWithBasicToolsTiles", reobserveAfter = true }
                },
                ["missingClearTools"] = !hasScythe && inside.Any(p => p.Value == "deadCrop") ? new[] { "Scythe" } : Array.Empty<string>(),
                ["reservedPaths"] = reserved.Where(t => bounds.Contains(t.X, t.Y)).Select(t => new { x = t.X, y = t.Y }).ToList(),
                ["pathDistances"] = new { origin = Distance(fromOrigin), home = Distance(fromHome), water = Distance(fromWater), chest = Distance(fromChest) },
                ["reachableInteraction"] = true
            };
        }).ToList();
        return new Dictionary<string, object>
        {
            ["locationId"] = locationName, ["season"] = scan.Season, ["dayOfMonth"] = scan.DayOfMonth,
            ["companionHasHoe"] = scan.CompanionHasHoe, ["seeds"] = scan.Seeds,
            ["regions"] = regions, ["regionLocked"] = requested.HasValue,
            ["pathOrigin"] = new { locationId = locationName, x = origin.X, y = origin.Y, isCompanion = actor.LocationName == locationName,
                source = atOriginMap ? "current-actor" : hasDirectEntry ? "observed-current-map-native-exit" : "fallback-observed-service-entry" },
            ["layoutOrigin"] = new { locationId = locationName, x = layoutOrigin.X, y = layoutOrigin.Y },
            ["homeEntrances"] = homeEntrances.Select(t => new { locationId = locationName, x = t.X, y = t.Y, role = "exterior-house-access" }).ToArray(),
            ["capturedRevision"] = WorldRevision,
            ["note"] = "Choose/persist one main region; re-query its bounds after clearing. Live crops and access paths are preserved. Distances are traversable paths, not straight-line estimates. homeEntrances are EXTERIOR coordinates on locationId, never interior FarmHouse destinations."
        };
    }
}

public static class FarmRegionPlanner
{
    public static IEnumerable<TileCoordinate> Neighbors(TileCoordinate t)
    {
        yield return new(t.X - 1, t.Y); yield return new(t.X + 1, t.Y);
        yield return new(t.X, t.Y - 1); yield return new(t.X, t.Y + 1);
    }

    public static Dictionary<TileCoordinate, int> Distances(HashSet<TileCoordinate> passable, IEnumerable<TileCoordinate> starts)
    {
        var distances = new Dictionary<TileCoordinate, int>();
        var queue = new Queue<TileCoordinate>();
        foreach (var s in starts.Distinct().Where(passable.Contains)) { distances[s] = 0; queue.Enqueue(s); }
        while (queue.TryDequeue(out var t))
        foreach (var n in Neighbors(t))
            if (passable.Contains(n) && distances.TryAdd(n, distances[t] + 1)) queue.Enqueue(n);
        return distances;
    }

    public static List<Rectangle> Candidates(Dictionary<TileCoordinate, string> cells, HashSet<TileCoordinate> reserved,
        Dictionary<TileCoordinate, int> paths, Rectangle? requested,
        Dictionary<TileCoordinate, int>? homePaths = null, Dictionary<TileCoordinate, int>? waterPaths = null)
    {
        if (requested.HasValue) return new() { requested.Value };
        if (cells.Count == 0) return new();
        var usable = cells.Keys.Where(t => !reserved.Contains(t)).ToHashSet();
        var options = new List<(Rectangle Bounds, double Score)>();
        // A few practical regular shapes, without assuming any farm coordinates.
        foreach (var size in new[] { (5, 3), (4, 4), (8, 5), (8, 8), (12, 8) })
        for (int y = cells.Keys.Min(t => t.Y); y <= cells.Keys.Max(t => t.Y); y++)
        for (int x = cells.Keys.Min(t => t.X); x <= cells.Keys.Max(t => t.X); x++)
        {
            var r = new Rectangle(x, y, size.Item1, size.Item2);
            var inside = usable.Where(t => r.Contains(t.X, t.Y)).ToHashSet();
            if (inside.Count < r.Width * r.Height * .8) continue;
            // One connected field, with an observed approach for every crop tile.
            if (Distances(inside, inside.Take(1)).Count != inside.Count) continue;
            int old = inside.Count(t => cells[t] is "tilledEmpty" or "deadCrop" or "liveCrop");
            int approach = inside.SelectMany(Neighbors).Where(paths.ContainsKey).Select(t => paths[t]).DefaultIfEmpty(9999).Min();
            // Raw capacity otherwise suppresses every small starter field and
            // sends small farms to distant empty corners. Expose scale choices;
            // the model compares maintenance distances and chooses the field.
            int? ServiceDistance(Dictionary<TileCoordinate,int>? service) => service == null ? null :
                inside.SelectMany(Neighbors).Where(service.ContainsKey).Select(t => (int?)service[t]).Min();
            double maintenance = homePaths == null && waterPaths == null ? approach * .25 :
                approach * .025 + (ServiceDistance(homePaths) ?? 0) * .20 + (ServiceDistance(waterPaths) ?? 0) * .10;
            int clearing = inside.Count(t => cells[t] is "grass" or "weed" or "stone" or "twig" or "deadCrop");
            options.Add((r, (old * 4 + inside.Count) * 20.0 / (r.Width * r.Height) - maintenance - clearing * .05));
        }
        var result = new List<Rectangle>();
        foreach (var size in new[] { (5, 3), (4, 4), (8, 5), (8, 8), (12, 8) })
        {
            var sameSize = new List<Rectangle>();
            foreach (var option in options.Where(o => o.Bounds.Width == size.Item1 && o.Bounds.Height == size.Item2).OrderByDescending(o => o.Score))
            {
                if (sameSize.Any(r => { var i = Rectangle.Intersect(r, option.Bounds); return i.Width * i.Height > r.Width * r.Height / 2; })) continue;
                sameSize.Add(option.Bounds);
                result.Add(option.Bounds);
                if (sameSize.Count == 2) break;
            }
        }
        return result;
    }
}
