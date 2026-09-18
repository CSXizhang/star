using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.GameData.Shops;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Observation;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class PurchaseStateMachineTests
{
    private static (PurchaseStateMachine machine, MechanicsActor actor, SimulatedWorldObserver observer, TestPurchaseAdapter adapter)
        CreateTestContext(TileCoordinate actorTile, string location = "SeedShop")
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = location };
        var actor = new MechanicsActor(
            initialPose: new AuthoritativePose(location, actorTile.X, actorTile.Y, FacingDirection.Up),
            stamina: 270f,
            water: 40);
        var adapter = new TestPurchaseAdapter
        {
            CounterTiles = new List<TileCoordinate> { new(10, 11) },
            AvailableMoney = 1000
        };
        var machine = new PurchaseStateMachine(actor, observer, adapter);
        return (machine, actor, observer, adapter);
    }

    [Fact]
    public void Start_RejectsEmptyOrInvalidItems()
    {
        var (machine, _, _, _) = CreateTestContext(new TileCoordinate(10, 12));

        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: Array.Empty<PurchaseItemRequest>()
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.False(started);
        Assert.NotNull(earlyResult);
        Assert.Equal(ExecutionState.Rejected, earlyResult.FinalState);
        Assert.Equal("EMPTY_ITEMS", earlyResult.ErrorCode);
    }

    [Fact]
    public void Start_RejectsInvalidCountInItems()
    {
        var (machine, _, _, _) = CreateTestContext(new TileCoordinate(10, 12));

        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 0) }
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.False(started);
        Assert.NotNull(earlyResult);
        Assert.Equal(ExecutionState.Rejected, earlyResult.FinalState);
        Assert.Equal("INVALID_ITEM", earlyResult.ErrorCode);
    }

    [Fact]
    public void Start_RejectsNonPositiveBudget()
    {
        var (machine, _, _, _) = CreateTestContext(new TileCoordinate(10, 12));

        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 0,
            Items: new[] { new PurchaseItemRequest("(O)472", 1) }
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.False(started);
        Assert.NotNull(earlyResult);
        Assert.Equal(ExecutionState.Rejected, earlyResult.FinalState);
        Assert.Equal("INVALID_BUDGET", earlyResult.ErrorCode);
    }

    [Fact]
    public void Start_RejectsLocationMismatch()
    {
        var (machine, _, _, _) = CreateTestContext(new TileCoordinate(10, 12), location: "Farm");

        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 2) }
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.False(started);
        Assert.NotNull(earlyResult);
        Assert.Equal(ExecutionState.Rejected, earlyResult.FinalState);
        Assert.Equal("LOCATION_MISMATCH", earlyResult.ErrorCode);
    }

    [Fact]
    public void Start_RejectsNotAdjacentToCounter()
    {
        // Counter is at (10, 11); actor at (20, 20) is not adjacent
        var (machine, _, _, _) = CreateTestContext(new TileCoordinate(20, 20));

        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 2) }
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.False(started);
        Assert.NotNull(earlyResult);
        Assert.Equal(ExecutionState.Rejected, earlyResult.FinalState);
        Assert.Equal("NOT_ADJACENT_TO_COUNTER", earlyResult.ErrorCode);
    }

    [Fact]
    public void Update_RejectsClosedShop()
    {
        // Actor at (10, 12) adjacent to counter (10, 11)
        var (machine, _, _, adapter) = CreateTestContext(new TileCoordinate(10, 12));
        adapter.CustomShopScan = new ShopScanInfo(
            ShopId: "SeedShop",
            Status: "ok",
            IsOpen: false,
            OwnerPresent: false,
            ClosedMessage: "Pierre's General Store is closed at 6:20.",
            Owners: new[] { "Pierre" },
            Currency: 0,
            AvailableMoney: 500,
            MoneyStatus: "ok",
            Items: Array.Empty<ShopItemInfo>()
        );

        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 2) }
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.True(started);
        Assert.Null(earlyResult);

        // Tick 1: Facing -> Acting
        machine.Update(null, 1);
        // Tick 2: Acting executes purchase
        machine.Update(null, 2);

        Assert.Equal(ExecutionState.Rejected, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal(ExecutionState.Rejected, machine.FinalResult.FinalState);
        Assert.Equal("SHOP_CLOSED", machine.FinalResult.ErrorCode);
        Assert.Single(machine.FinalResult.SkippedItems);
        Assert.Equal("shop-closed", machine.FinalResult.SkippedItems[0].Reason);
    }

    [Fact]
    public void Update_RejectsItemNotInStock()
    {
        var (machine, _, _, adapter) = CreateTestContext(new TileCoordinate(10, 12));
        adapter.Stock = new Dictionary<ISalable, ItemStockInformation>(); // empty stock

        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 2) }
        );

        machine.Start(request, out _);
        machine.Update(null, 1);
        machine.Update(null, 2);

        Assert.Equal(ExecutionState.Rejected, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal("NO_PURCHASEABLE_ITEMS", machine.FinalResult.ErrorCode);
        Assert.Single(machine.FinalResult.SkippedItems);
        Assert.Equal("item-not-in-stock", machine.FinalResult.SkippedItems[0].Reason);
    }

    [Fact]
    public void Update_RejectsLimitedStockItem()
    {
        var (machine, _, _, adapter) = CreateTestContext(new TileCoordinate(10, 12));
        var salable = new TestPurchaseSalable { QualifiedItemId = "(O)472", DisplayName = "Parsnip Seeds" };
        adapter.Stock = new Dictionary<ISalable, ItemStockInformation>
        {
            [salable] = new ItemStockInformation(price: 20, stock: 1, stockMode: LimitedStockMode.Global)
        };

        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 1) }
        );

        machine.Start(request, out _);
        machine.Update(null, 1);
        machine.Update(null, 2);

        Assert.Equal(ExecutionState.Rejected, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Single(machine.FinalResult.SkippedItems);
        Assert.Equal("limited-stock-unsupported", machine.FinalResult.SkippedItems[0].Reason);
    }

    [Fact]
    public void Update_AllowsInfiniteStock_WithGlobalLimitedStockMode()
    {
        var (machine, _, _, adapter) = CreateTestContext(new TileCoordinate(10, 12));
        var salable = new TestPurchaseSalable { QualifiedItemId = "(O)472", DisplayName = "Parsnip Seeds" };
        // Pierre's shop items in 1.6: stock is int.MaxValue with LimitedStockMode.Global
        adapter.Stock = new Dictionary<ISalable, ItemStockInformation>
        {
            [salable] = new ItemStockInformation(price: 20, stock: int.MaxValue, stockMode: LimitedStockMode.Global)
        };

        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 2) }
        );

        machine.Start(request, out _);
        machine.Update(null, 1);
        machine.Update(null, 2);

        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal(2, machine.FinalResult.PurchasedItems[0].Count);
        Assert.Empty(machine.FinalResult.SkippedItems);
    }

    [Fact]
    public void Update_AllowsInfiniteStock_WithSyncedKey()
    {
        var (machine, _, _, adapter) = CreateTestContext(new TileCoordinate(10, 12));
        var salable = new TestPurchaseSalable { QualifiedItemId = "(O)472", DisplayName = "Parsnip Seeds" };
        adapter.Stock = new Dictionary<ISalable, ItemStockInformation>
        {
            [salable] = new ItemStockInformation(20, int.MaxValue, null, null, LimitedStockMode.Global, "shared_shop_seed")
        };

        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 2) }
        );

        machine.Start(request, out _);
        machine.Update(null, 1);
        machine.Update(null, 2);

        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal(2, machine.FinalResult.PurchasedItems[0].Count);
        Assert.Empty(machine.FinalResult.SkippedItems);
    }

    [Fact]
    public void Update_RejectsFiniteStock_WithSyncedKey()
    {
        var (machine, _, _, adapter) = CreateTestContext(new TileCoordinate(10, 12));
        var salable = new TestPurchaseSalable { QualifiedItemId = "(O)472", DisplayName = "Parsnip Seeds" };
        adapter.Stock = new Dictionary<ISalable, ItemStockInformation>
        {
            [salable] = new ItemStockInformation(20, 1, null, null, LimitedStockMode.Global, "shared_shop_seed")
        };

        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 2) }
        );

        machine.Start(request, out _);
        machine.Update(null, 1);
        machine.Update(null, 2);

        Assert.Equal(ExecutionState.Rejected, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Single(machine.FinalResult.SkippedItems);
        Assert.Equal("limited-stock-unsupported", machine.FinalResult.SkippedItems[0].Reason);
    }

    [Fact]
    public void Update_RejectsBudgetExceeded()
    {
        var (machine, _, _, adapter) = CreateTestContext(new TileCoordinate(10, 12));
        var salable = new TestPurchaseSalable { QualifiedItemId = "(O)472", DisplayName = "Parsnip Seeds" };
        adapter.Stock = new Dictionary<ISalable, ItemStockInformation>
        {
            [salable] = new ItemStockInformation(price: 20, stock: int.MaxValue, stockMode: LimitedStockMode.None)
        };

        // 6 seeds * 20g = 120g > budget 100g
        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 6) }
        );

        machine.Start(request, out _);
        machine.Update(null, 1);
        machine.Update(null, 2);

        Assert.Equal(ExecutionState.Rejected, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal("BUDGET_EXCEEDED", machine.FinalResult.ErrorCode);
        Assert.Single(machine.FinalResult.SkippedItems);
        Assert.Equal("budget-exceeded", machine.FinalResult.SkippedItems[0].Reason);
    }

    [Fact]
    public void Update_RejectsInsufficientFunds()
    {
        var (machine, _, _, adapter) = CreateTestContext(new TileCoordinate(10, 12));
        adapter.AvailableMoney = 30; // only 30g
        var salable = new TestPurchaseSalable { QualifiedItemId = "(O)472", DisplayName = "Parsnip Seeds" };
        adapter.Stock = new Dictionary<ISalable, ItemStockInformation>
        {
            [salable] = new ItemStockInformation(price: 20, stock: int.MaxValue, stockMode: LimitedStockMode.None)
        };

        // 2 seeds * 20g = 40g > 30g available
        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 2) }
        );

        machine.Start(request, out _);
        machine.Update(null, 1);
        machine.Update(null, 2);

        Assert.Equal(ExecutionState.Rejected, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal("INSUFFICIENT_FUNDS", machine.FinalResult.ErrorCode);
        Assert.Single(machine.FinalResult.SkippedItems);
        Assert.Equal("insufficient-funds", machine.FinalResult.SkippedItems[0].Reason);
    }

    [Fact]
    public void Update_ExecutesSuccessfulPurchase()
    {
        var (machine, actor, _, adapter) = CreateTestContext(new TileCoordinate(10, 12));
        adapter.AvailableMoney = 500;
        var salable = new TestPurchaseSalable { QualifiedItemId = "(O)472", DisplayName = "Parsnip Seeds" };
        adapter.Stock = new Dictionary<ISalable, ItemStockInformation>
        {
            [salable] = new ItemStockInformation(price: 20, stock: int.MaxValue, stockMode: LimitedStockMode.None)
        };

        // Buy 2 seeds at 20g each = 40g, budget 100
        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 2) }
        );

        machine.Start(request, out _);
        machine.Update(null, 1);
        machine.Update(null, 2);

        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal(ExecutionState.Succeeded, machine.FinalResult.FinalState);
        Assert.Equal(40, machine.FinalResult.TotalCost);
        Assert.Equal(60, machine.FinalResult.RemainingBudget);
        Assert.Single(machine.FinalResult.PurchasedItems);
        Assert.Equal("(O)472", machine.FinalResult.PurchasedItems[0].ItemId);
        Assert.Equal(2, machine.FinalResult.PurchasedItems[0].Count);
        Assert.Equal(20, machine.FinalResult.PurchasedItems[0].UnitPrice);
        Assert.Equal(40, machine.FinalResult.PurchasedItems[0].Subtotal);

        // Verify actor inventory received the 2 seeds
        Assert.Equal(2, actor.GetItemCount("(O)472"));
        // Verify wallet balance
        Assert.Equal(460, adapter.AvailableMoney);
        Assert.Equal(40, adapter.TotalDeducted);
    }

    [Fact]
    public void Update_RollbackOnDeductionFailure()
    {
        var (machine, actor, _, adapter) = CreateTestContext(new TileCoordinate(10, 12));
        adapter.AvailableMoney = 500;
        adapter.FailDeductMoney = true;

        var salable = new TestPurchaseSalable { QualifiedItemId = "(O)472", DisplayName = "Parsnip Seeds" };
        adapter.Stock = new Dictionary<ISalable, ItemStockInformation>
        {
            [salable] = new ItemStockInformation(price: 20, stock: int.MaxValue, stockMode: LimitedStockMode.None)
        };

        var request = new PurchaseRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 2) }
        );

        machine.Start(request, out _);
        machine.Update(null, 1);
        machine.Update(null, 2);

        Assert.Equal(ExecutionState.Failed, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal("DEDUCTION_FAILED", machine.FinalResult.ErrorCode);
        Assert.Equal(0, actor.GetItemCount("(O)472"));
    }

    [Fact]
    public void Start_RejectsCounterNotFound_WhenCounterTilesEmpty()
    {
        var (machine, _, _, adapter) = CreateTestContext(new TileCoordinate(10, 12));
        adapter.CounterTiles = new List<TileCoordinate>(); // No counter exists

        var request = new PurchaseRequest(
            CommandId: "cmd-no-counter",
            TaskId: "task-no-counter",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 2) }
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.False(started);
        Assert.NotNull(earlyResult);
        Assert.Equal(ExecutionState.Rejected, earlyResult.FinalState);
        Assert.Equal("COUNTER_NOT_FOUND", earlyResult.ErrorCode);
        Assert.Contains("No shop counter for 'SeedShop' found", earlyResult.ErrorMessage);
    }

    [Fact]
    public void RejectionSemantics_WhenCounterExistsButShopClosed_RejectsWithShopClosed_NeverCounterNotFound()
    {
        // Counter is present at (10, 11), actor is adjacent at (10, 12)
        var (machine, _, _, adapter) = CreateTestContext(new TileCoordinate(10, 12));
        adapter.CounterTiles = new List<TileCoordinate> { new(10, 11) };
        adapter.CustomShopScan = new ShopScanInfo(
            ShopId: "SeedShop",
            Status: "ok",
            IsOpen: false,
            OwnerPresent: false,
            ClosedMessage: "Come back when Pierre's tending the shop.",
            Owners: new[] { "Pierre" },
            Currency: 0,
            AvailableMoney: 500,
            MoneyStatus: "ok",
            Items: Array.Empty<ShopItemInfo>()
        );

        var request = new PurchaseRequest(
            CommandId: "cmd-closed",
            TaskId: "task-closed",
            ShopId: "SeedShop",
            LocationId: "SeedShop",
            BudgetLimit: 100,
            Items: new[] { new PurchaseItemRequest("(O)472", 2) }
        );

        // Preflight: counter adjacency passes, machine starts without COUNTER_NOT_FOUND
        bool started = machine.Start(request, out var earlyResult);
        Assert.True(started);
        Assert.Null(earlyResult);

        // Tick 1: Facing -> Acting
        machine.Update(null, 1);
        // Tick 2: Acting executes ReevaluateShop -> closed rejection
        machine.Update(null, 2);

        Assert.Equal(ExecutionState.Rejected, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal(ExecutionState.Rejected, machine.FinalResult.FinalState);

        // Must be SHOP_CLOSED with shop-closed reason, NEVER COUNTER_NOT_FOUND
        Assert.Equal("SHOP_CLOSED", machine.FinalResult.ErrorCode);
        Assert.NotEqual("COUNTER_NOT_FOUND", machine.FinalResult.ErrorCode);
        Assert.Equal("Come back when Pierre's tending the shop.", machine.FinalResult.ErrorMessage);
        Assert.Single(machine.FinalResult.SkippedItems);
        Assert.Equal("shop-closed", machine.FinalResult.SkippedItems[0].Reason);

        // Transport payload verification
        var payload = machine.FinalResult.ToTransportPayload();
        Assert.Equal("rejected", payload.TerminalState);
        Assert.Equal("SHOP_CLOSED", payload.Error?.Code);
        Assert.NotNull(payload.Details);
        Assert.Equal("shop-closed", payload.Details!["skipReason"]);
        Assert.Equal("shop-closed", payload.Details!["rejectionReason"]);
    }

    [Fact]
    public void CounterDiscovery_ConsistencyBetweenAdapterAndObserver()
    {
        var monitor = new TestMonitor();
        var observer = new GameWorldObserver(monitor);
        var expectedTiles = new List<TileCoordinate> { new(4, 18), new(5, 18) };

        observer.OpenShopActionResolver = (shopId, _) =>
            (new Rectangle(4, 17, 2, 2), 900, 1700, true);
        observer.CounterTilesResolver = (shopId, _) => expectedTiles;

        var adapter = new NormalPurchaseAdapter(observer, monitor);

        // 1. Calling observer directly vs calling adapter returns identical coordinates
        var observerTiles = observer.GetShopCounterTiles("SeedShop", "SeedShop");
        var adapterTiles = adapter.GetShopCounterTiles("SeedShop", "SeedShop");

        Assert.Equal(expectedTiles.Count, observerTiles.Count);
        Assert.Equal(expectedTiles.Count, adapterTiles.Count);
        Assert.Equal(observerTiles, adapterTiles);

        // 2. Both share the exact same metadata and cache
        var meta = observer.GetShopActionMetadata("SeedShop", "SeedShop");
        Assert.True(meta.ActionFound);
        Assert.Equal(expectedTiles, meta.CounterTiles);
        Assert.Equal(900, meta.OpenTime);
        Assert.Equal(1700, meta.CloseTime);
    }

    [Fact]
    public void CounterDiscovery_BuyAction_ConsistencyBetweenAdapterAndObserver()
    {
        var monitor = new TestMonitor();
        var observer = new GameWorldObserver(monitor);
        var expectedTiles = new List<TileCoordinate> { new(4, 18), new(5, 18) };

        observer.ShopActionResolver = (shopId, _) => new ShopActionMetadata(
            LocationName: "SeedShop",
            OwnerArea: new Rectangle(4, 17, 1, 1),
            OpenTime: -1,
            CloseTime: -1,
            ActionFound: true,
            CounterTiles: expectedTiles
        );

        var adapter = new NormalPurchaseAdapter(observer, monitor);

        var observerTiles = observer.GetShopCounterTiles("SeedShop", "SeedShop");
        var adapterTiles = adapter.GetShopCounterTiles("SeedShop", "SeedShop");

        Assert.Equal(expectedTiles, observerTiles);
        Assert.Equal(expectedTiles, adapterTiles);

        var meta = observer.GetShopActionMetadata("SeedShop", "SeedShop");
        Assert.True(meta.ActionFound);
        Assert.Equal(new Rectangle(4, 17, 1, 1), meta.OwnerArea);
        Assert.Equal(expectedTiles, meta.CounterTiles);
    }
}
