using System.Text.Json.Nodes;
using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class CompanionMechanicsCoordinatorTests
{
    private (CompanionMechanicsCoordinator coordinator, MechanicsActor actor, SimulatedWorldObserver observer) CreateHarness(
        bool giveHoe = false,
        bool givePlant = false,
        string? seedItemId = null,
        int seedCount = 0,
        bool giveShipping = false,
        string? shipItemId = null,
        int shipItemCount = 0,
        AuthoritativePose? initialPose = null)
    {
        var pose = initialPose ?? new AuthoritativePose("Farm", 10, 10, FacingDirection.Down);
        var actor = new MechanicsActor("coord-companion", initialPose: pose);
        if (giveHoe)
        {
            actor.Hoe = new StardewValley.Tools.Hoe();
        }
        if (seedCount > 0 && seedItemId != null)
        {
            actor.TryAddItemToInventory(new InventoryItem(seedItemId, "Seeds", seedCount));
        }
        if (shipItemCount > 0 && shipItemId != null)
        {
            actor.TryAddItemToInventory(new InventoryItem(shipItemId, "Parsnip", shipItemCount));
        }
        var avatar = new CompanionAvatar(actor);
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);
        var adapter = new TestWateringCanAdapter(observer);
        var hoeAdapter = giveHoe ? new TestHoeAdapter(observer) : null;
        var plantAdapter = givePlant ? new TestPlantAdapter(observer) : null;
        var shippingAdapter = giveShipping ? new TestShippingAdapter(observer) : null;

        var coordinator = new CompanionMechanicsCoordinator(actor, observer, navigator, adapter, avatar,
            hoeAdapter: hoeAdapter, plantAdapter: plantAdapter, shippingAdapter: shippingAdapter);
        return (coordinator, actor, observer);
    }

    private EnvelopeDto CreateEnvelope(string messageType, string messageId, string? idempotencyKey = null)
    {
        return new EnvelopeDto(
            ProtocolVersion: "0.1",
            MessageType: messageType,
            MessageId: messageId,
            SenderInstanceId: "runtime-test",
            SequenceNumber: 1,
            WorldRevision: 1,
            SentAt: DateTimeOffset.UtcNow,
            Payload: new JsonObject(),
            IdempotencyKey: idempotencyKey
        );
    }

    [Fact]
    public void TryAcceptSkillExecute_ValidWaterZone_AcceptedAndExecuted()
    {
        var (coordinator, actor, observer) = CreateHarness();

        var t1 = new TileCoordinate(10, 12);
        observer.SetDirt(t1, TileDirtState.DryDirt());

        var payload = new SkillExecutePayload(
            CommandId: "cmd-water-001",
            TaskId: "task-water-01",
            SkillId: "water-zone",
            SkillVersion: "0.1",
            ExpectedWorldRevision: 1,
            Parameters: new WaterZoneParameters("Farm", new List<TileCoord> { new(10, 12) }),
            Budgets: new ExecutionBudgets(60, 50f, 20),
            CancelPolicy: "safe-point",
            PolicyDecisionId: "policy-1"
        );

        var envelope = CreateEnvelope("skill.execute", "msg-1", "idem-1");

        bool accepted = coordinator.TryAcceptSkillExecute(payload, envelope, out var rejectResult);

        Assert.True(accepted);
        Assert.Null(rejectResult);

        // Advance tick-driven state machine to completion
        coordinator.StateMachine.StepTicks(100);

        Assert.NotNull(coordinator.LastResult);
        Assert.Equal(ExecutionState.Succeeded, coordinator.LastResult.FinalState);
        Assert.Single(coordinator.LastResult.WateredTiles);
        Assert.Equal(t1, coordinator.LastResult.WateredTiles[0]);
    }

    [Fact]
    public void TryAcceptSkillExecute_UnsupportedSkill_Rejected()
    {
        var (coordinator, actor, observer) = CreateHarness();

        var payload = new SkillExecutePayload(
            CommandId: "cmd-fish-001",
            TaskId: "task-fish-01",
            SkillId: "fishing",
            SkillVersion: "0.1",
            ExpectedWorldRevision: 1,
            Parameters: new WaterZoneParameters("Farm", new List<TileCoord>()),
            Budgets: new ExecutionBudgets(60, 50f, 20),
            CancelPolicy: "safe-point",
            PolicyDecisionId: "policy-1"
        );

        var envelope = CreateEnvelope("skill.execute", "msg-2");

        bool accepted = coordinator.TryAcceptSkillExecute(payload, envelope, out var rejectResult);

        Assert.False(accepted);
        Assert.NotNull(rejectResult);
        Assert.Equal("rejected", rejectResult.TerminalState);
        Assert.Equal("UNSUPPORTED_SKILL", rejectResult.Error?.Code);
    }

    [Fact]
    public void TryAcceptSkillExecute_ConcurrentExecution_RejectedWithConflict()
    {
        var (coordinator, actor, observer) = CreateHarness();

        // Simulate actor already busy with another active task
        actor.SetActiveTask("existing-task-999");

        var payload = new SkillExecutePayload(
            CommandId: "cmd-water-002",
            TaskId: "task-water-02",
            SkillId: "water-zone",
            SkillVersion: "0.1",
            ExpectedWorldRevision: 1,
            Parameters: new WaterZoneParameters("Farm", new List<TileCoord> { new(10, 12) }),
            Budgets: new ExecutionBudgets(60, 50f, 20),
            CancelPolicy: "safe-point",
            PolicyDecisionId: "policy-1"
        );

        var envelope = CreateEnvelope("skill.execute", "msg-3");

        bool accepted = coordinator.TryAcceptSkillExecute(payload, envelope, out var rejectResult);

        Assert.False(accepted);
        Assert.NotNull(rejectResult);
        Assert.Equal("rejected", rejectResult.TerminalState);
        Assert.Equal("CONFLICT", rejectResult.Error?.Code);
    }

    [Fact]
    public void CaptureCurrentSnapshot_CapturesAuthoritativeState()
    {
        var (coordinator, actor, observer) = CreateHarness();

        var snapshot = coordinator.CaptureCurrentSnapshot(worldRevision: 5);

        Assert.Equal(5, snapshot.CapturedRevision);
        Assert.Equal("Farm", snapshot.Companion.LocationId);
        Assert.Equal(10, snapshot.Companion.TileX);
        Assert.Equal(10, snapshot.Companion.TileY);
        Assert.Equal(270f, snapshot.Companion.Stamina);
        Assert.Equal(40, snapshot.Companion.WaterCanLevel);
        Assert.True(snapshot.Companion.HasWateringCan);
        Assert.Equal("idle", snapshot.Companion.Activity);
        Assert.Equal("Farm", snapshot.World.CurrentLocation);
    }

    [Fact]
    public void TryAcceptSkillExecute_CancelledBeforeExecute_RejectsImmediately()
    {
        var (coordinator, actor, observer) = CreateHarness();

        // Cancellation arrives before execute is dispatched
        var cancelPayload = new SkillCancelPayload(
            CommandId: "cmd-cancelled-early",
            Reason: "Cancelled before arrival",
            RequestedByPlayer: true,
            CancelPolicy: "safe-point"
        );
        coordinator.OnCancelSkill(cancelPayload, CreateEnvelope("skill.cancel", "cancel-msg-1"));

        var executePayload = new SkillExecutePayload(
            CommandId: "cmd-cancelled-early",
            TaskId: "task-cancelled-early",
            SkillId: "water-zone",
            SkillVersion: "0.1",
            ExpectedWorldRevision: 1,
            Parameters: new WaterZoneParameters("Farm", new List<TileCoord> { new(10, 12) }),
            Budgets: new ExecutionBudgets(60, 50f, 20),
            CancelPolicy: "safe-point",
            PolicyDecisionId: "policy-1"
        );

        bool accepted = coordinator.TryAcceptSkillExecute(executePayload, CreateEnvelope("skill.execute", "exec-msg-1"), out var rejectResult);

        Assert.False(accepted);
        Assert.NotNull(rejectResult);
        Assert.Equal("cancelled", rejectResult.TerminalState);
        Assert.Equal("CANCELLED_BEFORE_EXECUTE", rejectResult.Error?.Code);
    }

    [Fact]
    public void TryAcceptSkillExecute_EmptyCan_RejectsImmediatelyWithCanEmpty()
    {
        var (coordinator, actor, observer) = CreateHarness();

        // Empty the actor's watering can
        actor.TryConsumeWater(actor.Water, out _);
        Assert.True(actor.IsWateringCanEmpty);

        var payload = new SkillExecutePayload(
            CommandId: "cmd-empty-can",
            TaskId: "task-empty-can",
            SkillId: "water-zone",
            SkillVersion: "0.1",
            ExpectedWorldRevision: 1,
            Parameters: new WaterZoneParameters("Farm", new List<TileCoord> { new(10, 12) }),
            Budgets: new ExecutionBudgets(60, 50f, 20),
            CancelPolicy: "safe-point",
            PolicyDecisionId: "policy-1"
        );

        bool accepted = coordinator.TryAcceptSkillExecute(payload, CreateEnvelope("skill.execute", "exec-empty-can"), out var rejectResult);

        Assert.False(accepted);
        Assert.NotNull(rejectResult);
        Assert.Equal("rejected", rejectResult.TerminalState);
        Assert.Equal("CAN_EMPTY", rejectResult.Error?.Code);
        Assert.True(rejectResult.RetryRecommended);
        Assert.Equal(0, rejectResult.CompletedCount);
        Assert.Equal(0, rejectResult.SkippedCount);
        Assert.Equal(0, rejectResult.FailedCount);
    }

    [Fact]
    public void ScanFarmWork_FiltersUntilledAndReturnsCorrectDirtAttributes()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };

        var t1 = new TileCoordinate(10, 10);
        var t2 = new TileCoordinate(10, 11);
        var t3 = new TileCoordinate(10, 12);
        var t4 = new TileCoordinate(10, 13);

        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetDirt(t2, TileDirtState.WateredDirt());
        observer.SetDirt(t3, new TileDirtState { IsTilled = true, IsWatered = false, HasCrop = true, CropId = "472", IsHarvestable = true });
        observer.SetDirt(t4, TileDirtState.NotTilled);

        var work = observer.ScanFarmWork("Farm");

        Assert.Equal(3, work.Count);
        Assert.Contains(work, w => w.Tile == t1 && !w.IsWatered && !w.HasCrop && !w.IsHarvestable);
        Assert.Contains(work, w => w.Tile == t2 && w.IsWatered && !w.HasCrop && !w.IsHarvestable);
        Assert.Contains(work, w => w.Tile == t3 && !w.IsWatered && w.HasCrop && w.CropId == "472" && w.IsHarvestable);
        Assert.DoesNotContain(work, w => w.Tile == t4);
    }

    [Fact]
    public void ScanFarmWork_OrdersDeterministicallyByYThenX()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };

        observer.SetDirt(new TileCoordinate(20, 15), TileDirtState.DryDirt());
        observer.SetDirt(new TileCoordinate(10, 15), TileDirtState.DryDirt());
        observer.SetDirt(new TileCoordinate(15, 10), TileDirtState.DryDirt());

        var work = observer.ScanFarmWork("Farm");

        Assert.Equal(3, work.Count);
        Assert.Equal(new TileCoordinate(15, 10), work[0].Tile);
        Assert.Equal(new TileCoordinate(10, 15), work[1].Tile);
        Assert.Equal(new TileCoordinate(20, 15), work[2].Tile);
    }

    [Fact]
    public void CaptureCurrentSnapshot_WhenNoFarmWork_ReturnsEmptyListsAndZeroCounts()
    {
        var (coordinator, _, _) = CreateHarness();

        var snapshot = coordinator.CaptureCurrentSnapshot(worldRevision: 1);

        Assert.NotNull(snapshot.FarmWork);
        Assert.Empty(snapshot.FarmWork.TilledUnwateredTiles);
        Assert.Equal(0, snapshot.FarmWork.TilledUnwateredCount);
        Assert.False(snapshot.FarmWork.IsTruncated);
        Assert.False(snapshot.FarmWork.Truncated);
        Assert.Equal(0, snapshot.FarmWork.MatureCropCount);

        // Verify World.FarmWork is identical
        Assert.NotNull(snapshot.World.FarmWork);
        Assert.Same(snapshot.FarmWork, snapshot.World.FarmWork);
    }

    [Fact]
    public void CaptureCurrentSnapshot_CarriesFarmWork_UnderLimit_NotTruncated()
    {
        var (coordinator, _, observer) = CreateHarness();

        // Add 3 dry tiles, 2 watered tiles, 1 mature crop on dry tile
        var d1 = new TileCoordinate(64, 15);
        var d2 = new TileCoordinate(64, 16);
        var d3 = new TileCoordinate(64, 17);
        var w1 = new TileCoordinate(65, 15);
        var w2 = new TileCoordinate(65, 16);

        observer.SetDirt(d1, TileDirtState.DryDirt());
        observer.SetDirt(d2, TileDirtState.DryDirt());
        observer.SetDirt(d3, new TileDirtState { IsTilled = true, IsWatered = false, HasCrop = true, CropId = "24", IsHarvestable = true });
        observer.SetDirt(w1, TileDirtState.WateredDirt());
        observer.SetDirt(w2, TileDirtState.WateredDirt());

        var snapshot = coordinator.CaptureCurrentSnapshot(worldRevision: 2);

        Assert.NotNull(snapshot.FarmWork);
        Assert.Equal(3, snapshot.FarmWork.TilledUnwateredCount);
        Assert.Equal(3, snapshot.FarmWork.TilledUnwateredTiles.Count);
        Assert.False(snapshot.FarmWork.IsTruncated);
        Assert.False(snapshot.FarmWork.Truncated);
        Assert.Equal(1, snapshot.FarmWork.MatureCropCount);

        // Verify dry tiles are present and watered tiles are excluded
        Assert.Contains(snapshot.FarmWork.TilledUnwateredTiles, t => t.X == 64 && t.Y == 15);
        Assert.Contains(snapshot.FarmWork.TilledUnwateredTiles, t => t.X == 64 && t.Y == 16);
        Assert.Contains(snapshot.FarmWork.TilledUnwateredTiles, t => t.X == 64 && t.Y == 17);
        Assert.DoesNotContain(snapshot.FarmWork.TilledUnwateredTiles, t => t.X == 65);
    }

    [Fact]
    public void CaptureCurrentSnapshot_CarriesFarmWork_OverLimit_TruncatesTo64()
    {
        var (coordinator, _, observer) = CreateHarness();

        // Create 80 dry tiles
        for (int i = 0; i < 80; i++)
        {
            observer.SetDirt(new TileCoordinate(10 + (i % 10), 20 + (i / 10)), TileDirtState.DryDirt());
        }

        var snapshot = coordinator.CaptureCurrentSnapshot(worldRevision: 3);

        Assert.NotNull(snapshot.FarmWork);
        Assert.Equal(80, snapshot.FarmWork.TilledUnwateredCount);
        Assert.Equal(64, snapshot.FarmWork.TilledUnwateredTiles.Count);
        Assert.True(snapshot.FarmWork.IsTruncated);
        Assert.True(snapshot.FarmWork.Truncated);
    }

    [Fact]
    public void TryAcceptSkillExecute_ValidHoeTiles_AcceptedAndExecuted()
    {
        var (coordinator, _, observer) = CreateHarness(giveHoe: true);

        var t1 = new TileCoordinate(10, 12);

        var payload = new SkillExecutePayload(
            CommandId: "cmd-hoe-001",
            TaskId: "task-hoe-01",
            SkillId: "hoe-tiles",
            SkillVersion: "0.1",
            ExpectedWorldRevision: 1,
            Parameters: new WaterZoneParameters("Farm", new List<TileCoord> { new(10, 12) }),
            Budgets: new ExecutionBudgets(60, 50f, 0),
            CancelPolicy: "safe-point",
            PolicyDecisionId: "policy-hoe"
        );

        var envelope = CreateEnvelope("skill.execute", "msg-hoe-1", "idem-hoe-1");

        bool accepted = coordinator.TryAcceptSkillExecute(payload, envelope, out var rejectResult);

        Assert.True(accepted);
        Assert.Null(rejectResult);

        coordinator.HoeMachine!.StepTicks(100);

        Assert.NotNull(coordinator.LastSkillResultPayload);
        Assert.Equal("succeeded", coordinator.LastSkillResultPayload.TerminalState);
        Assert.Equal(1, coordinator.LastSkillResultPayload.CompletedCount);
        Assert.True(observer.GetDirtState("Farm", t1).IsTilled);
    }

    [Fact]
    public void TryAcceptSkillExecute_ValidPlantSeeds_AcceptedAndExecuted()
    {
        var (coordinator, actor, observer) = CreateHarness(givePlant: true, seedItemId: "472", seedCount: 5);

        var t1 = new TileCoordinate(10, 12);
        observer.SetDirt(t1, TileDirtState.DryDirt());

        var parameters = new WaterZoneParameters("Farm", new List<TileCoord> { new(10, 12) })
        {
            SeedItemId = "472"
        };

        var payload = new SkillExecutePayload(
            CommandId: "cmd-plant-001",
            TaskId: "task-plant-01",
            SkillId: "plant-seeds",
            SkillVersion: "0.1",
            ExpectedWorldRevision: 1,
            Parameters: parameters,
            Budgets: new ExecutionBudgets(60, 50f, 0),
            CancelPolicy: "safe-point",
            PolicyDecisionId: "policy-plant"
        );

        var envelope = CreateEnvelope("skill.execute", "msg-plant-1", "idem-plant-1");

        bool accepted = coordinator.TryAcceptSkillExecute(payload, envelope, out var rejectResult);

        Assert.True(accepted);
        Assert.Null(rejectResult);

        coordinator.PlantMachine!.StepTicks(100);

        Assert.NotNull(coordinator.LastSkillResultPayload);
        Assert.Equal("succeeded", coordinator.LastSkillResultPayload.TerminalState);
        Assert.Equal(1, coordinator.LastSkillResultPayload.CompletedCount);
        Assert.Equal(4, actor.GetItemCount("472"));
        Assert.True(observer.GetDirtState("Farm", t1).HasCrop);
    }

    [Fact]
    public void CaptureCurrentSnapshot_IncludesPlantingScan()
    {
        var (coordinator, _, observer) = CreateHarness(giveHoe: true, seedItemId: "472", seedCount: 10);

        var t1 = new TileCoordinate(10, 11);
        var t2 = new TileCoordinate(10, 12);
        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetTillable(t2, true);

        var snapshot = coordinator.CaptureCurrentSnapshot(worldRevision: 5);

        Assert.NotNull(snapshot.Planting);
        Assert.True(snapshot.Planting.CompanionHasHoe);
        Assert.Single(snapshot.Planting.Seeds);
        Assert.Equal("472", snapshot.Planting.Seeds[0].ItemId);
        Assert.Equal(10, snapshot.Planting.Seeds[0].Stack);
        Assert.Contains(snapshot.Planting.CandidateTiles.TilledEmptyTiles, t => t.X == 10 && t.Y == 11);
    }

    [Fact]
    public void CaptureCurrentSnapshot_IncludesShopScan()
    {
        var (coordinator, _, observer) = CreateHarness();

        observer.SetShop("SeedShop", new ShopScanInfo(
            ShopId: "SeedShop",
            Status: "ok",
            IsOpen: true,
            OwnerPresent: true,
            ClosedMessage: null,
            Owners: new[] { "Pierre" },
            Currency: 0,
            AvailableMoney: 1000,
            MoneyStatus: "ok",
            Items: new[]
            {
                new ShopItemInfo(
                    ItemId: "(O)472",
                    Name: "Parsnip Seeds",
                    Price: 20,
                    Stock: int.MaxValue,
                    IsInfiniteStock: true
                )
            }
        ));

        var snapshot = coordinator.CaptureCurrentSnapshot(worldRevision: 6);

        Assert.NotNull(snapshot.Shop);
        Assert.Equal("SeedShop", snapshot.Shop.ShopId);
        Assert.Equal("ok", snapshot.Shop.Status);
        Assert.True(snapshot.Shop.IsOpen);
        Assert.True(snapshot.Shop.OwnerPresent);
        Assert.Equal(1000, snapshot.Shop.AvailableMoney);
        Assert.Single(snapshot.Shop.Items);
        Assert.Equal("(O)472", snapshot.Shop.Items[0].ItemId);
        Assert.Equal(20, snapshot.Shop.Items[0].Price);
        Assert.True(snapshot.Shop.Items[0].IsInfiniteStock);
    }

    [Fact]
    public void TryAcceptSkillExecute_ValidShipItems_AcceptedAndExecuted()
    {
        var pose = new AuthoritativePose("Farm", 71, 15, FacingDirection.Up);
        var (coordinator, actor, _) = CreateHarness(
            giveShipping: true, shipItemId: "(O)24", shipItemCount: 4, initialPose: pose);

        var parameters = new WaterZoneParameters("Farm")
        {
            Items = new List<ShippingItemRequest> { new("(O)24", 2) }
        };

        var payload = new SkillExecutePayload(
            CommandId: "cmd-ship-001",
            TaskId: "task-ship-01",
            SkillId: "ship-items",
            SkillVersion: "0.1",
            ExpectedWorldRevision: 1,
            Parameters: parameters,
            Budgets: new ExecutionBudgets(60, 50f, 0),
            CancelPolicy: "safe-point",
            PolicyDecisionId: "policy-ship"
        );

        var envelope = CreateEnvelope("skill.execute", "msg-ship-1", "idem-ship-1");

        bool accepted = coordinator.TryAcceptSkillExecute(payload, envelope, out var rejectResult);

        Assert.True(accepted);
        Assert.Null(rejectResult);

        coordinator.ShippingMachine!.Update(null, 1);
        coordinator.ShippingMachine!.Update(null, 2);

        Assert.NotNull(coordinator.LastSkillResultPayload);
        Assert.Equal("succeeded", coordinator.LastSkillResultPayload.TerminalState);
        Assert.Equal(2, coordinator.LastSkillResultPayload.CompletedCount);
        Assert.Equal(2, actor.GetItemCount("(O)24"));
    }

    private sealed class TestTransportServer : ITransportServer
    {
        public int Port => 0;
        public string SessionToken => "test-token";
        public TransportState State => TransportState.Ready;
        public bool IsClientConnected => true;
        public string? ActiveCommandId => null;

        public List<(WorldSnapshotPayload Snapshot, long WorldRevision)> SentSnapshots { get; } = new();

        public void Dispose() { }
        public void Start() { }
        public void Stop() { }
        public void Update() { }

        public Task SendSnapshotAsync(WorldSnapshotPayload snapshot, long worldRevision)
        {
            SentSnapshots.Add((snapshot, worldRevision));
            return Task.CompletedTask;
        }

        public Task SendSkillResultAsync(SkillResultPayload result, string correlationId, string? idempotencyKey = null) => Task.CompletedTask;
        public Task SendProtocolErrorAsync(string code, string message, string? correlationId = null) => Task.CompletedTask;

        public bool IsChatConnected => false;
        public Task<bool> SendChatSubmitAsync(ChatSubmitPayload payload) => Task.FromResult(true);
        public Task<bool> SendChatCancelAsync(ChatCancelPayload payload) => Task.FromResult(true);
        public event Action<ChatReplyPayload>? OnChatReplyReceived { add { } remove { } }
    }

    [Fact]
    public void OnTimeChanged_PushesFreshSnapshot_WithoutBumpingWorldRevision()
    {
        var (coordinator, _, observer) = CreateHarness();
        var transport = new TestTransportServer();
        coordinator.SetTransportServer(transport);

        int initialRev = observer.WorldRevision;

        observer.TimeOfDay = 900;
        coordinator.OnTimeChanged(900);

        Assert.Single(transport.SentSnapshots);
        Assert.Equal(initialRev, transport.SentSnapshots[0].WorldRevision);
        Assert.Equal(initialRev, observer.WorldRevision);
        Assert.Equal(900, transport.SentSnapshots[0].Snapshot.World.TimeOfDay);

        // Multiple time changes also do not bump revision
        observer.TimeOfDay = 910;
        coordinator.OnTimeChanged(910);
        Assert.Equal(2, transport.SentSnapshots.Count);
        Assert.Equal(initialRev, transport.SentSnapshots[1].WorldRevision);
        Assert.Equal(initialRev, observer.WorldRevision);
        Assert.Equal(910, transport.SentSnapshots[1].Snapshot.World.TimeOfDay);
    }

    [Fact]
    public void OnTimeChanged_WhenTransportServerNull_DoesNotThrow()
    {
        var (coordinator, _, _) = CreateHarness();
        var exception = Record.Exception(() => coordinator.OnTimeChanged(900));
        Assert.Null(exception);
    }
}
