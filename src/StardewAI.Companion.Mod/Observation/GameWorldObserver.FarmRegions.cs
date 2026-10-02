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
        var water = FindWaterRefillTiles(locationName, new TileCoordinate(width / 2, height / 2), Math.Max(width, height), width * height);
        var origin = actor.LocationName == locationName && passable.Contains(actor.Tile)
            ? actor.Tile : serviceTiles.FirstOrDefault(passable.Contains);
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
        for (int y = 0; y < height; y++)
        for (int x = 0; x < width; x++)
        {
            var t = new TileCoordinate(x, y);
            if (!FarmRegionPlanner.Neighbors(t).Any(fromOrigin.ContainsKey)) continue;
            var v = new Vector2(x, y);
            if (loc.objects.ContainsKey(v) || IsPlayerOnTile(locationName, t)) continue;
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
        var candidates = FarmRegionPlanner.Candidates(cells, reserved, fromOrigin, requested);
        var scan = ScanPlantingOptions(locationName, origin, 1, actor);
        var regions = candidates.Select(bounds =>
        {
            var inside = cells.Where(p => bounds.Contains(p.Key.X, p.Key.Y)).ToList();
            var available = inside.Where(p => !reserved.Contains(p.Key) && p.Value != "liveCrop" &&
                (hasScythe || p.Value is not ("deadCrop" or "grass"))).ToList();
            // Fill holes nearest existing soil, then extend continuously outward.
            var old = inside.Where(p => p.Value != "tillable" && p.Value != "grass").Select(p => p.Key).ToList();
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
                ["liveCropCount"] = inside.Count(p => p.Value == "liveCrop"),
                ["deadCropCount"] = inside.Count(p => p.Value == "deadCrop"),
                ["existingSoilCount"] = old.Count,
                ["plantingTiles"] = Tiles(ordered.Where(p => p.Value is "tilledEmpty" or "tillable")),
                ["tilledEmptyTiles"] = Tiles(ordered.Where(p => p.Value == "tilledEmpty")),
                ["tillableTiles"] = Tiles(ordered.Where(p => p.Value == "tillable")),
                ["clearWithScytheTiles"] = Tiles(ordered.Where(p => p.Value is "deadCrop" or "grass")),
                ["hoeAfterClearingTiles"] = Tiles(ordered.Where(p => p.Value == "grass")),
                ["clearingAction"] = new { operation = "cut_grass", location_id = locationName,
                    tilesField = "clearWithScytheTiles", reobserveAfter = true },
                ["missingClearTools"] = !hasScythe && inside.Any(p => p.Value == "deadCrop") ? new[] { "Scythe" } : Array.Empty<string>(),
                ["reservedPaths"] = reserved.Where(t => bounds.Contains(t.X, t.Y)).Select(t => new { x = t.X, y = t.Y }).ToList(),
                ["pathDistances"] = new { origin = Distance(fromOrigin), water = Distance(fromWater), chest = Distance(fromChest) },
                ["reachableInteraction"] = true
            };
        }).ToList();
        return new Dictionary<string, object>
        {
            ["locationId"] = locationName, ["season"] = scan.Season, ["dayOfMonth"] = scan.DayOfMonth,
            ["companionHasHoe"] = scan.CompanionHasHoe, ["seeds"] = scan.Seeds,
            ["regions"] = regions, ["regionLocked"] = requested.HasValue,
            ["pathOrigin"] = new { x = origin.X, y = origin.Y, isCompanion = actor.LocationName == locationName },
            ["layoutOrigin"] = new { x = layoutOrigin.X, y = layoutOrigin.Y },
            ["capturedRevision"] = WorldRevision,
            ["note"] = "Choose/persist one main region; re-query its bounds after clearing. Live crops and access paths are preserved. Distances are traversable paths, not straight-line estimates."
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
        Dictionary<TileCoordinate, int> paths, Rectangle? requested)
    {
        if (requested.HasValue) return new() { requested.Value };
        if (cells.Count == 0) return new();
        var usable = cells.Keys.Where(t => !reserved.Contains(t)).ToHashSet();
        var options = new List<(Rectangle Bounds, double Score)>();
        // A few practical regular shapes, without assuming any farm coordinates.
        foreach (var size in new[] { (8, 5), (8, 8), (12, 8) })
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
            options.Add((r, old * 4 + inside.Count - approach * .25));
        }
        var result = new List<Rectangle>();
        foreach (var option in options.OrderByDescending(o => o.Score))
        {
            if (result.Any(r => { var i = Rectangle.Intersect(r, option.Bounds); return i.Width * i.Height > Math.Min(r.Width * r.Height, option.Bounds.Width * option.Bounds.Height) / 2; })) continue;
            result.Add(option.Bounds);
            if (result.Count == 3) break;
        }
        return result;
    }
}
