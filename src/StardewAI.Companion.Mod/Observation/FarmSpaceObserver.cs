using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.TerrainFeatures;

namespace StardewAI.Companion.Mod.Observation;

/// <summary>Read-only, uncropped map facts. Placement remains item-specific and is checked at execution.</summary>
internal static class FarmSpaceObserver
{
    // Native warp landing coordinates may themselves be triggers or occupied.
    // Report a nearby checked standing point; this is geometry, not an unlock
    // claim. Execution still validates the entire native route and actual arrival.
    public static Domain.TileCoordinate? FindStandingTile(IWorldObserver observer, string location, Domain.TileCoordinate landing)
    {
        if (!observer.LocationExists(location)) return null;
        for (int radius = 0; radius <= 2; radius++)
            for (int dy = -radius; dy <= radius; dy++)
                for (int dx = -radius; dx <= radius; dx++)
                {
                    if (Math.Abs(dx) + Math.Abs(dy) != radius) continue;
                    var tile = new Domain.TileCoordinate(landing.X + dx, landing.Y + dy);
                    if (tile.X >= 0 && tile.Y >= 0 && !observer.IsWarpOrDoorTile(location, tile)
                        && observer.IsTilePassable(location, tile)) return tile;
                }
        return null;
    }

    public static Dictionary<string, object> Capture(GameLocation location, IWorldObserver observer, Rectangle? requestedRegion = null)
    {
        var layer = location.Map?.Layers.FirstOrDefault()
            ?? throw new InvalidOperationException("Location has no loaded map.");
        int mapWidth = layer.LayerWidth, mapHeight = layer.LayerHeight;
        var region = requestedRegion ?? new Rectangle(0, 0, mapWidth, mapHeight);
        if (region.Width < 1 || region.Height < 1 || region.X < 0 || region.Y < 0
            || (long)region.X + region.Width > mapWidth || (long)region.Y + region.Height > mapHeight)
            throw new ArgumentException("Region must be a positive rectangle inside the loaded map.");
        int width = region.Width, height = region.Height;
        if ((long)width * height > 65536)
            throw new InvalidOperationException("Map exceeds the spatial observation limit (65536 tiles); no truncated map returned.");
        var occupants = new List<object>();
        foreach (var pair in location.objects.Pairs)
            occupants.Add(new { kind = pair.Value is StardewValley.Objects.Chest { giftbox.Value: true } ? "giftbox" : "object", itemId = pair.Value.QualifiedItemId, name = pair.Value.DisplayName,
                nativeAction = pair.Value is StardewValley.Objects.Chest { giftbox.Value: true } ? "navigate adjacent, open_chest transfers native gift to inventory; advance ordinary dialogue, check inventory. pickup_items only if actual dropped items remain; not clear_debris" : null,
                x = (int)pair.Key.X, y = (int)pair.Key.Y, width = 1, height = 1 });
        foreach (var pair in location.terrainFeatures.Pairs)
            occupants.Add(new { kind = pair.Value.GetType().Name,
                itemId = pair.Value is Flooring floor ? floor.GetData()?.ItemId : null,
                x = (int)pair.Key.X, y = (int)pair.Key.Y, width = 1, height = 1 });
        foreach (var furniture in location.furniture)
        {
            var box = furniture.GetBoundingBox();
            occupants.Add(new { kind = "furniture", itemId = furniture.QualifiedItemId, name = furniture.DisplayName,
                x = box.X / 64, y = box.Y / 64, width = Math.Max(1, box.Width / 64), height = Math.Max(1, box.Height / 64) });
        }
        foreach (var building in location.buildings)
        {
            var door = building.getPointForHumanDoor();
            occupants.Add(new { kind = "building", id = building.id.Value.ToString(), name = building.buildingType.Value,
                x = building.tileX.Value, y = building.tileY.Value, width = building.tilesWide.Value, height = building.tilesHigh.Value,
                entrance = new { x = door.X, y = door.Y },
                indoors = building.GetIndoors()?.NameOrUniqueName });
        }
        foreach (var feature in location.largeTerrainFeatures)
        {
            var box = feature.getBoundingBox();
            occupants.Add(new { kind = feature.GetType().Name, x = box.X / 64, y = box.Y / 64,
                width = Math.Max(1, box.Width / 64), height = Math.Max(1, box.Height / 64) });
        }
        foreach (var feature in location.resourceClumps)
        {
            var box = feature.getBoundingBox();
            occupants.Add(new { kind = "resource-clump", x = box.X / 64, y = box.Y / 64,
                width = Math.Max(1, box.Width / 64), height = Math.Max(1, box.Height / 64) });
        }
        var rows = new List<string>();
        for (int y = region.Top; y < region.Bottom; y++)
        {
            var row = new char[width];
            for (int x = region.Left; x < region.Right; x++)
            {
                var tile = new Domain.TileCoordinate(x, y);
                bool passable = observer.IsTilePassable(location.NameOrUniqueName, tile);
                bool occupied = location.objects.ContainsKey(new Vector2(x, y)) || location.terrainFeatures.ContainsKey(new Vector2(x, y));
                row[x - region.Left] = observer.IsWarpOrDoorTile(location.NameOrUniqueName, tile) ? 'E' : !passable ? '#' : occupied ? 'o' : '.';
            }
            rows.Add(new string(row));
        }
        return new Dictionary<string, object>
        {
            ["locationId"] = location.NameOrUniqueName, ["width"] = width, ["height"] = height,
            ["mapWidth"] = mapWidth, ["mapHeight"] = mapHeight, ["offset"] = new { x = region.X, y = region.Y },
            ["capturedRevision"] = observer.WorldRevision, ["rows"] = rows,
            ["legend"] = new Dictionary<string, string> { ["."] = "passable ground; item placement still requires native validation", ["o"] = "passable with object or terrain", ["#"] = "blocked", ["E"] = "entrance/warp: preserve access" },
            ["occupants"] = occupants.Where(o =>
            {
                var data = System.Text.Json.JsonSerializer.SerializeToElement(o);
                return region.Intersects(new Rectangle(data.GetProperty("x").GetInt32(), data.GetProperty("y").GetInt32(),
                    data.GetProperty("width").GetInt32(), data.GetProperty("height").GetInt32()));
            }).ToList(),
            // Native exits can sit just beyond the last drawable map tile.
            // A clipped tile grid must not erase public cross-map connectivity.
            ["warps"] = location.warps.Where(w => !w.npcOnly.Value).Select(w => {
                var standing = FindStandingTile(observer, w.TargetName, new Domain.TileCoordinate(w.TargetX,w.TargetY));
                return new { x=w.X,y=w.Y,targetLocation=w.TargetName,targetX=w.TargetX,targetY=w.TargetY,
                    standingX=standing?.X,standingY=standing?.Y,standingTileAvailable=standing.HasValue,
                    standingEvidence="native collision/trigger geometry; not an unlock or route-success claim" };
            }).ToList(),
            ["placementGuaranteed"] = false
        };
    }
}
