using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class ShippingStateMachineTests
{
    private static (ShippingStateMachine machine, MechanicsActor actor, SimulatedWorldObserver observer, TestShippingAdapter adapter)
        CreateTestContext(TileCoordinate actorTile)
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var actor = new MechanicsActor(
            initialPose: new AuthoritativePose("Farm", actorTile.X, actorTile.Y, FacingDirection.Up),
            stamina: 270f,
            water: 40);
        var navigator = new SameMapNavigator(observer);
        var adapter = new TestShippingAdapter(observer);
        var machine = new ShippingStateMachine(actor, observer, navigator, adapter);

        return (machine, actor, observer, adapter);
    }

    [Fact]
    public void MechanicsActor_TryExtractItem_ExtractsRequestedCountAndReducesStack()
    {
        var actor = new MechanicsActor(initialPose: new AuthoritativePose("Farm", 10, 10, FacingDirection.Down));
        actor.TryAddItemToInventory(new InventoryItem("(O)24", "Parsnip", stack: 4));

        Assert.Equal(4, actor.GetItemCount("(O)24"));
        Assert.Equal(4, actor.GetItemCount("24"));

        bool extracted = actor.TryExtractItem("(O)24", 2, out var items);
        Assert.True(extracted);
        Assert.Equal(2, actor.GetItemCount("(O)24"));

        bool extractMore = actor.TryExtractItem("24", 2, out _);
        Assert.True(extractMore);
        Assert.Equal(0, actor.GetItemCount("(O)24"));

        bool extractExcess = actor.TryExtractItem("24", 1, out _);
        Assert.False(extractExcess);
    }

    [Fact]
    public void Start_RejectsNonFarmLocation()
    {
        var (machine, actor, _, _) = CreateTestContext(new TileCoordinate(71, 15));
        actor.TryAddItemToInventory(new InventoryItem("(O)24", "Parsnip", stack: 4));

        var request = new ShippingRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            LocationId: "Town",
            Items: new[] { new ShippingItemRequest("(O)24", 2) }
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.False(started);
        Assert.NotNull(earlyResult);
        Assert.Equal("rejected", earlyResult.ToTransportPayload().TerminalState);
        Assert.Contains("only supported on 'Farm'", earlyResult.ErrorMessage);
    }

    [Fact]
    public void Start_RejectsInsufficientItemCount()
    {
        var (machine, actor, _, _) = CreateTestContext(new TileCoordinate(71, 15));
        actor.TryAddItemToInventory(new InventoryItem("(O)24", "Parsnip", stack: 1));

        var request = new ShippingRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            LocationId: "Farm",
            Items: new[] { new ShippingItemRequest("(O)24", 2) }
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.False(started);
        Assert.NotNull(earlyResult);
        Assert.Equal("rejected", earlyResult.ToTransportPayload().TerminalState);
        Assert.Contains("insufficient count", earlyResult.ErrorMessage);
    }

    [Fact]
    public void Start_RejectsUnshippableItem()
    {
        var (machine, actor, _, adapter) = CreateTestContext(new TileCoordinate(71, 15));
        actor.TryAddItemToInventory(new InventoryItem("(T)Hoe", "Hoe", stack: 1, isTool: true));
        adapter.UnshippableItemIds.Add("(T)Hoe");

        var request = new ShippingRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            LocationId: "Farm",
            Items: new[] { new ShippingItemRequest("(T)Hoe", 1) }
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.False(started);
        Assert.NotNull(earlyResult);
        Assert.Equal("rejected", earlyResult.ToTransportPayload().TerminalState);
        Assert.Contains("cannot be shipped", earlyResult.ErrorMessage);
    }

    [Fact]
    public void Execute_AdjacentToShippingBin_Succeeds()
    {
        // Shipping bin footprint is (71, 14) and (72, 14).
        // Actor starts at (71, 15), which is directly adjacent to (71, 14).
        var (machine, actor, _, _) = CreateTestContext(new TileCoordinate(71, 15));
        actor.TryAddItemToInventory(new InventoryItem("(O)24", "Parsnip", stack: 4));

        var request = new ShippingRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            LocationId: "Farm",
            Items: new[] { new ShippingItemRequest("(O)24", 2) }
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.True(started);
        Assert.Null(earlyResult);

        // Tick 1: Facing -> Acting
        machine.Update(null, 1);
        Assert.Equal(ExecutionState.Acting, machine.CurrentState);

        // Tick 2: Acting -> Succeeded
        machine.Update(null, 2);
        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        var payload = result.ToTransportPayload();

        Assert.Equal("succeeded", payload.TerminalState);
        Assert.Equal(2, payload.CompletedCount);
        Assert.Equal(0, payload.SkippedCount);
        Assert.Equal(0, payload.FailedCount);

        Assert.NotNull(payload.Details);
        Assert.True(payload.Details.ContainsKey("estimatedTotalValue"));
        Assert.Equal(70, Convert.ToInt32(payload.Details["estimatedTotalValue"]));

        // Remaining inventory check
        Assert.Equal(2, actor.GetItemCount("(O)24"));
    }

    [Fact]
    public void RequestCancel_CancelsExecution()
    {
        var (machine, actor, _, _) = CreateTestContext(new TileCoordinate(71, 15));
        actor.TryAddItemToInventory(new InventoryItem("(O)24", "Parsnip", stack: 4));

        var request = new ShippingRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            LocationId: "Farm",
            Items: new[] { new ShippingItemRequest("(O)24", 2) }
        );

        machine.Start(request, out _);
        machine.RequestCancel("User cancelled");

        machine.Update(null, 1);
        Assert.Equal(ExecutionState.Cancelled, machine.CurrentState);

        var payload = machine.FinalResult?.ToTransportPayload();
        Assert.NotNull(payload);
        Assert.Equal("cancelled", payload.TerminalState);
    }
}
