using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Navigation;

/// <summary>
/// Navigates the Mechanics Actor across passable tiles within the SAME map.
/// Enforces:
/// 1. Same-map movement only; cross-map movement is strictly forbidden.
/// 2. Collision-respecting pathfinding.
/// 3. Valid adjacent standing tile and facing direction for tool interaction.
/// 4. Stuck detection without teleport recovery.
/// </summary>
public sealed class SameMapNavigator
{
    private readonly IWorldObserver _observer;
    private const int MaxSearchDepth = 2500;

    public SameMapNavigator(IWorldObserver observer)
    {
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
    }

    public PathResult FindPathToInteract(
        IFarmerActor actor,
        string targetLocation,
        TileCoordinate targetTile,
        IReadOnlyCollection<TileCoordinate>? avoidTiles = null)
    {
        ArgumentNullException.ThrowIfNull(actor);
        return FindPathToInteract(new AuthoritativePose(actor.LocationName, actor.Tile, actor.Facing), targetLocation, targetTile, avoidTiles);
    }

    /// <summary>
    /// Finds a valid path from the actor's current pose to an adjacent standing tile
    /// facing the target tile.
    ///
    /// The obstacle set is identical to the one enforced during physical walking:
    /// native tile passability, dynamic player occupancy, and any explicitly
    /// reported blocked tiles from a previous replan (`avoidTiles`). The tile the
    /// actor already stands on is always exempt: it was reached legally and
    /// rejecting it would make every replan fail identically.
    /// </summary>
    public PathResult FindPathToInteract(
        AuthoritativePose currentPose,
        string targetLocation,
        TileCoordinate targetTile,
        IReadOnlyCollection<TileCoordinate>? avoidTiles = null)
    {
        ArgumentNullException.ThrowIfNull(currentPose);

        // Invariant: strictly same-map
        if (!string.Equals(currentPose.LocationName, targetLocation, StringComparison.OrdinalIgnoreCase))
        {
            return PathResult.Failed(
                $"Cross-map navigation forbidden: Actor is at '{currentPose.LocationName}' but target is at '{targetLocation}'.");
        }

        // Check if already standing adjacent and facing target
        if (currentPose.Tile.IsAdjacentTo(targetTile))
        {
            var requiredFacing = FacingDirectionExtensions.DirectionToAdjacent(currentPose.Tile, targetTile);
            if (requiredFacing.HasValue)
            {
                // Already at valid standing position
                return PathResult.Completed(new[]
                {
                    new PathStep(currentPose.Tile, requiredFacing.Value)
                });
            }
        }

        // Identify candidate adjacent standing positions
        var candidates = GetCandidateStandingPositions(targetLocation, targetTile, avoidTiles).ToList();
        if (candidates.Count == 0)
        {
            return PathResult.Failed($"Target tile {targetTile} has no passable adjacent standing positions.");
        }

        // If currently standing on one of the candidate tiles, just rotate
        var currentCandidate = candidates.FirstOrDefault(c => c.Tile == currentPose.Tile);
        if (currentCandidate != default)
        {
            return PathResult.Completed(new[]
            {
                new PathStep(currentPose.Tile, currentCandidate.Facing)
            });
        }

        // Search for shortest path among candidates using BFS
        IReadOnlyList<PathStep>? bestSteps = null;
        foreach (var candidate in candidates)
        {
            var path = FindPathInternal(currentPose.LocationName, currentPose.Tile, candidate.Tile, candidate.Facing, avoidTiles);
            if (path is not null)
            {
                if (bestSteps is null || path.Count < bestSteps.Count)
                {
                    bestSteps = path;
                }
            }
        }

        if (bestSteps is null)
        {
            return PathResult.Failed($"No passable path found from {currentPose.Tile} to target adjacent positions for {targetTile}.");
        }

        return PathResult.Completed(bestSteps);
    }

    /// <summary>
    /// Finds a path between two passable tiles on the same map.
    /// </summary>
    public PathResult FindPath(
        string locationName,
        TileCoordinate startTile,
        TileCoordinate destinationTile,
        FacingDirection? finalFacing = null,
        IReadOnlyCollection<TileCoordinate>? avoidTiles = null)
    {
        if (startTile == destinationTile)
        {
            var facing = finalFacing ?? FacingDirection.Down;
            return PathResult.Completed(new[] { new PathStep(startTile, facing) });
        }

        var steps = FindPathInternal(locationName, startTile, destinationTile, finalFacing, avoidTiles);
        if (steps is null)
        {
            return PathResult.Failed($"No passable path from {startTile} to {destinationTile} on map '{locationName}'.");
        }

        return PathResult.Completed(steps);
    }

    private IReadOnlyList<PathStep>? FindPathInternal(
        string locationName,
        TileCoordinate start,
        TileCoordinate goal,
        FacingDirection? finalFacing,
        IReadOnlyCollection<TileCoordinate>? avoidTiles = null)
    {
        if (!IsTileNavigable(locationName, goal, avoidTiles) && start != goal)
        {
            return null;
        }

        var queue = new Queue<TileCoordinate>();
        var parentMap = new Dictionary<TileCoordinate, TileCoordinate>();
        var visited = new HashSet<TileCoordinate>();

        queue.Enqueue(start);
        visited.Add(start);

        bool found = false;
        int explored = 0;

        while (queue.Count > 0 && explored < MaxSearchDepth * 10)
        {
            var current = queue.Dequeue();
            explored++;

            if (current == goal)
            {
                found = true;
                break;
            }

            foreach (var neighbor in current.CardinalNeighbors())
            {
                if (visited.Contains(neighbor))
                    continue;

                // Neighbor must be navigable (or the goal tile)
                if (neighbor == goal || IsTileNavigable(locationName, neighbor, avoidTiles))
                {
                    visited.Add(neighbor);
                    parentMap[neighbor] = current;
                    queue.Enqueue(neighbor);
                }
            }
        }

        if (!found)
            return null;

        // Reconstruct path
        var pathTiles = new List<TileCoordinate>();
        var curr = goal;
        while (curr != start)
        {
            pathTiles.Add(curr);
            curr = parentMap[curr];
        }
        pathTiles.Reverse();

        var steps = new List<PathStep>();
        var prev = start;
        for (int i = 0; i < pathTiles.Count; i++)
        {
            var nextTile = pathTiles[i];
            var dir = FacingDirectionExtensions.DirectionToAdjacent(prev, nextTile) ?? FacingDirection.Down;
            steps.Add(new PathStep(nextTile, dir));
            prev = nextTile;
        }

        // Apply final facing if specified
        if (finalFacing.HasValue && steps.Count > 0)
        {
            var last = steps[^1];
            steps[^1] = new PathStep(last.Tile, finalFacing.Value);
        }

        return steps;
    }

    private IEnumerable<(TileCoordinate Tile, FacingDirection Facing)> GetCandidateStandingPositions(
        string locationName,
        TileCoordinate target,
        IReadOnlyCollection<TileCoordinate>? avoidTiles = null)
    {
        // North candidate: stand at (X, Y - 1), face South (Down)
        var north = new TileCoordinate(target.X, target.Y - 1);
        if (IsTileNavigable(locationName, north, avoidTiles))
            yield return (north, FacingDirection.Down);

        // East candidate: stand at (X + 1, Y), face West (Left)
        var east = new TileCoordinate(target.X + 1, target.Y);
        if (IsTileNavigable(locationName, east, avoidTiles))
            yield return (east, FacingDirection.Left);

        // South candidate: stand at (X, Y + 1), face North (Up)
        var south = new TileCoordinate(target.X, target.Y + 1);
        if (IsTileNavigable(locationName, south, avoidTiles))
            yield return (south, FacingDirection.Up);

        // West candidate: stand at (X - 1, Y), face East (Right)
        var west = new TileCoordinate(target.X - 1, target.Y);
        if (IsTileNavigable(locationName, west, avoidTiles))
            yield return (west, FacingDirection.Right);
    }

    /// <summary>
    /// Single source of truth for the navigation obstacle set. Planning and
    /// physical walking must agree: native passability, dynamic player occupancy,
    /// and any explicitly avoided (recently blocked) tiles.
    /// </summary>
    private bool IsTileNavigable(
        string locationName,
        TileCoordinate tile,
        IReadOnlyCollection<TileCoordinate>? avoidTiles)
    {
        if (avoidTiles is not null && avoidTiles.Contains(tile))
            return false;
        if (!_observer.IsTilePassable(locationName, tile))
            return false;
        if (_observer.IsPlayerOnTile(locationName, tile))
            return false;
        return true;
    }

    /// <summary>
    /// Searches for a passable and reachable standing tile within a small radius around a center tile.
    /// Used as fallback when the target destination tile itself is impassable (e.g. building footprint/door).
    /// </summary>
    public (TileCoordinate Tile, PathResult Path)? FindNearestPassableReachableTile(
        string locationName,
        TileCoordinate startTile,
        TileCoordinate centerTile,
        int maxRadius = 3,
        Func<TileCoordinate, bool>? isTileExcluded = null)
    {
        var candidates = new List<(TileCoordinate Tile, float Distance)>();

        for (int dx = -maxRadius; dx <= maxRadius; dx++)
        {
            for (int dy = -maxRadius; dy <= maxRadius; dy++)
            {
                if (dx == 0 && dy == 0) continue;
                float dist = MathF.Sqrt(dx * dx + dy * dy);
                if (dist > maxRadius + 0.5f) continue;

                var tile = new TileCoordinate(centerTile.X + dx, centerTile.Y + dy);
                if (tile.X >= 0 && tile.Y >= 0 && IsTileNavigable(locationName, tile, null))
                {
                    if (isTileExcluded != null && isTileExcluded(tile))
                        continue;
                    candidates.Add((tile, dist));
                }
            }
        }

        // Sort candidates: closest to centerTile first; tie-breaker: closest to startTile
        var sortedCandidates = candidates
            .OrderBy(c => c.Distance)
            .ThenBy(c => Math.Abs(c.Tile.X - startTile.X) + Math.Abs(c.Tile.Y - startTile.Y))
            .Select(c => c.Tile)
            .ToList();

        foreach (var candidate in sortedCandidates)
        {
            if (startTile == candidate)
            {
                return (candidate, PathResult.Completed(new[] { new PathStep(candidate, FacingDirection.Down) }));
            }

            var path = FindPath(locationName, startTile, candidate);
            if (path.Success && path.Steps.Count > 0)
            {
                return (candidate, path);
            }
        }

        return null;
    }
}
