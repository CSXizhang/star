using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class NavigationStateMachineTests
{
    [Fact]
    public void AlreadyAtDestinationVerifiesWithoutSearchingOrMoving()
    {
        var actor = new StubFarmerActor { LocationName = "Farm" };
        var observer = new SimulatedWorldObserver();
        var machine = new NavigationStateMachine(actor, observer, new SameMapNavigator(observer), new WorldMapGraph());
        var original = actor.Tile;
        Assert.True(machine.Start(new NavigationRequest("arrived", "task", "Farm", original, 120), out _));
        for (int tick = 1; tick < 10 && machine.IsExecuting; tick++) machine.Update(null, tick);
        Assert.Equal(ExecutionState.Succeeded, machine.FinalResult!.FinalState);
        Assert.Equal(original, actor.Tile);
        Assert.Equal(0, observer.TilePassabilityChecks);
    }

    [Theory]
    [InlineData("pause")]
    [InlineData("cancel")]
    [InlineData("clear")]
    [InlineData("blocked")]
    public void DynamicObstructionBacksOffWithoutRepeatingRouteSearchAndHonorsControls(string action)
    {
        var actor = new StubFarmerActor();
        actor.SetLocation("Farm", new(0, 1));
        var observer = new SimulatedWorldObserver();
        for (int x = 0; x <= 10; x++) observer.SetPassable("Farm", new(x, 1), true);
        var logs = new List<string>();
        var machine = new NavigationStateMachine(actor, observer, new SameMapNavigator(observer), new WorldMapGraph(),
            log: (text, _) => logs.Add(text));
        Assert.True(machine.Start(new NavigationRequest("blocked", "task", "Farm", new(10, 1), 120), out _));
        machine.Update(null, 1);
        observer.PlayerTile = new(1, 1);
        for (int tick = 2; tick <= 3; tick++) machine.Update(null, tick);
        Assert.Equal(ExecutionState.Preparing, machine.CurrentState);
        int before = observer.TilePassabilityChecks;
        var original = actor.Tile;
        for (int tick = 4; tick <= 10; tick++) machine.Update(null, tick);
        Assert.Equal(before, observer.TilePassabilityChecks);
        Assert.Equal(original, actor.Tile);
        if (action == "pause")
        {
            machine.RequestPause();
            machine.Update(null, 11);
            Assert.True(machine.IsPaused);
        }
        else if (action == "cancel")
        {
            machine.RequestCancel();
            machine.Update(null, 11);
            Assert.Equal(ExecutionState.Cancelled, machine.FinalResult!.FinalState);
        }
        else
        {
            if (action == "clear") observer.PlayerTile = null;
            for (int tick = 11; tick < 600 && machine.IsExecuting; tick++) machine.Update(null, tick);
            Assert.False(machine.IsExecuting);
            Assert.Equal(action == "clear" ? ExecutionState.Succeeded : ExecutionState.Failed, machine.FinalResult!.FinalState);
            if (action == "blocked") Assert.Equal("DESTINATION_UNREACHABLE", machine.FinalResult.ErrorCode);
        }
        Assert.Single(logs.Where(line => line.Contains("route resolved")));
    }

    [Fact]
    public void FinalLegBecomingUnreachableReturnsActualReasonWithinBoundedReplans()
    {
        var actor = new StubFarmerActor();
        actor.SetLocation("Farm", new(0, 1));
        var observer = new SimulatedWorldObserver();
        for (int x = 0; x <= 10; x++) observer.SetPassable("Farm", new(x, 1), true);
        var machine = new NavigationStateMachine(actor, observer, new SameMapNavigator(observer), new WorldMapGraph());
        Assert.True(machine.Start(new NavigationRequest("no-path", "task", "Farm", new(10, 1), 120), out _));
        machine.Update(null, 1);
        for (int x = 1; x <= 10; x++) observer.SetPassable("Farm", new(x, 1), false);
        for (int tick = 2; tick < 300 && machine.IsExecuting; tick++) machine.Update(null, tick);
        Assert.False(machine.IsExecuting);
        Assert.Equal("DESTINATION_UNREACHABLE", machine.FinalResult!.ErrorCode);
        Assert.Contains("unreachable", machine.FinalResult.ErrorMessage);
    }

    [Fact]
    public void PauseBeforeNativeArrivalBlocksMovementUntilExplicitResume()
    {
        var ui = new StardewAI.Companion.Mod.Menus.ChatCommandUiState();
        ui.NoteLocalPauseRequested();
        ui.BeginControl("pause", "pause");
        Assert.False(ui.ApplyNativePauseGate(null));
        ui.ApplyControlAck("pause", true, true);
        var actor = new StubFarmerActor { LocationName = "Farm" };
        var observer = new SimulatedWorldObserver();
        var machine = new NavigationStateMachine(actor, observer, new SameMapNavigator(observer), new WorldMapGraph());
        var original = actor.Tile;
        Assert.True(machine.Start(new NavigationRequest("late-nav", "task", "Farm", new(12, 12), 120), out _));
        Assert.True(ui.ApplyNativePauseGate(machine));
        for (int tick = 1; tick < 10; tick++) machine.Update(null, tick);
        Assert.True(machine.IsPaused);
        Assert.Equal(original, actor.Tile);
        ui.BeginControl("resume", "resume");
        Assert.True(ui.ApplyNativePauseGate(machine)); // resume waits for its ACK
        machine.Resume();
        ui.ApplyControlAck("resume", false, false);
        Assert.True(ui.ApplyNativePauseGate(machine));
        machine.Update(null, 10);
        Assert.True(machine.IsPaused);
        ui.BeginControl("resume2", "resume");
        ui.ApplyControlAck("resume2", true, false);
        ui.NoteLocalResumed();
        machine.Resume();
        Assert.False(ui.ApplyNativePauseGate(machine));
        for (int tick = 11; tick < 200 && machine.IsExecuting; tick++) machine.Update(null, tick);
        Assert.Equal(ExecutionState.Succeeded, machine.FinalResult!.FinalState);
        Assert.Equal(new TileCoordinate(12, 12), actor.Tile);
    }

    [Fact]
    public void PausedCancelSettlesWithoutMoving()
    {
        var actor = new StubFarmerActor { LocationName = "Farm" };
        var observer = new SimulatedWorldObserver();
        var machine = new NavigationStateMachine(actor, observer, new SameMapNavigator(observer), new WorldMapGraph());
        var original = actor.Tile;
        Assert.True(machine.Start(new NavigationRequest("pause-nav", "task", "Farm", new(12, 12), 120), out _));
        machine.RequestPause();
        machine.Update(null, 1);
        Assert.True(machine.IsPaused);
        machine.RequestCancel("cancel paused route");
        machine.Update(null, 2);
        Assert.Equal(ExecutionState.Cancelled, machine.FinalResult!.FinalState);
        Assert.Empty(machine.FinalResult.HopRecords);
        Assert.Equal(original, actor.Tile);
    }

    [Fact]
    public void CancelTakesPriorityOverPauseBeforeAnyRouteSearch()
    {
        var actor = new StubFarmerActor { LocationName = "Farm" };
        var observer = new SimulatedWorldObserver();
        var machine = new NavigationStateMachine(actor, observer, new SameMapNavigator(observer), new WorldMapGraph());
        Assert.True(machine.Start(new NavigationRequest("cancel-first", "task", "Farm", new(12, 12), 120), out _));
        machine.RequestPause();
        machine.RequestCancel();
        machine.Update(null, 1);
        Assert.Equal(ExecutionState.Cancelled, machine.FinalResult!.FinalState);
        Assert.Equal(0, observer.TilePassabilityChecks);
    }

    [Theory]
    [InlineData(false, false)]
    [InlineData(true, false)]
    [InlineData(true, true)]
    public void FailedRoute_ReportsOnlyReachableDestinationDoor(bool destinationDoor, bool entranceReachable)
    {
        var observer = new SimulatedWorldObserver();
        for (int x = 0; x <= 4; x++)
            observer.SetPassable("Town", new(x, 1), x != 2 || entranceReachable);
        var graph = new WorldMapGraph();
        graph.AddEdge(new MapEdge("Town", "JojaMart", new(1, 1), new(1, 1),
            MapEdgeKind.LockedDoorWarp, 900, 2300));
        if (destinationDoor)
            graph.AddEdge(new MapEdge("Town", "AnimalShop", new(4, 1), new(1, 1),
                MapEdgeKind.LockedDoorWarp, 900, 1800));
        var planner = new ReachableRoutePlanner(observer, new SameMapNavigator(observer), graph);
        Assert.Null(planner.Find("Town", new(0, 1), "AnimalShop", new(1, 1), 810, null, out var reason));
        Assert.NotNull(reason);
        Assert.DoesNotContain("JojaMart", reason);
        if (destinationDoor && entranceReachable)
        {
            Assert.Contains("AnimalShop", reason);
            Assert.Contains("locked", reason);
        }
        else
            Assert.Contains("No route found with passable entrance paths", reason);
    }

    [Theory]
    [InlineData("Forest", 1, 2)]
    [InlineData("Farm", 1, 3)]
    public void BlockedFarmEntrance_SelectsReachableDetourIncludingSameMapLoop(string startLocation, int startX, int hops)
    {
        var observer = new SimulatedWorldObserver();
        foreach (string location in new[] { "Farm", "Forest", "BusStop" })
            for (int x = 0; x <= 10; x++)
                observer.SetPassable(location, new TileCoordinate(x, 1), location != "Farm" || x <= 2 || x >= 8);
        var graph = new WorldMapGraph();
        graph.AddEdge(new MapEdge("Forest", "Farm", new(2, 1), new(1, 1), MapEdgeKind.Warp));
        graph.AddEdge(new MapEdge("Farm", "Forest", new(0, 1), new(1, 1), MapEdgeKind.Warp));
        graph.AddEdge(new MapEdge("Forest", "BusStop", new(3, 1), new(1, 1), MapEdgeKind.Warp));
        graph.AddEdge(new MapEdge("BusStop", "Farm", new(3, 1), new(9, 1), MapEdgeKind.Warp));
        var navigator = new SameMapNavigator(observer);
        var route = new ReachableRoutePlanner(observer, navigator, graph).Find(startLocation, new(startX, 1), "Farm", new(10, 1), 600, null, out _);
        Assert.NotNull(route);
        Assert.Equal(hops, route.Edges.Count);
        Assert.Equal("BusStop", route.Edges[^2].TargetLocation);
        var actor = new StubFarmerActor();
        actor.SetLocation(startLocation, new(startX, 1));
        var machine = new NavigationStateMachine(actor, observer, navigator, graph);
        Assert.True(machine.Start(new("cmd-detour", "task-detour", "Farm", new(10, 1), 120), out _));
        for (int tick = 1; tick < 600 && machine.IsExecuting; tick++) machine.Update(null, tick);
        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);
        Assert.Equal(new TileCoordinate(10, 1), actor.Tile);
        Assert.Equal(hops, machine.FinalResult!.HopRecords.Count - 1);
    }

    private sealed class StubFarmerActor : IFarmerActor
    {
        public string CompanionId => "companion-test";
        public float Stamina { get; set; } = 270f;
        public int MaxStamina => 270;
        public int WaterLeft { get; set; } = 40;
        public int MaxWater => 40;
        public string LocationName { get; set; } = "Farm";
        public Microsoft.Xna.Framework.Vector2 PixelPosition { get; set; } = new(10 * 64, 10 * 64);
        public TileCoordinate Tile => new((int)PixelPosition.X / 64, (int)PixelPosition.Y / 64);
        public FacingDirection Facing { get; set; } = FacingDirection.Down;
        public bool IsExhausted => false;
        public bool IsWateringCanEmpty => false;
        public string? ActiveTaskId { get; private set; }
        public void SetActiveTask(string? taskId) => ActiveTaskId = taskId;
        public void MovePixels(float dx, float dy) => PixelPosition += new Microsoft.Xna.Framework.Vector2(dx, dy);
        public void Face(FacingDirection dir) => Facing = dir;
        public void Halt() { }
        public void SetLocation(string locationName, TileCoordinate tile)
        {
            LocationName = locationName;
            PixelPosition = new Microsoft.Xna.Framework.Vector2(tile.X * 64, tile.Y * 64);
        }
        public StardewValley.Farmer? GameFarmer => null;
        public StardewValley.Tools.WateringCan? WateringCan => null;
        public StardewValley.Tools.Hoe? Hoe => null;
        public T? FindTool<T>() where T : StardewValley.Tool => null;
        public IReadOnlyList<string> GetToolNames() => Array.Empty<string>();
        public bool IsUsingTool => false;
        public int CurrentFrame => 0;
        public ToolAnimationPhase AnimationPhase => ToolAnimationPhase.None;
        public void BeginUsingTool() { }
        public ToolAnimationPhase UpdateToolAnimation(Microsoft.Xna.Framework.GameTime? time, long tickCount) => ToolAnimationPhase.None;
        public void EndUsingTool() { }
        public void ApplyPersistentState(CompanionActorState state) { }
        public CompanionActorState CapturePersistentState() => new();
        public int InventoryCapacity => 36;
        public int FreeInventorySlots => 36;
        public IReadOnlyList<InventoryItem> GetInventorySnapshot() => Array.Empty<InventoryItem>();
        public bool TryAddItemToInventory(InventoryItem item) => true;
        public bool TryAddItemToInventory(StardewValley.Item gameItem) => true;
        public bool TryRemoveItemAtSlot(int slotIndex, int stackToRemove, out InventoryItem? removed) { removed = null; return false; }
        public bool TryConsumeItem(string itemId, int count = 1) => false;
        public bool TryExtractItem(string itemId, int count, out List<StardewValley.Item> extractedItems) { extractedItems = new(); return false; }
        public int GetItemCount(string itemId) => 0;
    }

    [Fact]
    public void Start_EmptyLocation_RejectsImmediately()
    {
        var actor = new StubFarmerActor();
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var graph = new WorldMapGraph();
        var machine = new NavigationStateMachine(actor, observer, navigator, graph);

        var req = new NavigationRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            LocationId: "",
            TargetTile: new TileCoordinate(10, 10),
            MaxGameMinutes: 120
        );

        bool started = machine.Start(req, out var earlyResult);

        Assert.False(started);
        Assert.NotNull(earlyResult);
        Assert.Equal(ExecutionState.Rejected, earlyResult.FinalState);
        Assert.Equal("INVALID_PARAMETERS", earlyResult.ErrorCode);
        Assert.Null(actor.ActiveTaskId);
    }

    [Fact]
    public void Start_InvalidMaxMinutes_RejectsImmediately()
    {
        var actor = new StubFarmerActor();
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var graph = new WorldMapGraph();
        var machine = new NavigationStateMachine(actor, observer, navigator, graph);

        var req = new NavigationRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            LocationId: "Town",
            TargetTile: new TileCoordinate(10, 10),
            MaxGameMinutes: 0
        );

        bool started = machine.Start(req, out var earlyResult);

        Assert.False(started);
        Assert.NotNull(earlyResult);
        Assert.Equal(ExecutionState.Rejected, earlyResult.FinalState);
        Assert.Equal("INVALID_PARAMETERS", earlyResult.ErrorCode);
    }

    [Fact]
    public void Update_NoRouteAvailable_FailsGracefully()
    {
        var actor = new StubFarmerActor { LocationName = "Farm" };
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var graph = new WorldMapGraph(); // No edges registered
        // Register an edge from Desert so it counts as known in graph, but disconnected from Farm
        graph.AddEdge(new MapEdge("Desert", "Oasis", new TileCoordinate(10, 10), new TileCoordinate(5, 5), MapEdgeKind.Warp));

        var machine = new NavigationStateMachine(actor, observer, navigator, graph);

        var req = new NavigationRequest(
            CommandId: "cmd-2",
            TaskId: "task-2",
            LocationId: "Desert",
            TargetTile: new TileCoordinate(20, 20),
            MaxGameMinutes: 120
        );

        bool started = machine.Start(req, out var earlyResult);
        Assert.True(started);
        Assert.Null(earlyResult);
        Assert.Equal(ExecutionState.Preparing, machine.CurrentState);

        // Update once to process preparing -> find route
        machine.Update(null, 1);

        Assert.Equal(ExecutionState.Failed, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal("NO_ROUTE", machine.FinalResult.ErrorCode);
        Assert.Contains("No route found", machine.FinalResult.ErrorMessage);
    }

    [Fact]
    public void PauseAndResume_CycleTransitionsStateCorrectly()
    {
        var actor = new StubFarmerActor { LocationName = "Farm" };
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var graph = new WorldMapGraph();
        var machine = new NavigationStateMachine(actor, observer, navigator, graph);

        var req = new NavigationRequest(
            CommandId: "cmd-3",
            TaskId: "task-3",
            LocationId: "Farm",
            TargetTile: new TileCoordinate(12, 12),
            MaxGameMinutes: 120
        );

        Assert.True(machine.Start(req, out _));
        Assert.Equal(ExecutionState.Preparing, machine.CurrentState);

        // Request pause
        machine.RequestPause();
        machine.Update(null, 1);
        Assert.True(machine.IsPaused);
        Assert.Equal(ExecutionState.Paused, machine.CurrentState);

        // Resume
        machine.Resume();
        Assert.False(machine.IsPaused);
        Assert.Equal(ExecutionState.Preparing, machine.CurrentState);
    }

    [Fact]
    public void RequestCancel_TransitionsToCancelled()
    {
        var actor = new StubFarmerActor { LocationName = "Farm" };
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var graph = new WorldMapGraph();
        var machine = new NavigationStateMachine(actor, observer, navigator, graph);

        var req = new NavigationRequest(
            CommandId: "cmd-4",
            TaskId: "task-4",
            LocationId: "Farm",
            TargetTile: new TileCoordinate(15, 15),
            MaxGameMinutes: 120
        );

        Assert.True(machine.Start(req, out _));
        machine.RequestCancel("User aborted");

        machine.Update(null, 1);

        Assert.Equal(ExecutionState.Cancelled, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal("CANCELLED", machine.FinalResult.ErrorCode);
        Assert.Equal("User aborted", machine.FinalResult.ErrorMessage);
    }

    [Fact]
    public void DestinationImpassable_FallsBackToAdjacentPassableAndSetsTargetAdjusted()
    {
        var actor = new StubFarmerActor { LocationName = "Farm", PixelPosition = new(10 * 64, 10 * 64) };
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var graph = new WorldMapGraph();
        var machine = new NavigationStateMachine(actor, observer, navigator, graph);

        // Target (10, 12) is IMPASSABLE
        observer.SetPassable(new TileCoordinate(10, 12), false);
        // (10, 11) is passable and adjacent

        var req = new NavigationRequest(
            CommandId: "cmd-fallback-1",
            TaskId: "task-fallback-1",
            LocationId: "Farm",
            TargetTile: new TileCoordinate(10, 12),
            MaxGameMinutes: 120
        );

        Assert.True(machine.Start(req, out _));

        // Advance ticks until finished
        for (int i = 0; i < 50 && machine.CurrentState != ExecutionState.Succeeded && machine.CurrentState != ExecutionState.Failed; i++)
        {
            machine.Update(null, i + 1);
        }

        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.True(machine.FinalResult.TargetAdjusted);
        Assert.Equal(new TileCoordinate(10, 12), machine.FinalResult.RequestedTile);
        Assert.Equal(new TileCoordinate(10, 11), machine.FinalResult.FinalTile);
        Assert.Equal("Farm", machine.FinalResult.FinalLocation);
    }

    [Fact]
    public void DestinationPassable_DoesNotSetTargetAdjusted()
    {
        var actor = new StubFarmerActor { LocationName = "Farm", PixelPosition = new(10 * 64, 10 * 64) };
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var graph = new WorldMapGraph();
        var machine = new NavigationStateMachine(actor, observer, navigator, graph);

        var req = new NavigationRequest(
            CommandId: "cmd-passable-1",
            TaskId: "task-passable-1",
            LocationId: "Farm",
            TargetTile: new TileCoordinate(10, 11),
            MaxGameMinutes: 120
        );

        Assert.True(machine.Start(req, out _));

        for (int i = 0; i < 50 && machine.CurrentState != ExecutionState.Succeeded && machine.CurrentState != ExecutionState.Failed; i++)
        {
            machine.Update(null, i + 1);
        }

        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.False(machine.FinalResult.TargetAdjusted);
        Assert.Equal(new TileCoordinate(10, 11), machine.FinalResult.FinalTile);
    }

    [Fact]
    public void DestinationCompletelyEnclosed_FailsWithDestinationUnreachable()
    {
        var actor = new StubFarmerActor { LocationName = "Farm", PixelPosition = new(10 * 64, 10 * 64) };
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var graph = new WorldMapGraph();
        var machine = new NavigationStateMachine(actor, observer, navigator, graph);

        var target = new TileCoordinate(10, 12);
        for (int dx = -3; dx <= 3; dx++)
        {
            for (int dy = -3; dy <= 3; dy++)
            {
                observer.SetPassable(new TileCoordinate(target.X + dx, target.Y + dy), false);
            }
        }

        var req = new NavigationRequest(
            CommandId: "cmd-blocked-1",
            TaskId: "task-blocked-1",
            LocationId: "Farm",
            TargetTile: target,
            MaxGameMinutes: 120
        );

        Assert.True(machine.Start(req, out _));
        machine.Update(null, 1);

        Assert.Equal(ExecutionState.Failed, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal("DESTINATION_UNREACHABLE", machine.FinalResult.ErrorCode);
    }

    [Fact]
    public void DestinationWarpTriggerTile_SnapsToAdjacentPassableAndSetsTargetAdjusted()
    {
        var actor = new StubFarmerActor { LocationName = "Farm", PixelPosition = new(60 * 64, 15 * 64) };
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var graph = new WorldMapGraph();
        var machine = new NavigationStateMachine(actor, observer, navigator, graph);

        var requested = new TileCoordinate(64, 15);
        observer.SetWarpOrDoor(requested, true);

        var req = new NavigationRequest(
            CommandId: "cmd-warp-snap-1",
            TaskId: "task-warp-snap-1",
            LocationId: "Farm",
            TargetTile: requested,
            MaxGameMinutes: 120
        );

        Assert.True(machine.Start(req, out _));

        for (int i = 0; i < 200 && machine.CurrentState != ExecutionState.Succeeded && machine.CurrentState != ExecutionState.Failed; i++)
        {
            machine.Update(null, i + 1);
        }

        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.True(machine.FinalResult.TargetAdjusted);
        Assert.Equal(requested, machine.FinalResult.RequestedTile);
        Assert.NotEqual(requested, machine.FinalResult.FinalTile);
        Assert.True(machine.FinalResult.FinalTile.IsAdjacentTo(requested));
        Assert.Equal("Farm", machine.FinalResult.FinalLocation);
    }

    [Fact]
    public void DestinationWarpTriggerTile_GraphEdgeSourceTile_SnapsToAdjacentPassable()
    {
        var actor = new StubFarmerActor { LocationName = "Farm", PixelPosition = new(60 * 64, 15 * 64) };
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var graph = new WorldMapGraph();

        var doorTile = new TileCoordinate(64, 15);
        graph.AddEdge(new MapEdge("Farm", "FarmHouse", doorTile, new TileCoordinate(10, 20), MapEdgeKind.Door));

        var machine = new NavigationStateMachine(actor, observer, navigator, graph);

        var req = new NavigationRequest(
            CommandId: "cmd-warp-snap-2",
            TaskId: "task-warp-snap-2",
            LocationId: "Farm",
            TargetTile: doorTile,
            MaxGameMinutes: 120
        );

        Assert.True(machine.Start(req, out _));

        for (int i = 0; i < 200 && machine.CurrentState != ExecutionState.Succeeded && machine.CurrentState != ExecutionState.Failed; i++)
        {
            machine.Update(null, i + 1);
        }

        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.True(machine.FinalResult.TargetAdjusted);
        Assert.Equal(doorTile, machine.FinalResult.RequestedTile);
        Assert.NotEqual(doorTile, machine.FinalResult.FinalTile);
        Assert.True(machine.FinalResult.FinalTile.IsAdjacentTo(doorTile));
    }

    [Fact]
    public void DestinationNormalPassable_DoesNotSetTargetAdjusted_SettlesAndSucceeds()
    {
        var actor = new StubFarmerActor { LocationName = "Farm", PixelPosition = new(10 * 64, 10 * 64) };
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var graph = new WorldMapGraph();
        var machine = new NavigationStateMachine(actor, observer, navigator, graph);

        var target = new TileCoordinate(10, 11);
        var req = new NavigationRequest(
            CommandId: "cmd-normal-1",
            TaskId: "task-normal-1",
            LocationId: "Farm",
            TargetTile: target,
            MaxGameMinutes: 120
        );

        Assert.True(machine.Start(req, out _));

        for (int i = 0; i < 50 && machine.CurrentState != ExecutionState.Succeeded && machine.CurrentState != ExecutionState.Failed; i++)
        {
            machine.Update(null, i + 1);
        }

        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.False(machine.FinalResult.TargetAdjusted);
        Assert.Equal(target, machine.FinalResult.FinalTile);
        Assert.Equal(target, machine.FinalResult.RequestedTile);
    }

    [Fact]
    public void ArrivalVerification_WhenCompanionWarpsAwayDuringWindow_FailsWithArrivalUnstable()
    {
        var actor = new StubFarmerActor { LocationName = "Farm", PixelPosition = new(10 * 64, 10 * 64) };
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var graph = new WorldMapGraph();
        var machine = new NavigationStateMachine(actor, observer, navigator, graph);

        var target = new TileCoordinate(10, 11);
        var req = new NavigationRequest(
            CommandId: "cmd-warp-fail-1",
            TaskId: "task-warp-fail-1",
            LocationId: "Farm",
            TargetTile: target,
            MaxGameMinutes: 120
        );

        Assert.True(machine.Start(req, out _));

        int tick = 1;
        while (machine.CurrentState != ExecutionState.Verifying && tick < 50)
        {
            machine.Update(null, tick++);
        }

        Assert.Equal(ExecutionState.Verifying, machine.CurrentState);

        actor.SetLocation("FarmHouse", new TileCoordinate(10, 20));

        machine.Update(null, tick++);

        Assert.Equal(ExecutionState.Failed, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal("ARRIVAL_UNSTABLE", machine.FinalResult.ErrorCode);
        Assert.Equal("FarmHouse", machine.FinalResult.FinalLocation);
        Assert.Contains("ARRIVAL_UNSTABLE: warped to 'FarmHouse'", machine.FinalResult.ErrorMessage);
    }

    [Fact]
    public void ArrivalVerification_WhenCompanionDriftsTileDuringWindow_FailsWithArrivalUnstable()
    {
        var actor = new StubFarmerActor { LocationName = "Farm", PixelPosition = new(10 * 64, 10 * 64) };
        var observer = new SimulatedWorldObserver();
        var navigator = new SameMapNavigator(observer);
        var graph = new WorldMapGraph();
        var machine = new NavigationStateMachine(actor, observer, navigator, graph);

        var target = new TileCoordinate(10, 11);
        var req = new NavigationRequest(
            CommandId: "cmd-drift-fail-1",
            TaskId: "task-drift-fail-1",
            LocationId: "Farm",
            TargetTile: target,
            MaxGameMinutes: 120
        );

        Assert.True(machine.Start(req, out _));

        int tick = 1;
        while (machine.CurrentState != ExecutionState.Verifying && tick < 50)
        {
            machine.Update(null, tick++);
        }

        Assert.Equal(ExecutionState.Verifying, machine.CurrentState);

        actor.PixelPosition = new Microsoft.Xna.Framework.Vector2(20 * 64, 20 * 64);

        machine.Update(null, tick++);

        Assert.Equal(ExecutionState.Failed, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal("ARRIVAL_UNSTABLE", machine.FinalResult.ErrorCode);
        Assert.Contains("ARRIVAL_UNSTABLE: drifted to", machine.FinalResult.ErrorMessage);
    }
}
