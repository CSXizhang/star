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
    private const long MaxNoProgressTicks = 7200;
    private const int VerificationSettleTicks = 3;

    private readonly IFarmerActor _actor;
    private readonly IWorldObserver _observer;
    private readonly SameMapNavigator _navigator;
    private readonly IWorldMapGraph _mapGraph;
    private readonly ReachableRoutePlanner _routePlanner;
    private IReadOnlyList<MapEdge> _plannedEdges = Array.Empty<MapEdge>();
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
    private long _replanAfterTick;
    private Vector2? _nativeWalkingPoint;
    private TileCoordinate? _nativeWalkingTile;
    private int _nativeStallTicks;
    private float _nativeBestDistance = float.PositiveInfinity;
    private int _nativeNoProgressTicks;

    private readonly List<string> _visitedLocations = new();
    private readonly List<NavigationHopRecord> _hopRecords = new();
    private float _staminaUsed;
    private int _waterUsed;
    private long _elapsedTicks;
    private readonly ExecutionProgressWatchdog _progressWatchdog = new();
    private readonly WalkingProgress _walkingProgress = new();
    private int _startClock;
    private int? _pauseStartedClock;
    private int _pausedGameMinutes;
    private bool _targetAdjusted;
    private TileCoordinate? _effectiveTargetTile;
    private int _verifyingTicks;

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
    public Dictionary<string, object> PreviewRoute(string location, TileCoordinate target)
    {
        var route = _routePlanner.Find(_actor.LocationName, _actor.Tile, location, target,
            _observer.TimeOfDay, _actor.GameFarmer, out var failure, requireExactDestination: ActorGuards.IsPlayerBound(_actor));
        var result = new Dictionary<string, object>
        {
            ["reachable"] = route is not null,
            ["exactArrivalRequired"] = ActorGuards.IsPlayerBound(_actor),
            ["nativeMovementGeometry"] = ActorGuards.IsPlayerBound(_actor) ? "standing-center-v2" : "avatar-grid-v1",
            ["nativeUiClear"] = (!Game1.eventUp || ActorGuards.FestivalFreeRoam) && Game1.activeClickableMenu == null,
            ["nativeCollisionChecked"] = ActorGuards.IsPlayerBound(_actor) && location==_actor.LocationName,
            ["collisionEvidenceScope"] = location==_actor.LocationName ? "current-location-native-pathfinding-no-effects" : "cross-map-geometry-not-a-native-unlock-proof",
            ["observedTime"] = _observer.TimeOfDay,
            ["origin"] = new { locationId = _actor.LocationName, tile = _actor.Tile },
            ["destination"] = new { locationId = location, tile = target },
            ["estimateBasis"] = "Normal 60 FPS walking and 7-second game clock; excludes model deliberation, supplies and service time. Recheck service availability at dispatch."
        };
        if (route is null)
        {
            result["reason"] = failure?.Message ?? "No reachable route";
            result["reasonCode"] = failure?.ReasonCode ?? "NO_ROUTE";
        }
        else
        {
            result["locations"] = new[] { _actor.LocationName }.Concat(route.Edges.Select(e => e.TargetLocation)).ToArray();
            result["walkingTiles"] = route.WalkingTiles;
            result["estimatedGameMinutes"] = (int)Math.Ceiling(route.WalkingTiles * 16d / 60d * 10d / 7d);
            result["arrivalTile"] = route.TargetTile;
        }
        return result;
    }
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
        _routePlanner = new ReachableRoutePlanner(_observer, _navigator, _mapGraph);
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
            _plannedEdges = Array.Empty<MapEdge>();
            _visitedLocations.Clear();
            _hopRecords.Clear();
            _currentLegIndex = 0;
            _currentExitEdge = null;
            _currentPath.Clear();
            _currentPathIndex = 0;
            _replanCount = 0;
            _replanAfterTick = 0;
            _nativeWalkingTile = null;
            _nativeWalkingPoint = null;
            _nativeStallTicks = 0;
            _nativeNoProgressTicks = 0;
            _nativeBestDistance = float.PositiveInfinity;
            _staminaUsed = 0f;
            _waterUsed = 0;
            _elapsedTicks = 0;
            _progressWatchdog.Reset();
            _walkingProgress.Reset();
            _startClock = _observer.TimeOfDay;
            _pauseStartedClock = null;
            _pausedGameMinutes = 0;
            _cancelRequested = false;
            _cancelReason = null;
            _pauseRequested = false;
            _targetAdjusted = false;
            _effectiveTargetTile = null;
            _verifyingTicks = 0;
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

            if (request.MaxGameMinutes < 0)
            {
                CurrentState = ExecutionState.Rejected;
                _actor.SetActiveTask(null);
                earlyTerminalResult = FinishExecution(ExecutionState.Rejected,
                    $"Invalid maxGameMinutes: {request.MaxGameMinutes}. Must be non-negative; 0 means no time limit.", "INVALID_PARAMETERS");
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
                if (_pauseStartedClock is { } pauseClock)
                    _pausedGameMinutes += IWorldObserver.CalculateGameMinutesElapsed(pauseClock, _observer.TimeOfDay);
                _pauseStartedClock = null;
                _pauseRequested = false;
                _locationRoute.Clear();
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

            if (_cancelRequested)
            {
                FinishExecution(ExecutionState.Cancelled, _cancelReason ?? "Cancelled by request.", "CANCELLED");
                return;
            }
            if (IsPaused) return;

            if (_pauseRequested && CurrentState is ExecutionState.Navigating or ExecutionState.Preparing or ExecutionState.Verifying)
            {
                _pauseRequested = false;
                CurrentState = ExecutionState.Paused;
                _pauseStartedClock = _observer.TimeOfDay;
                _actor.Halt();
                _avatar?.SetAnimation("idle");
                NotifyPlayer("AI Companion: Navigation paused.");
                return;
            }

            _elapsedTicks++;

            if (_progressWatchdog.HasTimedOut(_elapsedTicks, MaxNoProgressTicks, _actor, CurrentTargetIndex))
            {
                Log($"navigate-to no-progress timeout exceeded ({MaxNoProgressTicks} ticks). Terminating.", LogLevel.Warn);
                FinishExecution(ExecutionState.Failed, "Execution made no progress within the stall timeout.", "TIMEOUT");
                return;
            }

            // The execution budget counts active work; result resources continue
            // to report the actual game-clock duration including player pauses.
            int elapsedMinutes = Math.Max(0,
                IWorldObserver.CalculateGameMinutesElapsed(_startClock, _observer.TimeOfDay) - _pausedGameMinutes);
            if (_currentRequest is not null && _currentRequest.MaxGameMinutes > 0 && elapsedMinutes > _currentRequest.MaxGameMinutes)
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
        if (_elapsedTicks < _replanAfterTick) return;
        if (TryVerifyArrival()) return;

        // If route not yet calculated, find route across maps
        if (_locationRoute.Count == 0)
        {
            string startLoc = _actor.LocationName;
            string targetLoc = _currentRequest!.LocationId;

            var route = _routePlanner.Find(startLoc, _actor.Tile, targetLoc, _currentRequest.TargetTile,
                _observer.TimeOfDay, _actor.GameFarmer, out var routeFailure, requireExactDestination: ActorGuards.IsPlayerBound(_actor));
            if (route == null)
            {
                // Same-map requests keep the established DESTINATION_UNREACHABLE code;
                // cross-map failures surface the planner's structured classification
                // (exit-side vs arrival-side blockage, coordinates, blocking tiles).
                bool sameMap = string.Equals(startLoc, targetLoc, StringComparison.OrdinalIgnoreCase);
                string failureCode;
                string failureMessage;
                if (sameMap)
                {
                    failureCode = "DESTINATION_UNREACHABLE";
                    failureMessage = routeFailure?.Message ?? $"No route found between '{startLoc}' and '{targetLoc}'.";
                }
                else if (routeFailure is not null)
                {
                    failureCode = routeFailure.ReasonCode;
                    failureMessage = routeFailure.Message;
                }
                else
                {
                    failureCode = "NO_ROUTE";
                    failureMessage = $"No route found between '{startLoc}' and '{targetLoc}'.";
                }
                FinishExecution(ExecutionState.Failed, failureMessage, failureCode, routeFailure?.Details);
                return;
            }

            _plannedEdges = route.Edges;
            _locationRoute = new[] { startLoc }.Concat(route.Edges.Select(e => e.TargetLocation)).ToList();
            _currentLegIndex = 0;
            if (_visitedLocations.Count == 0) _visitedLocations.Add(_actor.LocationName);

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
            if (TryVerifyArrival()) return;
            var requestedTile = _currentRequest!.TargetTile;
            var pathResult = _routePlanner.FindDestinationPath(_actor.LocationName, _actor.Tile, requestedTile, requireExactDestination: ActorGuards.IsPlayerBound(_actor));
            if (!pathResult.Success || pathResult.Steps.Count == 0)
            {
                string reason = pathResult.ErrorMessage ?? $"Destination {requestedTile} on '{_actor.LocationName}' is unreachable.";
                if (_replanCount > 0) RetryBlockedLeg(reason, "DESTINATION_UNREACHABLE");
                else FinishExecution(ExecutionState.Failed, reason, "DESTINATION_UNREACHABLE");
                return;
            }
            _effectiveTargetTile = pathResult.Steps[^1].Tile;
            _targetAdjusted = _effectiveTargetTile != requestedTile;

            _currentExitEdge = null;
            _currentPath = pathResult.Steps.ToList();
            _currentPathIndex = 0;
            CurrentState = ExecutionState.Navigating;
            return;
        }

        // Mid-route hop: find edge to next location
        string currentLoc = _locationRoute[_currentLegIndex];
        string nextLoc = _locationRoute[_currentLegIndex + 1];

        var candidateEdges = new[] { _plannedEdges[_currentLegIndex] };
        if (candidateEdges.Length == 0)
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
                firstUnavailableReason ?? $"Exit to '{nextLoc}' is unavailable.",
                "DESTINATION_UNREACHABLE_NOW", firstLockDetails);
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
            string reason = $"No passable path to the exit from '{currentLoc}' to '{nextLoc}'.";
            if (_replanCount > 0) RetryBlockedLeg(reason, "PATH_UNREACHABLE");
            else FinishExecution(ExecutionState.Failed, reason, "PATH_UNREACHABLE");
            return;
        }

        _currentExitEdge = bestEdge;
        _currentPath = bestSteps.ToList();
        _currentPathIndex = 0;
        CurrentState = ExecutionState.Navigating;
    }

    private bool TryVerifyArrival()
    {
        if (!string.Equals(_actor.LocationName, _currentRequest!.LocationId, StringComparison.OrdinalIgnoreCase)
            || _actor.Tile != (_effectiveTargetTile ?? _currentRequest.TargetTile)
            || IsWarpOrDoorTriggerTile(_actor.LocationName, _actor.Tile)) return false;
        _actor.Halt();
        _effectiveTargetTile ??= _actor.Tile;
        if (_visitedLocations.Count == 0) _visitedLocations.Add(_actor.LocationName);
        _verifyingTicks = 0;
        CurrentState = ExecutionState.Verifying;
        return true;
    }

    private void RetryBlockedLeg(string reason, string code)
    {
        Log($"navigate-to replan ({code}): {reason}", LogLevel.Warn);
        _actor.Halt();
        _nativeWalkingPoint = null;
        _nativeWalkingTile = null;
        _nativeStallTicks = 0;
        _nativeNoProgressTicks = 0;
        _nativeBestDistance = float.PositiveInfinity;
        if (++_replanCount > MaxReplansPerLeg)
        {
            FinishExecution(ExecutionState.Failed, reason, code);
            return;
        }
        _replanAfterTick = _elapsedTicks + 30 * (1 << (_replanCount - 1));
        CurrentState = ExecutionState.Preparing;
    }

    private void HandleNavigating()
    {
        if (ActorGuards.IsPlayerBound(_actor) && _currentExitEdge != null &&
            _actor.LocationName == _currentExitEdge.TargetLocation)
        {
            // Native warp tiles can transition before the grid path's final tick.
            // Never carry a source-map route into the new map.
            _actor.Halt();
            _pendingNativeWarp=null;
            _hopRecords.Add(new NavigationHopRecord(_currentExitEdge.SourceLocation,_currentPath.Count,_currentExitEdge));
            _currentLegIndex++;
            _replanCount=0; _replanAfterTick=0;
            _visitedLocations.Add(_actor.LocationName);
            PlanCurrentLeg();
            return;
        }
        if (_currentPathIndex >= _currentPath.Count)
        {
            _actor.Halt();
            bool isFinalLeg = _currentLegIndex >= _locationRoute.Count - 1;
            if (isFinalLeg)
            {
                _verifyingTicks = 0;
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
                if (transitionError == null && ActorGuards.IsPlayerBound(_actor)) return;
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
            _replanCount = 0;
            _replanAfterTick = 0;
            _visitedLocations.Add(_actor.LocationName);

            // Plan next leg on the newly arrived map
            PlanCurrentLeg();
            return;
        }

        var targetStep = _currentPath[_currentPathIndex];
        var targetTile = targetStep.Tile;
        if (!ActorGuards.IsPlayerBound(_actor) && _walkingProgress.IsBlocked(_actor, targetTile))
        {
            FinishExecution(ExecutionState.Failed,
                $"Walking blocked on '{_actor.LocationName}' at {_actor.Tile} toward {targetTile}; pixel={_actor.PixelPosition}. Inspect the obstruction before choosing clearance or another route.", "MOVEMENT_BLOCKED");
            return;
        }
        if (ActorGuards.IsPlayerBound(_actor))
        {
            // A native event/warp may relocate the player while preserving the map
            // name. BFS steps are adjacent: a distant next step is a stale path,
            // not a collision to retry for minutes against the same old waypoint.
            var currentTile = _actor.Tile;
            bool wrongMap = _currentLegIndex < _locationRoute.Count
                && !string.Equals(_actor.LocationName, _locationRoute[_currentLegIndex], StringComparison.OrdinalIgnoreCase);
            if (wrongMap || Math.Abs(currentTile.X - targetTile.X) + Math.Abs(currentTile.Y - targetTile.Y) > 1)
            {
                _locationRoute.Clear();
                _plannedEdges = Array.Empty<MapEdge>();
                _currentLegIndex = 0;
                _currentExitEdge = null;
                _pendingNativeWarp = null;
                _currentPath.Clear();
                _currentPathIndex = 0;
                RetryBlockedLeg("Native player pose changed outside the adjacent route; replanning from the actual position.", "NATIVE_ROUTE_CHANGED");
                return;
            }
            if ((Game1.eventUp && !ActorGuards.FestivalFreeRoam) || Game1.activeClickableMenu != null)
            {
                FinishExecution(ExecutionState.PartiallySucceeded,
                    "Native event/menu interrupted walking; inspect native UI and continue it through native-advance/native-answer before replanning.",
                    "NATIVE_UI_ACTIVE");
                return;
            }
            var point = _actor.PixelPosition;
            var distance = Vector2.Distance(point, ActorGuards.WalkingTargetPixel(_actor, targetTile));
            if (_nativeWalkingTile != targetTile || distance < _nativeBestDistance - 0.5f)
            {
                _nativeBestDistance = distance;
                _nativeNoProgressTicks = 0;
            }
            else
                _nativeNoProgressTicks++;
            // Native animation or oscillation can move the position without
            // approaching the waypoint. Bound that failure too, including a
            // farmer whose native CanMove gate remains closed without a menu.
            if (_nativeNoProgressTicks >= 180)
            {
                RetryBlockedLeg($"No native waypoint progress on '{_actor.LocationName}' from {_actor.Tile} toward {targetTile}; pixel={point}, distance={distance:F2}, best={_nativeBestDistance:F2}, canMove={_actor.GameFarmer?.CanMove}, usingTool={_actor.GameFarmer?.UsingTool}, heldDirection={(_actor as PlayerActor)?.HeldDirection}.", "NATIVE_MOVEMENT_STALLED");
                return;
            }
            if (_nativeWalkingTile == targetTile && _nativeWalkingPoint.HasValue
                && Vector2.Distance(point, _nativeWalkingPoint.Value) < 0.5f
                && _actor.GameFarmer?.CanMove == true)
                _nativeStallTicks++;
            else
                _nativeStallTicks = 0;
            _nativeWalkingPoint = point;
            _nativeWalkingTile = targetTile;
            if (_nativeStallTicks >= 90)
            {
                RetryBlockedLeg($"Native collision prevented walking from {_actor.Tile} toward {targetTile} on '{_actor.LocationName}'; inspect the obstruction or choose another route.", "NATIVE_COLLISION");
                return;
            }
        }
        // Native farmers have a bounding-box standing point rather than the legacy
        // avatar's exact grid origin. Arrival is the native tile actually reached.
        if (ActorGuards.HasReachedWalkingTile(_actor, targetTile))
        {
            _actor.Halt();
            _currentPathIndex++;
            return;
        }
        var targetPixel = ActorGuards.WalkingTargetPixel(_actor, targetTile);
        var currentPixel = _actor.PixelPosition;

        // Player obstruction avoidance
        if (_observer.IsPlayerOnTile(_actor.LocationName, targetTile)
            || !_observer.IsTilePassable(_actor.LocationName, targetTile))
        {
            // Give moving obstructions time to clear. Keep the route and retry
            // only this leg; pause/cancel are still checked on every update.
            RetryBlockedLeg($"Path obstructed on '{_actor.LocationName}' at {targetTile}.", "PATH_BLOCKED");
            return;
        }

        var diff = targetPixel - currentPixel;
        float dist = diff.Length();

        if (ActorGuards.IsPlayerBound(_actor))
        {
            _actor.MovePixels(diff.X, diff.Y);
            return;
        }
        if (dist <= WalkPixelsPerTick)
        {
            if (!ActorGuards.TryFinishWalkingStep(_actor, targetPixel)) return;
            _currentPathIndex++;
        }
        else
        {
            diff.Normalize();
            _actor.Facing = targetStep.Facing;
            _actor.MovePixels(diff.X * WalkPixelsPerTick, diff.Y * WalkPixelsPerTick);
        }
    }

    private string? _pendingNativeWarp;
    private bool PerformMapTransition(MapEdge edge, out string? error)
    {
        error = null;
        string targetLocName = edge.TargetLocation;
        if (!_routePlanner.TryArrival(edge, out TileCoordinate targetTile))
        {
            error = $"No passable landing tile for boundary into '{targetLocName}'.";
            return false;
        }

        if (ActorGuards.IsPlayerBound(_actor)) {
            if (_actor.LocationName == targetLocName) { _pendingNativeWarp=null; return true; }
            if (_pendingNativeWarp != targetLocName) {
                if ((Math.Abs(_actor.Tile.X-edge.SourceTile.X)+Math.Abs(_actor.Tile.Y-edge.SourceTile.Y)) > 1) { error="Not at native exit."; return false; }
                _pendingNativeWarp=targetLocName; _actor.SetLocation(targetLocName,targetTile);
            }
            return false; // Wait for native fade/map transition, never write actor coordinates.
        }
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

        if (gameFarmer != null && targetLoc != null)
        {
            gameFarmer.currentLocation = targetLoc;
            gameFarmer.Position = ActorGuards.WalkingTargetPixel(_actor, targetTile);
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
        if (!locMatches)
        {
            FinishExecution(ExecutionState.Failed,
                $"ARRIVAL_UNSTABLE: warped to '{_actor.LocationName}' at {_actor.Tile} (expected '{reqLoc}' at {expectedTile}).",
                "ARRIVAL_UNSTABLE");
            return;
        }

        bool tileMatches;
        if (ActorGuards.IsPlayerBound(_actor))
        {
            tileMatches = _actor.Tile == reqTile && !IsWarpOrDoorTriggerTile(_actor.LocationName, _actor.Tile);
        }
        else if (_targetAdjusted)
        {
            tileMatches = (_actor.Tile == expectedTile || _actor.Tile.IsAdjacentTo(expectedTile)) &&
                          !IsWarpOrDoorTriggerTile(_actor.LocationName, _actor.Tile);
        }
        else
        {
            tileMatches = (_actor.Tile == expectedTile || _actor.Tile == reqTile || _actor.Tile.IsAdjacentTo(reqTile)) &&
                          !IsWarpOrDoorTriggerTile(_actor.LocationName, _actor.Tile);
        }

        if (!tileMatches)
        {
            FinishExecution(ExecutionState.Failed,
                $"ARRIVAL_UNSTABLE: drifted to {_actor.Tile} on '{_actor.LocationName}' (expected {expectedTile}).",
                "ARRIVAL_UNSTABLE");
            return;
        }

        _verifyingTicks++;
        if (_verifyingTicks < VerificationSettleTicks)
        {
            return;
        }

        if (_hopRecords.Count == 0 || _hopRecords[^1].Location != reqLoc)
        {
            _hopRecords.Add(new NavigationHopRecord(reqLoc, _currentPath.Count, null));
        }
        FinishExecution(ExecutionState.Succeeded, null, null);
    }

    private bool IsWarpOrDoorTriggerTile(string locationName, TileCoordinate tile)
    {
        if (_observer.IsWarpOrDoorTile(locationName, tile))
        {
            return true;
        }

        var edges = _mapGraph.GetOutgoingEdges(locationName);
        foreach (var edge in edges)
        {
            if (edge.SourceTile == tile && !string.Equals(edge.TargetLocation, locationName, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }

        return false;
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
        Dictionary<string, object>? lockDetails = null,
        Dictionary<string, object>? failureDetails = null)
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
            RequestedTile: _currentRequest?.TargetTile,
            FailureDetails: failureDetails
        );

        FinalResult = result;
        Log($"navigate-to finished: {terminalState} - final at '{_actor.LocationName}' {_actor.Tile}, hops={_hopRecords.Count}, visited={string.Join("->", _visitedLocations)}.");
        OnCompleted?.Invoke(result);
        return result;
    }
}
