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
}
