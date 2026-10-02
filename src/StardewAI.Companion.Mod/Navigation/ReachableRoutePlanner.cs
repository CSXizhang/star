using StardewValley;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Navigation;

public sealed record ReachableRoute(IReadOnlyList<MapEdge> Edges, int WalkingTiles, TileCoordinate TargetTile);

/// <summary>Routes through reachable entrance poses, including leaving and re-entering one map.</summary>
public sealed class ReachableRoutePlanner
{
    private readonly IWorldObserver _observer;
    private readonly SameMapNavigator _navigator;
    private readonly IWorldMapGraph _graph;

    public ReachableRoutePlanner(IWorldObserver observer, SameMapNavigator navigator, IWorldMapGraph graph)
    {
        _observer = observer;
        _navigator = navigator;
        _graph = graph;
    }

    private readonly record struct Entrance(string Location, TileCoordinate Tile);
    private static Entrance Key(string location, TileCoordinate tile) => new(location.ToUpperInvariant(), tile);

    public ReachableRoute? Find(string location, TileCoordinate start, string targetLocation,
        TileCoordinate target, int time, Farmer? farmer, out string? reason)
    {
        var origin = Key(location, start);
        var costs = new Dictionary<Entrance, int> { [origin] = 0 };
        var previous = new Dictionary<Entrance, (Entrance From, MapEdge Edge)>();
        var queue = new PriorityQueue<(Entrance State, string Name), int>();
        queue.Enqueue((origin, location), 0);
        ReachableRoute? best = null;
        string? unavailable = null;
        int explored = 0;
        while (queue.TryDequeue(out var node, out int cost) && explored++ < 256)
        {
            if (cost != costs[node.State]) continue;
            if (best is not null && cost >= best.WalkingTiles) break;
            if (string.Equals(node.Name, targetLocation, StringComparison.OrdinalIgnoreCase))
            {
                var final = FindDestinationPath(node.Name, node.State.Tile, target);
                if (final.Success)
                {
                    int total = cost + Math.Max(0, final.Steps.Count - 1);
                    if (best is null || total < best.WalkingTiles)
                    {
                        var edges = new List<MapEdge>();
                        var state = node.State;
                        while (previous.TryGetValue(state, out var back))
                        {
                            edges.Add(back.Edge);
                            state = back.From;
                        }
                        edges.Reverse();
                        best = new ReachableRoute(edges, total, final.Steps[^1].Tile);
                    }
                }
            }
            foreach (var edge in _graph.GetOutgoingEdges(node.Name))
            {
                if (!_graph.CheckEdgeTraversable(edge, time, farmer, out var blocked, out _))
                {
                    // Only a reachable entrance into the requested destination can
                    // explain its failure; locked side branches are irrelevant.
                    if (string.Equals(edge.TargetLocation, targetLocation, StringComparison.OrdinalIgnoreCase)
                        && FindExitPath(node.Name, node.State.Tile, edge).Success)
                        unavailable ??= blocked;
                    continue;
                }
                var walk = FindExitPath(node.Name, node.State.Tile, edge);
                if (!walk.Success || !TryArrival(edge, out var arrival)) continue;
                int nextCost = cost + Math.Max(0, walk.Steps.Count - 1) + 1;
                var next = Key(edge.TargetLocation, arrival);
                if (costs.TryGetValue(next, out int old) && old <= nextCost) continue;
                costs[next] = nextCost;
                previous[next] = (node.State, edge);
                queue.Enqueue((next, edge.TargetLocation), nextCost);
            }
        }
        reason = best is not null ? null : unavailable ?? $"No route found with passable entrance paths from '{location}' to '{targetLocation}'.";
        return best;
    }

    public PathResult FindExitPath(string location, TileCoordinate start, MapEdge edge) =>
        _observer.IsTilePassable(location, edge.SourceTile)
            ? _navigator.FindPath(location, start, edge.SourceTile)
            : _navigator.FindPathToInteract(new AuthoritativePose(location, start, FacingDirection.Down), location, edge.SourceTile);

    public bool TryArrival(MapEdge edge, out TileCoordinate tile)
    {
        tile = edge.TargetTile;
        foreach (var candidate in new[] { tile }.Concat(tile.CardinalNeighbors()))
        {
            if (!_observer.IsTilePassable(edge.TargetLocation, candidate) || _observer.IsPlayerOnTile(edge.TargetLocation, candidate)) continue;
            tile = candidate;
            return true;
        }
        return false;
    }

    public bool IsTrigger(string location, TileCoordinate tile) =>
        _observer.IsWarpOrDoorTile(location, tile) || _graph.GetOutgoingEdges(location).Any(e => e.SourceTile == tile);

    public PathResult FindDestinationPath(string location, TileCoordinate start, TileCoordinate target)
    {
        if (!IsTrigger(location, target) && _observer.IsTilePassable(location, target))
        {
            var exact = _navigator.FindPath(location, start, target);
            if (exact.Success) return exact;
        }
        if (start.IsAdjacentTo(target) && _observer.IsTilePassable(location, start) && !IsTrigger(location, start))
            return PathResult.Completed(new[] { new PathStep(start, FacingDirection.Down) });
        var nearby = _navigator.FindNearestPassableReachableTile(location, start, target, 3, t => IsTrigger(location, t));
        return nearby?.Path ?? PathResult.Failed($"Destination {target} on '{location}' is unreachable.");
    }
}
