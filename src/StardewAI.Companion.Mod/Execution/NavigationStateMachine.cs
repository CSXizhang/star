using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>
/// Tick-driven execution state machine for the "navigate-to" skill.
/// Orchestrates cross-map movement by finding topological routes across locations,
/// walking continuously on each map using genuine collision-respecting mechanics,
/// verifying operating hours and lock conditions at each boundary, and transitioning
/// the companion Mechanics Actor across maps without touching Game1.player.
/// </summary>
public sealed class NavigationStateMachine : ISkillExecutionMachine
{
    private const float WalkPixelsPerTick = 4f;
    private const int MaxReplansPerLeg = 3;
    private const long MaxMonotonicTicks = 7200;

    private readonly IFarmerActor _actor;
    private readonly IWorldObserver _observer;
    private readonly SameMapNavigator _navigator;
    private readonly IWorldMapGraph _mapGraph;
    private readonly CompanionAvatar? _avatar;
    private readonly Action<string, LogLevel>? _log;

    private readonly object _stateLock = new();

    private NavigationRequest? _currentRequest;
    private List<string> _locationRoute = new();
    private int _currentLegIndex;
    private MapEdge? _currentExitEdge;
    private List<PathStep> _currentPath = new();
    private int _currentPathIndex;
    private int _replanCount;

    private readonly List<string> _visitedLocations = new();
    private readonly List<NavigationHopRecord> _hopRecords = new();
    private float _staminaUsed;
    private int _waterUsed;
    private long _elapsedTicks;
    private int _startClock;
    private bool _targetAdjusted;
    private TileCoordinate? _effectiveTargetTile;

    private volatile bool _cancelRequested;
    private volatile string? _cancelReason;
    private volatile bool _pauseRequested;

    public ExecutionState CurrentState { get; private set; } = ExecutionState.Created;
    public bool IsExecuting => CurrentState is ExecutionState.Validating or ExecutionState.Preparing
        or ExecutionState.Navigating or ExecutionState.Verifying
        or ExecutionState.Paused or ExecutionState.Cancelling;
    public bool IsPaused => CurrentState == ExecutionState.Paused;
    public NavigationResult? FinalResult { get; private set; }

    public NavigationRequest? CurrentRequest => _currentRequest;
    public string? ActiveTaskId => _currentRequest?.TaskId;
    public int TotalTargets => _locationRoute.Count;
    public int CurrentTargetIndex => _currentLegIndex;

    public event Action<NavigationResult>? OnCompleted;
    public event Action<string>? OnNotification;

    private void NotifyPlayer(string message) => OnNotification?.Invoke(message);

    public NavigationStateMachine(
        IFarmerActor actor,
        IWorldObserver observer,
        SameMapNavigator navigator,
        IWorldMapGraph mapGraph,
        CompanionAvatar? avatar = null,
        Action<string, LogLevel>? log = null)
    {
        _actor = actor ?? throw new ArgumentNullException(nameof(actor));
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _navigator = navigator ?? throw new ArgumentNullException(nameof(navigator));
        _mapGraph = mapGraph ?? throw new ArgumentNullException(nameof(mapGraph));
        _avatar = avatar;
        _log = log;
    }

    private void Log(string message, LogLevel level = LogLevel.Info) => _log?.Invoke(message, level);

    public bool Start(NavigationRequest request, out NavigationResult? earlyTerminalResult)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_stateLock)
        {
            _currentRequest = request;
            _locationRoute.Clear();
            _visitedLocations.Clear();
            _hopRecords.Clear();
            _currentLegIndex = 0;
            _currentExitEdge = null;
            _currentPath.Clear();
            _currentPathIndex = 0;
            _replanCount = 0;
            _staminaUsed = 0f;
            _waterUsed = 0;
            _elapsedTicks = 0;
            _startClock = _observer.TimeOfDay;
            _cancelRequested = false;
            _cancelReason = null;
            _pauseRequested = false;
            _targetAdjusted = false;
            _effectiveTargetTile = null;
            FinalResult = null;

            _actor.SetActiveTask(request.TaskId);
            CurrentState = ExecutionState.Validating;

            if (string.IsNullOrWhiteSpace(request.LocationId))
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected, "Target locationId cannot be empty.", "INVALID_PARAMETERS");
                return false;
            }

            if (request.MaxGameMinutes < 1)
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    $"Invalid maxGameMinutes: {request.MaxGameMinutes}. Must be at least 1.", "INVALID_PARAMETERS");
                return false;
            }

            // Check if target location exists
            bool locExists = false;
            try
            {
                if (Game1.getLocationFromName(request.LocationId) != null)
                {
                    locExists = true;
                }
            }
            catch { }

            if (!locExists)
            {
                if (string.Equals(request.LocationId, _actor.LocationName, StringComparison.OrdinalIgnoreCase) ||
                    _mapGraph.GetOutgoingEdges(request.LocationId).Count > 0 ||
                    _mapGraph.GetEdges(_actor.LocationName, request.LocationId).Count > 0)
                {
                    locExists = true;
                }
            }

            if (!locExists)
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    $"Target location '{request.LocationId}' cannot be resolved.", "UNKNOWN_LOCATION");
                return false;
            }

            CurrentState = ExecutionState.Preparing;
            Log($"navigate-to accepted: target '{request.LocationId}' at {request.TargetTile} (commandId={request.CommandId}, task={request.TaskId}).");

            earlyTerminalResult = null;
            return true;
        }
    }

    public void RequestPause()
    {
        lock (_stateLock)
        {
            if (IsExecuting && !IsPaused)
            {
                _pauseRequested = true;
                Log($"navigate-to pause requested (task={ActiveTaskId}).");
            }
        }
    }

    public void Resume()
    {
        lock (_stateLock)
        {
            if (CurrentState == ExecutionState.Paused)
            {
                _pauseRequested = false;
                CurrentState = ExecutionState.Preparing;
                Log($"navigate-to resumed (task={ActiveTaskId}).");
            }
        }
    }

    public void RequestCancel(string reason = "Cancellation requested.")
    {
        lock (_stateLock)
        {
            if (IsExecuting && CurrentState is not (ExecutionState.Cancelling or ExecutionState.Cancelled))
            {
                _cancelRequested = true;
                _cancelReason = reason;
                Log($"navigate-to cancel requested: {reason} (task={ActiveTaskId}).");
            }
        }
    }

    public void Update(GameTime? time, long tickCount)
    {
        lock (_stateLock)
        {
            if (!IsExecuting) return;

            if (IsPaused) return;

            if (_pauseRequested && CurrentState is ExecutionState.Navigating or ExecutionState.Preparing)
            {
                _pauseRequested = false;
                CurrentState = ExecutionState.Paused;
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                NotifyPlayer("AI Companion: Navigation paused.");
                return;
            }

            if (_cancelRequested && CurrentState is not (ExecutionState.Cancelling or ExecutionState.Cancelled))
            {
                CurrentState = ExecutionState.Cancelling;
            }

            _elapsedTicks++;

            if (_elapsedTicks > MaxMonotonicTicks)
            {
                Log($"navigate-to monotonic timeout exceeded ({MaxMonotonicTicks} ticks). Terminating.", LogLevel.Warn);
                FinishExecution(ExecutionState.Failed, "Monotonic tick limit exceeded.", "TIMEOUT");
                return;
            }

            int elapsedMinutes = IWorldObserver.CalculateGameMinutesElapsed(_startClock, _observer.TimeOfDay);
            if (_currentRequest is not null && elapsedMinutes > _currentRequest.MaxGameMinutes)
            {
                Log($"navigate-to budget exceeded: {elapsedMinutes} > {_currentRequest.MaxGameMinutes} minutes.", LogLevel.Warn);
                FinishExecution(ExecutionState.PartiallySucceeded, "Game-clock minute budget exceeded.", "BUDGET_EXCEEDED");
                return;
            }

            switch (CurrentState)
            {
                case ExecutionState.Preparing:
                    HandlePreparing();
                    break;
                case ExecutionState.Navigating:
                    HandleNavigating();
                    break;
                case ExecutionState.Verifying:
                    HandleVerifying();
                    break;
                case ExecutionState.Cancelling:
                    FinishExecution(ExecutionState.Cancelled, _cancelReason ?? "Cancelled by request.", "CANCELLED");
                    break;
            }
        }
    }

    private void HandlePreparing()
    {
        if (CheckPauseOrCancel()) return;

        // If route not yet calculated, find route across maps
        if (_locationRoute.Count == 0)
        {
            string startLoc = _actor.LocationName;
            string targetLoc = _currentRequest!.LocationId;

            var route = _mapGraph.FindLocationRoute(startLoc, targetLoc, out var failureReason);
            if (route == null || route.Count == 0)
            {
                FinishExecution(ExecutionState.Failed,
                    failureReason ?? $"No route found between '{startLoc}' and '{targetLoc}'.",
                    "NO_ROUTE");
                return;
            }

            _locationRoute = route.ToList();
            _currentLegIndex = 0;
            _visitedLocations.Clear();
            _visitedLocations.Add(_actor.LocationName);

            Log($"navigate-to route resolved: {string.Join(" -> ", _locationRoute)}.");
        }

        PlanCurrentLeg();
    }

    private void PlanCurrentLeg()
    {
        if (CheckPauseOrCancel()) return;

        bool isFinalLeg = _currentLegIndex >= _locationRoute.Count - 1;
        if (isFinalLeg)
        {
            var requestedTile = _currentRequest!.TargetTile;
            TileCoordinate targetTile = requestedTile;
            PathResult? pathResult = null;
            _targetAdjusted = false;
            _effectiveTargetTile = requestedTile;

            // 1. If requested tile is passable, try direct path
            if (_observer.IsTilePassable(_actor.LocationName, requestedTile))
            {
                if (_actor.Tile == requestedTile)
                {
                    CurrentState = ExecutionState.Verifying;
                    return;
                }

                pathResult = _navigator.FindPath(_actor.LocationName, _actor.Tile, requestedTile);
                if (pathResult.Success && pathResult.Steps.Count > 0)
                {
                    targetTile = requestedTile;
                    _effectiveTargetTile = requestedTile;
                    _targetAdjusted = false;
                }
            }

            // 2. If requested tile is impassable or direct path failed, fallback to adjacent/nearest reachable tile
            if (pathResult == null || !pathResult.Success || pathResult.Steps.Count == 0)
            {
                // Check if actor is already at a passable tile adjacent to requested tile
                if (_actor.Tile.IsAdjacentTo(requestedTile) && _observer.IsTilePassable(_actor.LocationName, _actor.Tile))
                {
                    _effectiveTargetTile = _actor.Tile;
                    _targetAdjusted = true;
                    Log($"Actor already at adjacent passable tile {_actor.Tile} for impassable destination {requestedTile} (targetAdjusted=true).");
                    CurrentState = ExecutionState.Verifying;
                    return;
                }

                // Search for nearest reachable standing tile within small radius (1..3)
                var fallback = _navigator.FindNearestPassableReachableTile(
                    _actor.LocationName,
                    _actor.Tile,
                    requestedTile,
                    maxRadius: 3);

                if (fallback.HasValue)
                {
                    targetTile = fallback.Value.Tile;
                    pathResult = fallback.Value.Path;
                    _effectiveTargetTile = targetTile;
                    _targetAdjusted = true;
                    Log($"Destination tile {requestedTile} is impassable or unreachable on '{_actor.LocationName}'. Adjusted target to nearest reachable tile {targetTile} (targetAdjusted=true, steps={pathResult.Steps.Count}).");
                }
            }

            if (pathResult == null || !pathResult.Success || pathResult.Steps.Count == 0)
            {
                FinishExecution(ExecutionState.Failed,
                    $"No passable path found to destination tile {requestedTile} (or nearby reachable tiles) on map '{_actor.LocationName}'.",
                    "DESTINATION_UNREACHABLE");
                return;
            }

            _currentExitEdge = null;
            _currentPath = pathResult.Steps.ToList();
            _currentPathIndex = 0;
            _replanCount = 0;
            CurrentState = ExecutionState.Navigating;
            return;
        }

        // Mid-route hop: find edge to next location
        string currentLoc = _locationRoute[_currentLegIndex];
        string nextLoc = _locationRoute[_currentLegIndex + 1];

        var candidateEdges = _mapGraph.GetEdges(currentLoc, nextLoc);
        if (candidateEdges.Count == 0)
        {
            FinishExecution(ExecutionState.Failed,
                $"No outgoing warp or door found from '{currentLoc}' to '{nextLoc}'.",
                "NO_WARP");
            return;
        }

        // Pre-evaluate traversability
        var availableEdges = new List<MapEdge>();
        string? firstUnavailableReason = null;
        Dictionary<string, object>? firstLockDetails = null;

        foreach (var edge in candidateEdges)
        {
            if (_mapGraph.CheckEdgeTraversable(edge, _observer.TimeOfDay, _actor.GameFarmer, out var reason, out var lockDetails))
            {
                availableEdges.Add(edge);
            }
            else if (firstUnavailableReason == null)
            {
                firstUnavailableReason = reason;
                firstLockDetails = lockDetails;
            }
        }

        if (availableEdges.Count == 0)
        {
            FinishExecution(ExecutionState.Failed,
                firstUnavailableReason ?? $"All exits from '{currentLoc}' to '{nextLoc}' are unavailable.",
                "DESTINATION_UNREACHABLE_NOW",
                firstLockDetails);
            return;
        }

        // Find shortest path to any candidate edge
        IReadOnlyList<PathStep>? bestSteps = null;
        MapEdge? bestEdge = null;

        foreach (var edge in availableEdges)
        {
            PathResult pr;
            if (_observer.IsTilePassable(currentLoc, edge.SourceTile))
            {
                pr = _navigator.FindPath(currentLoc, _actor.Tile, edge.SourceTile);
            }
            else
            {
                pr = _navigator.FindPathToInteract(_actor, currentLoc, edge.SourceTile);
            }

            if (pr.Success && pr.Steps.Count > 0)
            {
                if (bestSteps == null || pr.Steps.Count < bestSteps.Count)
                {
                    bestSteps = pr.Steps;
                    bestEdge = edge;
                }
            }
        }

        if (bestSteps == null || bestEdge == null)
        {
            FinishExecution(ExecutionState.Failed,
                $"No passable path found on '{currentLoc}' to any warp leading to '{nextLoc}'.",
                "PATH_BLOCKED");
            return;
        }

        _currentExitEdge = bestEdge;
        _currentPath = bestSteps.ToList();
        _currentPathIndex = 0;
        _replanCount = 0;
        CurrentState = ExecutionState.Navigating;
    }

    private void HandleNavigating()
    {
        if (_currentPathIndex >= _currentPath.Count)
        {
            _actor.Halt();
            bool isFinalLeg = _currentLegIndex >= _locationRoute.Count - 1;
            if (isFinalLeg)
            {
                CurrentState = ExecutionState.Verifying;
                return;
            }

            // At exit warp/door
            if (_currentExitEdge == null)
            {
                FinishExecution(ExecutionState.Failed, "Missing exit edge definition for leg.", "NAVIGATION_ERROR");
                return;
            }

            // Re-evaluate edge traversability upon arrival
            int currentClock = _observer.TimeOfDay;
            if (!_mapGraph.CheckEdgeTraversable(_currentExitEdge, currentClock, _actor.GameFarmer, out var failReason, out var lockDetails))
            {
                FinishExecution(ExecutionState.Failed,
                    failReason ?? "Exit warp is currently unavailable.",
                    "DESTINATION_UNREACHABLE_NOW",
                    lockDetails);
                return;
            }

            // Perform map transition
            if (!PerformMapTransition(_currentExitEdge, out var transitionError))
            {
                FinishExecution(ExecutionState.Failed,
                    transitionError ?? "Map boundary transition failed.",
                    "TRANSITION_FAILED");
                return;
            }

            // Record hop
            _hopRecords.Add(new NavigationHopRecord(
                _currentExitEdge.SourceLocation,
                _currentPath.Count,
                _currentExitEdge
            ));

            _currentLegIndex++;
            _visitedLocations.Add(_actor.LocationName);

            // Plan next leg on the newly arrived map
            PlanCurrentLeg();
            return;
        }

        var targetStep = _currentPath[_currentPathIndex];
        var targetTile = targetStep.Tile;
        var targetPixel = new Vector2(targetTile.X * 64, targetTile.Y * 64);
        var currentPixel = _actor.PixelPosition;

        // Player obstruction avoidance
        if (_observer.IsPlayerOnTile(_actor.LocationName, targetTile))
        {
            _actor.Halt();
            _replanCount++;
            if (_replanCount > MaxReplansPerLeg)
            {
                FinishExecution(ExecutionState.Failed,
                    $"Path obstructed by player on '{_actor.LocationName}' at {targetTile}.",
                    "PATH_BLOCKED");
                return;
            }

            // Replan current leg
            PlanCurrentLeg();
            return;
        }

        var diff = targetPixel - currentPixel;
        float dist = diff.Length();

        if (dist <= WalkPixelsPerTick)
        {
            _actor.PixelPosition = targetPixel;
            _currentPathIndex++;
        }
        else
        {
            diff.Normalize();
            _actor.Facing = targetStep.Facing;
            _actor.MovePixels(diff.X * WalkPixelsPerTick, diff.Y * WalkPixelsPerTick);
        }
    }

    private bool PerformMapTransition(MapEdge edge, out string? error)
    {
        error = null;
        string targetLocName = edge.TargetLocation;
        TileCoordinate targetTile = edge.TargetTile;

        GameLocation? targetLoc = null;
        try
        {
            targetLoc = Game1.getLocationFromName(targetLocName);
        }
        catch { }

        // Conservative check: active event or cutscene
        if (targetLoc != null)
        {
            if (targetLoc.currentEvent != null || Game1.eventUp)
            {
                error = $"Cannot traverse map: event or cutscene is active on '{targetLocName}'.";
                return false;
            }
        }

        var gameFarmer = _actor.GameFarmer;
        var oldLoc = gameFarmer?.currentLocation;

        // Safe arrival tile check: if exact landing tile is impassable, search adjacent tiles
        if (!_observer.IsTilePassable(targetLocName, targetTile))
        {
            foreach (var neighbor in targetTile.CardinalNeighbors())
            {
                if (_observer.IsTilePassable(targetLocName, neighbor))
                {
                    targetTile = neighbor;
                    break;
                }
            }
        }

        if (gameFarmer != null && targetLoc != null)
        {
            gameFarmer.currentLocation = targetLoc;
            gameFarmer.Position = new Vector2(targetTile.X * 64, targetTile.Y * 64);
            gameFarmer.faceDirection((int)FacingDirection.Down);
        }

        _actor.SetLocation(targetLocName, targetTile);
        _actor.Halt();

        Log($"Companion traversed map boundary from '{edge.SourceLocation}' to '{targetLocName}' at tile {targetTile}.");
        return true;
    }

    private void HandleVerifying()
    {
        string reqLoc = _currentRequest!.LocationId;
        var reqTile = _currentRequest.TargetTile;
        var expectedTile = _effectiveTargetTile ?? reqTile;

        bool locMatches = string.Equals(_actor.LocationName, reqLoc, StringComparison.OrdinalIgnoreCase);
        bool tileMatches = _actor.Tile == expectedTile ||
                           _actor.Tile == reqTile ||
                           _actor.Tile.IsAdjacentTo(reqTile) ||
                           (_targetAdjusted && _actor.Tile.IsAdjacentTo(expectedTile));

        if (locMatches && tileMatches)
        {
            // Record final leg if not already recorded
            if (_hopRecords.Count == 0 || _hopRecords[^1].Location != reqLoc)
            {
                _hopRecords.Add(new NavigationHopRecord(reqLoc, _currentPath.Count, null));
            }
            FinishExecution(ExecutionState.Succeeded, null, null);
        }
        else
        {
            FinishExecution(ExecutionState.Failed,
                $"Verification failed: Companion at '{_actor.LocationName}' {_actor.Tile}, expected '{reqLoc}' {expectedTile} (requested: {reqTile}).",
                "LOCATION_MISMATCH");
        }
    }

    private bool CheckPauseOrCancel()
    {
        if (_cancelRequested)
        {
            CurrentState = ExecutionState.Cancelling;
            return true;
        }

        if (_pauseRequested)
        {
            CurrentState = ExecutionState.Paused;
            Log($"navigate-to paused at safe boundary (task={ActiveTaskId}).");
            return true;
        }

        return false;
    }

    private NavigationResult FinishExecution(
        ExecutionState terminalState,
        string? errorMessage,
        string? errorCode,
        Dictionary<string, object>? lockDetails = null)
    {
        CurrentState = terminalState;
        _actor.SetActiveTask(null);
        _actor.Halt();

        int elapsedMinutes = IWorldObserver.CalculateGameMinutesElapsed(_startClock, _observer.TimeOfDay);

        var result = new NavigationResult(
            CommandId: _currentRequest?.CommandId ?? "unknown",
            TaskId: _currentRequest?.TaskId ?? "unknown",
            FinalState: terminalState,
            TargetLocation: _currentRequest?.LocationId ?? "unknown",
            TargetTile: _effectiveTargetTile ?? (_currentRequest?.TargetTile ?? default),
            FinalLocation: _actor.LocationName,
            FinalTile: _actor.Tile,
            VisitedLocations: _visitedLocations.Distinct().ToList(),
            HopRecords: _hopRecords.ToList(),
            StaminaUsed: _staminaUsed,
            WaterUsed: _waterUsed,
            GameMinutesElapsed: elapsedMinutes,
            FinalWorldRevision: _observer.WorldRevision,
            ErrorMessage: errorMessage,
            ErrorCode: errorCode,
            LockDetails: lockDetails,
            TargetAdjusted: _targetAdjusted,
            RequestedTile: _currentRequest?.TargetTile
        );

        FinalResult = result;
        Log($"navigate-to finished: {terminalState} - final at '{_actor.LocationName}' {_actor.Tile}, hops={_hopRecords.Count}, visited={string.Join("->", _visitedLocations)}.");
        OnCompleted?.Invoke(result);
        return result;
    }
}
