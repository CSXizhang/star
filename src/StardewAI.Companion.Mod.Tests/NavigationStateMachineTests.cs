using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class NavigationStateMachineTests
{
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
}
