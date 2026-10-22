using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class ChestWithdrawTests
{
    private static (ChestActionStateMachine machine, MechanicsActor actor, SimulatedWorldObserver observer, TestChestAdapter adapter)
        CreateTestContext(TileCoordinate actorTile, TileCoordinate chestTile)
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var actor = new MechanicsActor(
            initialPose: new AuthoritativePose("Farm", actorTile.X, actorTile.Y, FacingDirection.Right),
            stamina: 270f,
            water: 40);
        var navigator = new SameMapNavigator(observer);
        var adapter = new TestChestAdapter(observer);

        // Register chest in observer
        observer.SetChest(chestTile, new ChestScanInfo(
            Tile: chestTile,
            Capacity: 36,
            FreeSlots: 35,
            Contents: new List<ChestSlotInfo>
            {
                new ChestSlotInfo(0, "(O)CarrotSeeds", "Carrot Seeds", 5, 0, "胡萝卜种子", true, new[] { "spring" })
            }
        ));

        var machine = new ChestActionStateMachine(actor, observer, navigator, adapter);
        return (machine, actor, observer, adapter);
    }

    [Theory]
    [InlineData("184", 1)]
    [InlineData("(O)184", 1)]
    [InlineData("(BC)184", 0)]
    public void DepositFilter_AcceptsNativeObjectIdWithoutCrossingQualifiedTypes(string filter, int expected)
    {
        var chestTile = new TileCoordinate(11, 10);
        var (machine, actor, _, _) = CreateTestContext(new TileCoordinate(10, 10), chestTile);
        actor.TryAddItemToInventory(new InventoryItem("(O)184", "Milk", 1));
        var request = new ChestActionRequest("deposit-id", "deposit-task", ChestActionKind.Deposit,
            "Farm", chestTile, ItemIds: new[] { filter });
        Assert.True(machine.Start(request, out var early));
        machine.StepTicks(100);
        Assert.Equal(expected, machine.FinalResult!.ToTransportPayload().CompletedCount);
    }

    [Fact]
    public void ChestActionRequest_Withdraw_SerializesCorrectlyToTransportPayload()
    {
        var chestTile = new TileCoordinate(10, 10);
        var withdrawn = new List<WithdrawnItemInfo>
        {
            new("(O)CarrotSeeds", "Carrot Seeds", 0, 3)
        };

        var result = new ChestActionResult(
            commandId: "cmd-withdraw-1",
            taskId: "task-withdraw-1",
            kind: ChestActionKind.Withdraw,
            finalState: ExecutionState.Succeeded,
            chestTile: chestTile,
            depositedItems: Array.Empty<DepositedItemInfo>(),
            skippedItems: Array.Empty<SkippedItemInfo>(),
            failedItems: Array.Empty<FailedItemInfo>(),
            merges: Array.Empty<ChestMergeInfo>(),
            staminaUsed: 0f,
            waterUsed: 0,
            gameMinutesElapsed: 10,
            finalWorldRevision: 1,
            withdrawnItems: withdrawn
        );

        var payload = result.ToTransportPayload();

        Assert.Equal("cmd-withdraw-1", payload.CommandId);
        Assert.Equal("task-withdraw-1", payload.TaskId);
        Assert.Equal("succeeded", payload.TerminalState);
        Assert.Equal("withdraw-chest", payload.SkillId);
        Assert.Equal(1, payload.CompletedCount);
        Assert.Single(payload.Effects);
        Assert.Equal("withdrawn", payload.Effects[0]["state"]);
        Assert.Equal("(O)CarrotSeeds", payload.Effects[0]["itemId"]);
        Assert.Equal(3, payload.Effects[0]["stack"]);
    }

    [Theory]
    [InlineData(2, 2, 77, ExecutionState.Succeeded)]
    [InlineData(77, 77, 2, ExecutionState.Succeeded)]
    [InlineData(80, 79, 0, ExecutionState.PartiallySucceeded)]
    public void ExactDeposit_ConservesQuantityAcrossQualityStacks(int requested, int actual, int remaining, ExecutionState state)
    {
        var chest = new TileCoordinate(11, 10);
        var (machine, actor, _, adapter) = CreateTestContext(new TileCoordinate(10, 10), chest);
        actor.TryAddItemToInventory(new InventoryItem("(T)WateringCan", "Watering Can", 1) { IsTool = true });
        actor.TryAddItemToInventory(new InventoryItem("(O)24", "Parsnip", 76) { Quality = 0 });
        actor.TryAddItemToInventory(new InventoryItem("(O)24", "Parsnip", 2) { Quality = 1 });
        actor.TryAddItemToInventory(new InventoryItem("(O)24", "Parsnip", 1) { Quality = 2 });
        Assert.True(machine.Start(new ChestActionRequest("deposit", "deposit-task", ChestActionKind.Deposit,
            "Farm", chest, DepositItems: new[] { new DepositItemSpec("24", requested) }), out _));
        machine.StepTicks(100);
        Assert.Equal(state, machine.CurrentState);
        Assert.Equal(actual, adapter.StoredItems["(O)24"]);
        Assert.Equal(remaining, actor.GetItemCount("(O)24"));
        var payload = machine.FinalResult!.ToTransportPayload();
        Assert.Equal(requested, payload.Details!["requestedQuantity"]);
        Assert.Equal(0, payload.SkippedCount);
        Assert.Equal(actual, payload.Details["actualQuantity"]);
        Assert.Equal(Math.Max(0, requested - actual), payload.Details["remainingQuantity"]);
        Assert.Equal(requested == 2 ? new[] { 0 } : requested == 77 ? new[] { 0, 1 } : new[] { 0, 1, 2 },
            machine.FinalResult.DepositedItems.Select(x => x.Quality).ToArray());
    }

    [Fact]
    public void ExactDeposit_MissingItemCannotCompleteSuccessfully()
    {
        var chest = new TileCoordinate(11, 10);
        var (machine, _, _, _) = CreateTestContext(new TileCoordinate(10, 10), chest);
        Assert.True(machine.Start(new ChestActionRequest("missing", "missing-task", ChestActionKind.Deposit,
            "Farm", chest, DepositItems: new[] { new DepositItemSpec("24", 2) }), out var result));
        Assert.Equal(ExecutionState.Failed, result!.FinalState);
        Assert.Equal(2, result.ToTransportPayload().Details!["remainingQuantity"]);
    }

    [Fact]
    public void ChestActionStateMachine_Withdraw_SucceedsAndTransfersItems()
    {
        var actorTile = new TileCoordinate(10, 10);
        var chestTile = new TileCoordinate(11, 10);
        var (machine, actor, _, adapter) = CreateTestContext(actorTile, chestTile);

        adapter.StoredItems["(O)CarrotSeeds"] = 5;

        var request = new ChestActionRequest(
            CommandId: "cmd-w1",
            TaskId: "task-w1",
            Kind: ChestActionKind.Withdraw,
            LocationId: "Farm",
            ChestTile: chestTile,
            WithdrawItems: new List<WithdrawItemSpec>
            {
                new("(O)CarrotSeeds", 3)
            }
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.True(started);
        Assert.Null(earlyResult);

        // Step execution ticks to complete adjacent interaction
        machine.StepTicks(10);

        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Single(machine.FinalResult.WithdrawnItems);
        Assert.Equal(3, machine.FinalResult.WithdrawnItems[0].Stack);
        Assert.Equal(2, adapter.StoredItems["(O)CarrotSeeds"]);
        Assert.Equal(3, actor.GetItemCount("(O)CarrotSeeds"));
    }

    [Fact]
    public void ChestActionStateMachine_Withdraw_FailsWhenItemNotFound()
    {
        var actorTile = new TileCoordinate(10, 10);
        var chestTile = new TileCoordinate(11, 10);
        var (machine, _, _, _) = CreateTestContext(actorTile, chestTile);

        var request = new ChestActionRequest(
            CommandId: "cmd-w2",
            TaskId: "task-w2",
            Kind: ChestActionKind.Withdraw,
            LocationId: "Farm",
            ChestTile: chestTile,
            WithdrawItems: new List<WithdrawItemSpec>
            {
                new("(O)MissingSeed", 1)
            }
        );

        bool started = machine.Start(request, out _);
        Assert.True(started);

        machine.StepTicks(10);

        Assert.Equal(ExecutionState.Failed, machine.CurrentState);
        Assert.Single(machine.FinalResult!.FailedItems);
        Assert.Equal("item-not-found", machine.FinalResult.FailedItems[0].Reason);
    }

    [Fact]
    public void WithdrawalShortage_IsPartialAndReportsUnfulfilledQuantity()
    {
        var chest = new TileCoordinate(11, 10);
        var (machine, actor, _, adapter) = CreateTestContext(new TileCoordinate(10, 10), chest);
        adapter.StoredItems["(O)24"] = 76;
        Assert.True(machine.Start(new ChestActionRequest("short", "short-task", ChestActionKind.Withdraw,
            "Farm", chest, WithdrawItems: new[] { new WithdrawItemSpec("(O)24", 77) }), out _));
        machine.StepTicks(20);
        Assert.Equal(ExecutionState.PartiallySucceeded, machine.CurrentState);
        Assert.Equal(76, actor.GetItemCount("(O)24"));
        var payload = machine.FinalResult!.ToTransportPayload();
        Assert.Equal(77, payload.Details!["requestedQuantity"]);
        Assert.Equal(76, payload.Details["actualQuantity"]);
        Assert.Equal(1, payload.Details["remainingQuantity"]);
    }

    [Fact]
    public void Withdrawal_ContinuesIntoNextStackUntilRequestedQuantityIsMet()
    {
        var chest = new TileCoordinate(11, 10);
        var (machine, actor, _, adapter) = CreateTestContext(new TileCoordinate(10, 10), chest);
        adapter.StoredItems["(O)24"] = 79;
        adapter.MaximumTransferPerCall = 76;
        Assert.True(machine.Start(new ChestActionRequest("multi-stack", "multi-stack-task", ChestActionKind.Withdraw,
            "Farm", chest, WithdrawItems: new[] { new WithdrawItemSpec("(O)24", 77) }), out _));
        machine.StepTicks(20);
        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);
        Assert.Equal(77, actor.GetItemCount("(O)24"));
        Assert.Equal(2, adapter.StoredItems["(O)24"]);
        Assert.Equal(new[] { 76, 1 }, machine.FinalResult!.WithdrawnItems.Select(x => x.Stack));
        var payload = machine.FinalResult.ToTransportPayload();
        Assert.Equal(77, payload.Details!["requestedQuantity"]);
        Assert.Equal(77, payload.Details["actualQuantity"]);
        Assert.Equal(0, payload.Details["remainingQuantity"]);
    }

    [Fact]
    public void ScanPlantingOptions_CompanionHasHoe_AccuratelyReflectsHoePossession()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var actorWithoutHoe = new MechanicsActor(initialPose: new AuthoritativePose("Farm", 10, 10, FacingDirection.Down));
        actorWithoutHoe.Hoe = null;

        var scanWithout = observer.ScanPlantingOptions("Farm", new TileCoordinate(10, 10), 10, actorWithoutHoe);
        Assert.False(scanWithout.CompanionHasHoe);

        var actorWithHoe = new MechanicsActor(initialPose: new AuthoritativePose("Farm", 10, 10, FacingDirection.Down));
        actorWithHoe.Hoe = new StardewValley.Tools.Hoe();

        var scanWith = observer.ScanPlantingOptions("Farm", new TileCoordinate(10, 10), 10, actorWithHoe);
        Assert.True(scanWith.CompanionHasHoe);
    }

    [Fact]
    public void InventoryItem_PreservesDisplayName()
    {
        var item = new InventoryItem("(O)CarrotSeeds", "Carrot Seeds", stack: 3, displayName: "胡萝卜种子");
        Assert.Equal("胡萝卜种子", item.DisplayName);
        Assert.Equal("Carrot Seeds", item.Name);
        Assert.Equal("(O)CarrotSeeds", item.ItemId);
    }

    [Fact]
    public void LocationValidationUsesCompanionNotPlayer()
    {
        // The player stands on the Farm while the companion is elsewhere. A guard
        // keyed on the player's map would accept; the companion-keyed guard rejects.
        var (machine, actor, observer, _) = CreateTestContext(new TileCoordinate(10, 10), new TileCoordinate(11, 10));
        actor.TryAddItemToInventory(new InventoryItem("(O)184", "Milk", 1));
        actor.UpdatePose("FarmHouse", new TileCoordinate(10, 10), FacingDirection.Down);
        observer.CurrentLocationName = "Farm";

        var request = new ChestActionRequest("cmd-loc", "task-loc", ChestActionKind.Deposit,
            "Farm", new TileCoordinate(11, 10), ItemIds: new[] { "(O)184" });

        Assert.False(machine.Start(request, out var early));
        Assert.NotNull(early);
        Assert.Equal(ExecutionState.Rejected, early!.FinalState);
        Assert.Equal("LOCATION_MISMATCH", early.ErrorCode);
    }
}
