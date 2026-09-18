using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Navigation;

public interface IWorldMapGraph
{
    IReadOnlyList<string>? FindLocationRoute(string startLocation, string targetLocation, out string? failureReason);
    IReadOnlyList<MapEdge> GetEdges(string sourceLocation, string targetLocation);
    IReadOnlyList<MapEdge> GetOutgoingEdges(string sourceLocation);
    bool CheckEdgeTraversable(
        MapEdge edge,
        int currentTime,
        Farmer? farmer,
        out string? failureReason,
        out Dictionary<string, object>? lockDetails);
    void InvalidateCache();
}

/// <summary>
/// World Map graph constructed dynamically at runtime from Game1.locations and each GameLocation's
/// actual warps, doors, and tile actions. Prohibits hardcoded static map connection tables.
/// Supports BFS route finding across locations and real-time edge availability evaluation.
/// </summary>
public sealed class WorldMapGraph : IWorldMapGraph
{
    private readonly Func<IEnumerable<GameLocation>> _locationsProvider;
    private readonly Action<string, LogLevel>? _log;
    private readonly List<MapEdge> _customEdges = new();
    private readonly object _cacheLock = new();
    private Dictionary<string, List<MapEdge>>? _cachedAdjacency;
    private int _cachedFingerprint = -1;

    public WorldMapGraph(
        Func<IEnumerable<GameLocation>>? locationsProvider = null,
        Action<string, LogLevel>? log = null)
    {
        _locationsProvider = locationsProvider ?? (() => Game1.locations ?? Enumerable.Empty<GameLocation>());
        _log = log;
    }

    private void Log(string message, LogLevel level = LogLevel.Info) => _log?.Invoke(message, level);

    public void InvalidateCache()
    {
        lock (_cacheLock)
        {
            _cachedAdjacency = null;
            _cachedFingerprint = -1;
        }
    }

    /// <summary>
    /// Registers a custom edge (primarily used in offline unit tests).
    /// </summary>
    public void AddEdge(MapEdge edge)
    {
        ArgumentNullException.ThrowIfNull(edge);
        lock (_cacheLock)
        {
            _customEdges.Add(edge);
            InvalidateCache();
        }
    }

    private static string? GetLocationName(GameLocation? loc)
    {
        if (loc == null) return null;
        return !string.IsNullOrWhiteSpace(loc.NameOrUniqueName)
            ? loc.NameOrUniqueName
            : loc.Name;
    }

    private GameLocation? ResolveLocation(string locationName)
    {
        if (string.IsNullOrWhiteSpace(locationName)) return null;

        try
        {
            var allLocs = _locationsProvider();
            if (allLocs != null)
            {
                foreach (var l in allLocs)
                {
                    if (l == null) continue;
                    string? name = GetLocationName(l);
                    if (string.Equals(name, locationName, StringComparison.OrdinalIgnoreCase) ||
                        string.Equals(l.Name, locationName, StringComparison.OrdinalIgnoreCase))
                    {
                        return l;
                    }

                    if (l.buildings != null)
                    {
                        foreach (var b in l.buildings)
                        {
                            if (b == null) continue;
                            var indoors = b.GetIndoors();
                            if (indoors != null)
                            {
                                string? inName = b.GetIndoorsName() ?? GetLocationName(indoors);
                                if (string.Equals(inName, locationName, StringComparison.OrdinalIgnoreCase) ||
                                    string.Equals(indoors.Name, locationName, StringComparison.OrdinalIgnoreCase))
                                {
                                    return indoors;
                                }
                            }
                        }
                    }
                }
            }
        }
        catch { }

        try
        {
            var loc = Game1.getLocationFromName(locationName);
            if (loc != null) return loc;
        }
        catch { }

        try
        {
            if (Game1.currentLocation != null)
            {
                string? currName = GetLocationName(Game1.currentLocation);
                if (string.Equals(currName, locationName, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(Game1.currentLocation.Name, locationName, StringComparison.OrdinalIgnoreCase))
                {
                    return Game1.currentLocation;
                }
            }
        }
        catch { }

        return null;
    }

    private int ComputeFingerprint(IReadOnlyList<GameLocation> locations)
    {
        unchecked
        {
            int hash = 17;
            lock (_cacheLock)
            {
                hash = hash * 31 + _customEdges.Count;
                foreach (var ce in _customEdges)
                {
                    hash = hash * 31 + (ce.SourceLocation?.GetHashCode(StringComparison.OrdinalIgnoreCase) ?? 0);
                    hash = hash * 31 + (ce.TargetLocation?.GetHashCode(StringComparison.OrdinalIgnoreCase) ?? 0);
                }
            }

            foreach (var loc in locations)
            {
                if (loc == null) continue;
                hash = hash * 31 + (loc.NameOrUniqueName?.GetHashCode(StringComparison.OrdinalIgnoreCase) ?? 0);
                hash = hash * 31 + (loc.Name?.GetHashCode(StringComparison.OrdinalIgnoreCase) ?? 0);

                if (loc.buildings != null)
                {
                    hash = hash * 31 + loc.buildings.Count;
                    foreach (var b in loc.buildings)
                    {
                        if (b == null) continue;
                        hash = hash * 31 + (b.buildingType?.Value?.GetHashCode(StringComparison.OrdinalIgnoreCase) ?? 0);
                        hash = hash * 31 + (b.GetIndoorsName()?.GetHashCode(StringComparison.OrdinalIgnoreCase) ?? 0);
                        hash = hash * 31 + b.daysOfConstructionLeft.Value;
                        hash = hash * 31 + (b.tileX?.Value ?? 0);
                        hash = hash * 31 + (b.tileY?.Value ?? 0);
                    }
                }

                if (loc.warps != null)
                {
                    hash = hash * 31 + loc.warps.Count;
                }
            }
            return hash;
        }
    }

    private Dictionary<string, List<MapEdge>> GetOrCreateAdjacency()
    {
        lock (_cacheLock)
        {
            List<GameLocation> locs;
            try
            {
                locs = (_locationsProvider() ?? Enumerable.Empty<GameLocation>()).ToList();
            }
            catch
            {
                locs = new List<GameLocation>();
            }

            int fp = ComputeFingerprint(locs);
            if (_cachedAdjacency != null && _cachedFingerprint == fp)
            {
                return _cachedAdjacency;
            }

            _cachedAdjacency = BuildAdjacency(locs);
            _cachedFingerprint = fp;
            return _cachedAdjacency;
        }
    }

    public IReadOnlyList<string>? FindLocationRoute(string startLocation, string targetLocation, out string? failureReason)
    {
        if (string.IsNullOrWhiteSpace(startLocation)) throw new ArgumentException("Start location cannot be null or empty.", nameof(startLocation));
        if (string.IsNullOrWhiteSpace(targetLocation)) throw new ArgumentException("Target location cannot be null or empty.", nameof(targetLocation));

        if (string.Equals(startLocation, targetLocation, StringComparison.OrdinalIgnoreCase))
        {
            failureReason = null;
            return new[] { startLocation };
        }

        var adjacency = GetOrCreateAdjacency();

        var queue = new Queue<string>();
        var parent = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var visited = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        queue.Enqueue(startLocation);
        visited.Add(startLocation);

        bool found = false;
        while (queue.Count > 0)
        {
            var current = queue.Dequeue();
            if (string.Equals(current, targetLocation, StringComparison.OrdinalIgnoreCase))
            {
                found = true;
                break;
            }

            if (adjacency.TryGetValue(current, out var edges))
            {
                foreach (var edge in edges)
                {
                    if (!visited.Contains(edge.TargetLocation))
                    {
                        visited.Add(edge.TargetLocation);
                        parent[edge.TargetLocation] = current;
                        queue.Enqueue(edge.TargetLocation);
                    }
                }
            }
        }

        if (!found)
        {
            failureReason = $"No route found between map '{startLocation}' and '{targetLocation}' in dynamic world graph.";
            return null;
        }

        var route = new List<string>();
        string? curr = targetLocation;
        while (curr != null)
        {
            route.Add(curr);
            if (!parent.TryGetValue(curr, out curr))
            {
                break;
            }
        }
        route.Reverse();

        failureReason = null;
        return route;
    }

    public IReadOnlyList<MapEdge> GetEdges(string sourceLocation, string targetLocation)
    {
        if (string.IsNullOrWhiteSpace(sourceLocation)) throw new ArgumentException("Source location cannot be null or empty.", nameof(sourceLocation));
        if (string.IsNullOrWhiteSpace(targetLocation)) throw new ArgumentException("Target location cannot be null or empty.", nameof(targetLocation));

        var outgoing = GetOutgoingEdges(sourceLocation);
        return outgoing
            .Where(e => string.Equals(e.TargetLocation, targetLocation, StringComparison.OrdinalIgnoreCase))
            .ToList();
    }

    public IReadOnlyList<MapEdge> GetOutgoingEdges(string sourceLocation)
    {
        if (string.IsNullOrWhiteSpace(sourceLocation)) throw new ArgumentException("Source location cannot be null or empty.", nameof(sourceLocation));

        var edges = new List<MapEdge>();

        // 1. Custom registered edges
        lock (_cacheLock)
        {
            foreach (var ce in _customEdges)
            {
                if (string.Equals(ce.SourceLocation, sourceLocation, StringComparison.OrdinalIgnoreCase))
                {
                    edges.Add(ce);
                }
            }
        }

        // 2. Discover dynamically from resolved GameLocation
        var loc = ResolveLocation(sourceLocation);
        if (loc != null)
        {
            DiscoverEdgesFromLocation(loc, edges);
        }

        return edges.Distinct().ToList();
    }

    private void DiscoverEdgesFromLocation(GameLocation loc, List<MapEdge> edges)
    {
        string locName = GetLocationName(loc) ?? loc.Name;
        if (string.IsNullOrWhiteSpace(locName)) return;

        // A. Real-time warps from loc.warps
        if (loc.warps != null)
        {
            foreach (var w in loc.warps)
            {
                if (w == null) continue;

                // Ignore NPC-only warps
                try
                {
                    if (w.npcOnly.Value) continue;
                }
                catch { }

                if (string.IsNullOrWhiteSpace(w.TargetName)) continue;

                edges.Add(new MapEdge(
                    SourceLocation: locName,
                    TargetLocation: w.TargetName,
                    SourceTile: new TileCoordinate(w.X, w.Y),
                    TargetTile: new TileCoordinate(w.TargetX, w.TargetY),
                    EdgeKind: MapEdgeKind.Warp
                ));
            }
        }

        // B. Real-time doors and tile actions from loc.doors
        if (loc.doors != null)
        {
            foreach (var pair in loc.doors.Pairs)
            {
                var doorPt = pair.Key;
                var sourceTile = new TileCoordinate(doorPt.X, doorPt.Y);

                // Inspect tile property Action for LockedDoorWarp or Warp
                string? actionStr = null;
                try
                {
                    actionStr = loc.doesTileHaveProperty(doorPt.X, doorPt.Y, "Action", "Buildings");
                    if (string.IsNullOrWhiteSpace(actionStr))
                    {
                        actionStr = loc.doesTileHaveProperty(doorPt.X, doorPt.Y, "Action", "Back");
                    }
                }
                catch { }

                if (!string.IsNullOrWhiteSpace(actionStr))
                {
                    var tokens = actionStr.Split(' ', StringSplitOptions.RemoveEmptyEntries);
                    if (tokens.Length >= 6 && string.Equals(tokens[0], "LockedDoorWarp", StringComparison.OrdinalIgnoreCase))
                    {
                        // LockedDoorWarp <TargetX> <TargetY> <TargetLocation> <OpenTime> <CloseTime> [NpcName] [MinFriendship]
                        if (int.TryParse(tokens[1], out int tx) &&
                            int.TryParse(tokens[2], out int ty) &&
                            !string.IsNullOrWhiteSpace(tokens[3]) &&
                            int.TryParse(tokens[4], out int openTime) &&
                            int.TryParse(tokens[5], out int closeTime))
                        {
                            string? npc = tokens.Length > 6 ? tokens[6] : null;
                            int? minFriendship = tokens.Length > 7 && int.TryParse(tokens[7], out int mf) ? mf : null;

                            edges.Add(new MapEdge(
                                SourceLocation: locName,
                                TargetLocation: tokens[3],
                                SourceTile: sourceTile,
                                TargetTile: new TileCoordinate(tx, ty),
                                EdgeKind: MapEdgeKind.LockedDoorWarp,
                                OpenTime: openTime,
                                CloseTime: closeTime,
                                NpcName: npc,
                                MinFriendship: minFriendship
                            ));
                            continue;
                        }
                    }
                    else if (tokens.Length >= 4 && string.Equals(tokens[0], "Warp", StringComparison.OrdinalIgnoreCase))
                    {
                        // Warp <TargetX> <TargetY> <TargetLocation>
                        if (int.TryParse(tokens[1], out int tx) &&
                            int.TryParse(tokens[2], out int ty) &&
                            !string.IsNullOrWhiteSpace(tokens[3]))
                        {
                            edges.Add(new MapEdge(
                                SourceLocation: locName,
                                TargetLocation: tokens[3],
                                SourceTile: sourceTile,
                                TargetTile: new TileCoordinate(tx, ty),
                                EdgeKind: MapEdgeKind.Door
                            ));
                            continue;
                        }
                    }
                }

                // Fallback to getWarpFromDoor
                try
                {
                    var doorWarp = loc.getWarpFromDoor(doorPt, null);
                    if (doorWarp != null && !string.IsNullOrWhiteSpace(doorWarp.TargetName))
                    {
                        edges.Add(new MapEdge(
                            SourceLocation: locName,
                            TargetLocation: doorWarp.TargetName,
                            SourceTile: sourceTile,
                            TargetTile: new TileCoordinate(doorWarp.TargetX, doorWarp.TargetY),
                            EdgeKind: MapEdgeKind.Door
                        ));
                    }
                }
                catch { }
            }
        }

        // C. Real-time buildings and indoor transitions from loc.buildings
        if (loc.buildings != null)
        {
            foreach (var b in loc.buildings)
            {
                if (b == null) continue;

                // If under construction, human door is inaccessible
                try
                {
                    if (b.daysOfConstructionLeft.Value > 0) continue;
                }
                catch { }

                GameLocation? indoors = null;
                try
                {
                    indoors = b.GetIndoors();
                }
                catch { }

                if (indoors == null) continue;

                string indoorsName = b.GetIndoorsName() ?? GetLocationName(indoors) ?? indoors.Name;
                if (string.IsNullOrWhiteSpace(indoorsName)) continue;

                // Calculate human door tile on parent location
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

                var sourceDoorTile = new TileCoordinate(doorPt.X, doorPt.Y);

                // Target arrival tile inside the indoors:
                // Native Stardew Valley Building.doAction uses (indoors.warps[0].X, indoors.warps[0].Y - 1)
                // where warps[0] is the return warp to the parent location.
                TileCoordinate targetArrivalTile = new TileCoordinate(1, 1);
                try
                {
                    if (indoors.warps != null && indoors.warps.Count > 0)
                    {
                        Warp? returnWarp = indoors.warps.FirstOrDefault(w =>
                            w != null && (string.Equals(w.TargetName, locName, StringComparison.OrdinalIgnoreCase) ||
                                          string.Equals(w.TargetName, loc.Name, StringComparison.OrdinalIgnoreCase) ||
                                          string.Equals(w.TargetName, "Farm", StringComparison.OrdinalIgnoreCase)));
                        returnWarp ??= indoors.warps[0];

                        if (returnWarp != null)
                        {
                            targetArrivalTile = new TileCoordinate(returnWarp.X, Math.Max(0, returnWarp.Y - 1));
                        }
                    }
                }
                catch { }

                edges.Add(new MapEdge(
                    SourceLocation: locName,
                    TargetLocation: indoorsName,
                    SourceTile: sourceDoorTile,
                    TargetTile: targetArrivalTile,
                    EdgeKind: MapEdgeKind.Door
                ));
            }
        }
    }

    private Dictionary<string, List<MapEdge>> BuildAdjacency(IReadOnlyList<GameLocation> allLocs)
    {
        var adjacency = new Dictionary<string, List<MapEdge>>(StringComparer.OrdinalIgnoreCase);

        void AddEdgesForLoc(string locName, IEnumerable<MapEdge> edges)
        {
            if (string.IsNullOrWhiteSpace(locName)) return;
            if (!adjacency.TryGetValue(locName, out var list))
            {
                list = new List<MapEdge>();
                adjacency[locName] = list;
            }
            list.AddRange(edges);
        }

        // Add custom edges
        lock (_cacheLock)
        {
            foreach (var ce in _customEdges)
            {
                AddEdgesForLoc(ce.SourceLocation, new[] { ce });
            }
        }

        // Add dynamically discovered edges from all game locations and their buildings
        try
        {
            var processed = new HashSet<GameLocation>();

            void ProcessLocation(GameLocation loc)
            {
                if (loc == null || !processed.Add(loc)) return;

                string locName = GetLocationName(loc) ?? loc.Name;
                if (string.IsNullOrWhiteSpace(locName)) return;

                var edges = new List<MapEdge>();
                DiscoverEdgesFromLocation(loc, edges);
                AddEdgesForLoc(locName, edges);
                if (!string.IsNullOrWhiteSpace(loc.Name) && !string.Equals(loc.Name, locName, StringComparison.OrdinalIgnoreCase))
                {
                    AddEdgesForLoc(loc.Name, edges);
                }

                // Recursively ensure all building interiors are discovered and added to graph
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

                        var indoors = b.GetIndoors();
                        if (indoors != null)
                        {
                            ProcessLocation(indoors);
                        }
                    }
                }
            }

            foreach (var loc in allLocs)
            {
                ProcessLocation(loc);
            }
        }
        catch
        {
            // Headless safe guard
        }

        return adjacency;
    }

    public bool CheckEdgeTraversable(
        MapEdge edge,
        int currentTime,
        Farmer? farmer,
        out string? failureReason,
        out Dictionary<string, object>? lockDetails)
    {
        ArgumentNullException.ThrowIfNull(edge);

        failureReason = null;
        lockDetails = null;

        // 1. Target location must be resolvable in game context (if Game1 is available)
        try
        {
            var targetLoc = Game1.getLocationFromName(edge.TargetLocation);
            if (targetLoc != null)
            {
                // Event or festival cutscenes lock map traversal
                if (targetLoc.currentEvent != null || Game1.eventUp)
                {
                    failureReason = $"Map '{edge.TargetLocation}' currently has an active event or cutscene.";
                    lockDetails = new Dictionary<string, object>
                    {
                        ["reason"] = "event-active",
                        ["sourceLocation"] = edge.SourceLocation,
                        ["targetLocation"] = edge.TargetLocation
                    };
                    return false;
                }

                // Check festival store closures
                if (edge.EdgeKind == MapEdgeKind.LockedDoorWarp && GameLocation.AreStoresClosedForFestival())
                {
                    failureReason = $"Stores on '{edge.SourceLocation}' are closed today for a festival.";
                    lockDetails = new Dictionary<string, object>
                    {
                        ["reason"] = "festival-stores-closed",
                        ["sourceLocation"] = edge.SourceLocation,
                        ["targetLocation"] = edge.TargetLocation
                    };
                    return false;
                }
            }
        }
        catch
        {
            // Headless test safe guard
        }

        // 2. Real-time time lock evaluation for LockedDoorWarp
        if (edge.EdgeKind == MapEdgeKind.LockedDoorWarp && edge.OpenTime.HasValue && edge.CloseTime.HasValue)
        {
            bool hasTownKey = false;
            try
            {
                hasTownKey = farmer?.HasTownKey == true || Game1.player?.HasTownKey == true;
            }
            catch { }

            if (!hasTownKey)
            {
                int open = edge.OpenTime.Value;
                int close = edge.CloseTime.Value;
                bool withinOperatingHours = currentTime >= open && currentTime < close;

                bool friendshipUnlocked = false;
                if (!withinOperatingHours && !string.IsNullOrWhiteSpace(edge.NpcName) && edge.MinFriendship.HasValue && edge.MinFriendship.Value > 0)
                {
                    try
                    {
                        var friendships = farmer?.friendshipData ?? Game1.player?.friendshipData;
                        if (friendships != null && friendships.TryGetValue(edge.NpcName, out var fr) && fr.Points >= edge.MinFriendship.Value)
                        {
                            friendshipUnlocked = true;
                        }
                    }
                    catch { }
                }

                if (!withinOperatingHours && !friendshipUnlocked)
                {
                    failureReason = $"Door from '{edge.SourceLocation}' to '{edge.TargetLocation}' is locked. Operating hours: {open:D4}-{close:D4}, current game time: {currentTime:D4}.";
                    lockDetails = new Dictionary<string, object>
                    {
                        ["reason"] = "door-locked-hours",
                        ["sourceLocation"] = edge.SourceLocation,
                        ["targetLocation"] = edge.TargetLocation,
                        ["doorTile"] = new Dictionary<string, object> { ["x"] = edge.SourceTile.X, ["y"] = edge.SourceTile.Y },
                        ["openTime"] = open,
                        ["closeTime"] = close,
                        ["currentTime"] = currentTime,
                        ["npcName"] = edge.NpcName ?? "",
                        ["minFriendship"] = edge.MinFriendship ?? 0
                    };
                    return false;
                }
            }
        }

        return true;
    }
}
