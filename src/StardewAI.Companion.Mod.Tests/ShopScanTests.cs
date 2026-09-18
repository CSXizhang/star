using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;
using StardewValley;
using StardewValley.GameData.Shops;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class ShopScanTests
{
    private sealed class TestSalable : ISalable
    {
        public string TypeDefinitionId { get; set; } = "(O)";
        public string QualifiedItemId { get; set; } = "(O)472";
        public string DisplayName { get; set; } = "Parsnip Seeds";
        public string Name { get; set; } = "Parsnip Seeds";
        public bool IsRecipe { get; set; } = false;
        public int Stack { get; set; } = 1;
        public int Quality { get; set; } = 0;
        public bool IsInfinite { get; set; } = false;

        public string GetItemTypeId() => TypeDefinitionId;
        public bool ShouldDrawIcon() => true;
        public void drawInMenu(SpriteBatch spriteBatch, Vector2 location, float scaleSize, float transparency, float layerDepth, StackDrawType drawStackNumber, Color color, bool drawShadow) { }
        public string getDescription() => "Plant these in the spring.";
        public int maximumStackSize() => 999;
        public int addToStack(Item stack) => 0;
        public int sellToStorePrice(long specificPlayerID = -1) => 10;
        public int salePrice(bool ignoreProfitMargins = false) => 20;
        public bool appliesProfitMargins() => false;
        public bool actionWhenPurchased(string shopId) => false;
        public bool canStackWith(ISalable other) => false;
        public bool CanBuyItem(Farmer farmer) => true;
        public bool IsInfiniteStock() => IsInfinite;
        public ISalable GetSalableInstance() => this;
        public void FixStackSize() { }
        public void FixQuality() { }
    }

    private static (GameWorldObserver observer, IFarmerActor actor) CreateHarness(Farmer? gameFarmer = null)
    {
        var monitor = new TestMonitor();
        var observer = new GameWorldObserver(monitor);
        var actor = new TestFarmerActor(gameFarmer);
        return (observer, actor);
    }



    private sealed class TestFarmerActor : IFarmerActor
    {
        public Farmer? GameFarmer { get; }

        public TestFarmerActor(Farmer? gameFarmer = null)
        {
            GameFarmer = gameFarmer;
        }

        public string CompanionId => "companion-1";
        public float Stamina { get; set; } = 270f;
        public int MaxStamina => 270;
        public int WaterLeft { get; set; } = 40;
        public int MaxWater => 40;
        public string LocationName { get; set; } = "Farm";
        public Vector2 PixelPosition { get; set; } = Vector2.Zero;
        public TileCoordinate Tile { get; set; } = new(10, 10);
        public FacingDirection Facing { get; set; } = FacingDirection.Down;
        public bool IsExhausted => false;
        public bool IsWateringCanEmpty => false;
        public string? ActiveTaskId => null;
        public void SetActiveTask(string? taskId) { }
        public void MovePixels(float dx, float dy) { }
        public void Face(FacingDirection dir) { }
        public void Halt() { }
        public void SetLocation(string locationName, TileCoordinate tile)
        {
            LocationName = locationName;
            Tile = tile;
            PixelPosition = new Vector2(tile.X * 64, tile.Y * 64);
        }
        public StardewValley.Tools.WateringCan? WateringCan => null;
        public StardewValley.Tools.Hoe? Hoe => null;
        public T? FindTool<T>() where T : StardewValley.Tool => null;
        public IReadOnlyList<string> GetToolNames() => Array.Empty<string>();
        public bool IsUsingTool => false;
        public int CurrentFrame => 0;
        public ToolAnimationPhase AnimationPhase => ToolAnimationPhase.None;
        public void BeginUsingTool() { }
        public ToolAnimationPhase UpdateToolAnimation(GameTime? time, long tickCount) => ToolAnimationPhase.None;
        public void EndUsingTool() { }
        public void ApplyPersistentState(CompanionActorState state) { }
        public CompanionActorState CapturePersistentState() => new();
        public int InventoryCapacity => 36;
        public int FreeInventorySlots => 36;
        public IReadOnlyList<InventoryItem> GetInventorySnapshot() => Array.Empty<InventoryItem>();
        public bool TryAddItemToInventory(InventoryItem item) => true;
        public bool TryAddItemToInventory(Item gameItem) => true;
        public bool TryRemoveItemAtSlot(int slotIndex, int stackToRemove, out InventoryItem? removed) { removed = null; return false; }
        public bool TryConsumeItem(string itemId, int count = 1) => false;
        public bool TryExtractItem(string itemId, int count, out List<Item> extractedItems) { extractedItems = new(); return false; }
        public int GetItemCount(string itemId) => 0;
    }

    [Fact]
    public void ScanShop_UnknownShopId_ReturnsUnknownStatus_AndListsAvailableShops()
    {
        var (observer, actor) = CreateHarness();
        var knownShops = new Dictionary<string, ShopData>
        {
            ["SeedShop"] = new() { Currency = 0 },
            ["Saloon"] = new() { Currency = 0 }
        };
        observer.ShopDataResolver = _ => knownShops;

        var scan = observer.ScanShop("JojaMart", actor);

        Assert.Equal("JojaMart", scan.ShopId);
        Assert.Equal("unknown", scan.Status);
        Assert.False(scan.IsOpen);
        Assert.False(scan.OwnerPresent);
        Assert.Empty(scan.Items);
        Assert.NotNull(scan.ErrorMessage);
        Assert.Contains("Unknown shopId 'JojaMart'", scan.ErrorMessage);
        Assert.Contains("Saloon, SeedShop", scan.ErrorMessage);
    }

    [Fact]
    public void ScanShop_ShopDataLoadError_ReturnsErrorStatus()
    {
        var (observer, actor) = CreateHarness();
        observer.ShopDataResolver = _ => throw new InvalidOperationException("Game content asset missing");

        var scan = observer.ScanShop("SeedShop", actor);

        Assert.Equal("SeedShop", scan.ShopId);
        Assert.Equal("error", scan.Status);
        Assert.False(scan.IsOpen);
        Assert.False(scan.OwnerPresent);
        Assert.NotNull(scan.ErrorMessage);
        Assert.Contains("Game content asset missing", scan.ErrorMessage);
    }

    [Fact]
    public void ScanShop_KnownShop_WhenOwnerAtCounterDuringHours_ReturnsOpenTrue_AndOwnerPresentTrue()
    {
        var (observer, actor) = CreateHarness();
        var shopData = new ShopData
        {
            Currency = 0,
            Owners = new List<ShopOwnerData>
            {
                new() { Id = "Pierre", Name = "Pierre" }
            }
        };
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["SeedShop"] = shopData };
        observer.ShopOwnersResolver = _ => new List<ShopOwnerData> { new() { Id = "Pierre", Name = "Pierre" } };
        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>();
        observer.OpenShopActionResolver = (_, _) => (new Rectangle(4, 17, 2, 2), 900, 1700, true);
        observer.TimeOfDayResolver = () => 1000;
        observer.ShopNpcsResolver = _ => new[] { new ShopNpcInfo("Pierre", new Point(4, 17)) };

        var scan = observer.ScanShop("SeedShop", actor);

        Assert.Equal("SeedShop", scan.ShopId);
        Assert.Equal("ok", scan.Status);
        Assert.True(scan.IsOpen);
        Assert.True(scan.OwnerPresent);
        Assert.Null(scan.ClosedMessage);
        Assert.Contains("Pierre", scan.Owners);
    }

    [Fact]
    public void ScanShop_KnownShop_WhenOwnerNotInCounterArea_ReturnsOpenFalse_AndOwnerPresentFalse()
    {
        var (observer, actor) = CreateHarness();
        var shopData = new ShopData
        {
            Currency = 0,
            Owners = new List<ShopOwnerData>
            {
                new() { Id = "Pierre", Name = "Pierre", ClosedMessage = "Pierre's store is closed on Wednesdays." }
            }
        };
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["SeedShop"] = shopData };
        observer.ShopOwnersResolver = _ => new List<ShopOwnerData> { new() { Id = "Pierre", Name = "Pierre", ClosedMessage = "Pierre's store is closed on Wednesdays." } };
        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>();
        observer.OpenShopActionResolver = (_, _) => (new Rectangle(4, 17, 2, 2), 900, 1700, true);
        observer.TimeOfDayResolver = () => 1000;
        // Pierre is in bedroom at (0, 0), NOT at counter (4, 17, 2, 2)
        observer.ShopNpcsResolver = _ => new[] { new ShopNpcInfo("Pierre", new Point(0, 0)) };

        var scan = observer.ScanShop("SeedShop", actor);

        Assert.Equal("SeedShop", scan.ShopId);
        Assert.Equal("ok", scan.Status);
        Assert.False(scan.IsOpen);
        Assert.False(scan.OwnerPresent);
        Assert.Equal("Pierre's store is closed on Wednesdays.", scan.ClosedMessage);
    }

    [Fact]
    public void ScanShop_KnownShop_WhenOwnerAtCounter_ButBeforeOpeningTime_ReturnsOpenFalse()
    {
        var (observer, actor) = CreateHarness();
        var shopData = new ShopData
        {
            Currency = 0,
            Owners = new List<ShopOwnerData>
            {
                new() { Id = "Pierre", Name = "Pierre", ClosedMessage = "Closed until 9:00 AM." }
            }
        };
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["SeedShop"] = shopData };
        observer.ShopOwnersResolver = _ => new List<ShopOwnerData> { new() { Id = "Pierre", Name = "Pierre", ClosedMessage = "Closed until 9:00 AM." } };
        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>();
        observer.OpenShopActionResolver = (_, _) => (new Rectangle(4, 17, 2, 2), 900, 1700, true);
        observer.TimeOfDayResolver = () => 630; // 6:30 AM < 900
        observer.ShopNpcsResolver = _ => new[] { new ShopNpcInfo("Pierre", new Point(4, 17)) };

        var scan = observer.ScanShop("SeedShop", actor);

        Assert.Equal("SeedShop", scan.ShopId);
        Assert.Equal("ok", scan.Status);
        Assert.False(scan.IsOpen);
        Assert.True(scan.OwnerPresent);
        Assert.Equal("Closed until 9:00 AM.", scan.ClosedMessage);
    }

    [Fact]
    public void ScanShop_KnownShop_WhenNoActionOrOwnerArea_ReturnsUnknownStatus_AndOpenFalse()
    {
        var (observer, actor) = CreateHarness();
        var shopData = new ShopData
        {
            Currency = 0,
            Owners = new List<ShopOwnerData>
            {
                new() { Id = "Pierre", Name = "Pierre", ClosedMessage = "Store closed." }
            }
        };
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["SeedShop"] = shopData };
        observer.ShopOwnersResolver = _ => new List<ShopOwnerData> { new() { Id = "Pierre", Name = "Pierre", ClosedMessage = "Store closed." } };
        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>();
        // ActionFound = false, OwnerArea = null -> cannot reliably determine standing area
        observer.OpenShopActionResolver = (_, _) => (null, -1, -1, false);
        observer.TimeOfDayResolver = () => 1000;
        observer.ShopNpcsResolver = _ => new[] { new ShopNpcInfo("Pierre", new Point(4, 17)) };

        var scan = observer.ScanShop("SeedShop", actor);

        Assert.Equal("SeedShop", scan.ShopId);
        Assert.Equal("unknown", scan.Status);
        Assert.False(scan.IsOpen);
        Assert.False(scan.OwnerPresent);
        Assert.NotNull(scan.ErrorMessage);
    }

    [Fact]
    public void ScanShop_InanimateShop_WhenUnmannedOwner_ReturnsOpenTrue_AndOwnerPresentTrue()
    {
        var (observer, actor) = CreateHarness();
        var shopData = new ShopData
        {
            Currency = 0,
            Owners = new List<ShopOwnerData>
            {
                new() { Id = "VendingMachine", Name = "None" }
            }
        };
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["Saloon_Vending"] = shopData };
        observer.ShopOwnersResolver = _ => new List<ShopOwnerData> { new() { Id = "VendingMachine", Name = "None" } };
        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>();
        observer.OpenShopActionResolver = (_, _) => (null, -1, -1, false);

        var scan = observer.ScanShop("Saloon_Vending", actor);

        Assert.Equal("Saloon_Vending", scan.ShopId);
        Assert.Equal("ok", scan.Status);
        Assert.True(scan.IsOpen);
        Assert.True(scan.OwnerPresent);
        Assert.Null(scan.ClosedMessage);
    }

    [Fact]
    public void ScanShop_KnownShop_WhenClosed_ReturnsOpenFalse_AndClosedMessage()
    {
        var (observer, actor) = CreateHarness();
        var shopData = new ShopData
        {
            Currency = 0,
            Owners = new List<ShopOwnerData>
            {
                new() { Id = "Pierre", Name = "Pierre", ClosedMessage = "Pierre's store is closed on Wednesdays." }
            }
        };
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["SeedShop"] = shopData };
        observer.ShopOwnersResolver = _ => new List<ShopOwnerData>(); // No active owner = closed
        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>();

        var scan = observer.ScanShop("SeedShop", actor);

        Assert.Equal("SeedShop", scan.ShopId);
        Assert.Equal("ok", scan.Status);
        Assert.False(scan.IsOpen);
        Assert.False(scan.OwnerPresent);
        Assert.Equal("Pierre's store is closed on Wednesdays.", scan.ClosedMessage);
    }

    [Fact]
    public void ScanShop_AvailableMoney_WhenGameFarmerNull_ReturnsNullAndMissing()
    {
        var (observer, actor) = CreateHarness(gameFarmer: null);
        var shopData = new ShopData { Currency = 0 };
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["SeedShop"] = shopData };
        observer.ShopOwnersResolver = _ => new List<ShopOwnerData>();
        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>();

        var scan = observer.ScanShop("SeedShop", actor);

        Assert.Null(scan.AvailableMoney);
        Assert.Equal("missing", scan.MoneyStatus);
    }

    [Fact]
    public void ScanShop_AvailableMoney_WhenCompanionMoneyResolved_ReturnsValueAndOk()
    {
        var farmer = (Farmer)System.Runtime.Serialization.FormatterServices.GetUninitializedObject(typeof(Farmer));
        var (observer, actor) = CreateHarness(farmer);
        var shopData = new ShopData { Currency = 0 };
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["SeedShop"] = shopData };
        observer.ShopOwnersResolver = _ => new List<ShopOwnerData>();
        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>();
        observer.CompanionMoneyResolver = _ => 4500;

        var scan = observer.ScanShop("SeedShop", actor);

        Assert.Equal(4500, scan.AvailableMoney);
        Assert.Equal("ok", scan.MoneyStatus);
    }

    [Fact]
    public void ScanShop_ProjectsItemStockInformation_Correctly()
    {
        var (observer, actor) = CreateHarness();
        var shopData = new ShopData { Currency = 0 };
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["SeedShop"] = shopData };
        observer.ShopOwnersResolver = _ => new List<ShopOwnerData> { new() { Id = "Pierre", Name = "Pierre" } };

        var item1 = new TestSalable { QualifiedItemId = "(O)472", DisplayName = "Parsnip Seeds" };
        var stock1 = new ItemStockInformation(
            price: 20,
            stock: int.MaxValue,
            stockMode: LimitedStockMode.None
        );

        var item2 = new TestSalable { QualifiedItemId = "(O)474", DisplayName = "Cauliflower Seeds" };
        var stock2 = new ItemStockInformation(
            price: 80,
            stock: 5,
            tradeItem: "(O)388",
            tradeItemCount: 2,
            stockMode: LimitedStockMode.Player,
            actionsOnPurchase: new List<string> { "AwardAchievement" }
        );

        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>
        {
            [item1] = stock1,
            [item2] = stock2
        };

        var scan = observer.ScanShop("SeedShop", actor);

        Assert.Equal("ok", scan.Status);
        Assert.Equal(2, scan.Items.Count);

        var parsnip = scan.Items[0];
        Assert.Equal("(O)472", parsnip.ItemId);
        Assert.Equal("Parsnip Seeds", parsnip.Name);
        Assert.Equal(20, parsnip.Price);
        Assert.Equal(int.MaxValue, parsnip.Stock);
        Assert.True(parsnip.IsInfiniteStock);
        Assert.Null(parsnip.TradeItem);
        Assert.Null(parsnip.TradeItemCount);
        Assert.Null(parsnip.LimitedStockMode);

        var cauli = scan.Items[1];
        Assert.Equal("(O)474", cauli.ItemId);
        Assert.Equal("Cauliflower Seeds", cauli.Name);
        Assert.Equal(80, cauli.Price);
        Assert.Equal(5, cauli.Stock);
        Assert.False(cauli.IsInfiniteStock);
        Assert.Equal("(O)388", cauli.TradeItem);
        Assert.Equal(2, cauli.TradeItemCount);
        Assert.Equal("Player", cauli.LimitedStockMode);
        Assert.NotNull(cauli.ActionsOnPurchase);
        Assert.Contains("AwardAchievement", cauli.ActionsOnPurchase);
    }

    [Fact]
    public void ScanShop_KnownShop_WhenCannotDetermineOwnerArea_ReturnsUnknownStatus()
    {
        var (observer, actor) = CreateHarness();
        var shopData = new ShopData
        {
            Currency = 0,
            Owners = new List<ShopOwnerData>
            {
                new() { Id = "Clint", Name = "Clint" }
            }
        };
        // Blacksmith shop has no hardcoded IL mirror for owner area and no resolver provided
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["Blacksmith"] = shopData };
        observer.ShopOwnersResolver = _ => new List<ShopOwnerData> { new() { Id = "Clint", Name = "Clint" } };
        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>();
        // Counter tiles exist, but OwnerArea is null
        observer.ShopActionResolver = (_, _) => new ShopActionMetadata(
            LocationName: "Blacksmith",
            OwnerArea: null,
            OpenTime: 900,
            CloseTime: 1600,
            ActionFound: true,
            CounterTiles: new[] { new TileCoordinate(3, 14) }
        );

        var scan = observer.ScanShop("Blacksmith", actor);

        Assert.Equal("Blacksmith", scan.ShopId);
        Assert.Equal("unknown", scan.Status);
        Assert.False(scan.IsOpen);
        Assert.False(scan.OwnerPresent);
        Assert.Contains("Cannot reliably determine owner standing area", scan.ErrorMessage);
    }

    [Fact]
    public void ScanShop_KnownShop_WhenPierreAtCounterStandingArea_ReturnsOpenTrue()
    {
        var (observer, actor) = CreateHarness();
        var shopData = new ShopData
        {
            Currency = 0,
            Owners = new List<ShopOwnerData>
            {
                new() { Id = "Pierre", Name = "Pierre" }
            }
        };
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["SeedShop"] = shopData };
        observer.ShopOwnersResolver = _ => new List<ShopOwnerData> { new() { Id = "Pierre", Name = "Pierre" } };
        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>();
        // Using SeedShop with owner standing area (4, 17, 1, 1) and Pierre at (4, 17)
        observer.ShopActionResolver = (_, _) => new ShopActionMetadata(
            LocationName: "SeedShop",
            OwnerArea: new Rectangle(4, 17, 1, 1),
            OpenTime: -1,
            CloseTime: -1,
            ActionFound: true,
            CounterTiles: new[] { new TileCoordinate(4, 18), new TileCoordinate(5, 18) }
        );
        observer.ShopNpcsResolver = _ => new[] { new ShopNpcInfo("Pierre", new Point(4, 17)) };

        var scan = observer.ScanShop("SeedShop", actor);

        Assert.Equal("SeedShop", scan.ShopId);
        Assert.Equal("ok", scan.Status);
        Assert.True(scan.IsOpen);
        Assert.True(scan.OwnerPresent);
        Assert.Null(scan.ClosedMessage);
    }

    [Fact]
    public void ScanShop_KnownShop_WhenPierreAtHomeOtherTile_ReturnsOpenFalse()
    {
        var (observer, actor) = CreateHarness();
        actor.SetLocation("SeedShop", new TileCoordinate(4, 19));

        var shopData = new ShopData
        {
            Currency = 0,
            Owners = new List<ShopOwnerData>
            {
                new() { Id = "Pierre", Name = "Pierre", ClosedMessage = "Come back when Pierre's tending the shop." }
            }
        };
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["SeedShop"] = shopData };
        observer.ShopOwnersResolver = _ => new List<ShopOwnerData> { new() { Id = "Pierre", Name = "Pierre", ClosedMessage = "Come back when Pierre's tending the shop." } };
        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>();
        observer.ShopActionResolver = (_, _) => new ShopActionMetadata(
            LocationName: "SeedShop",
            OwnerArea: new Rectangle(4, 17, 1, 1),
            OpenTime: -1,
            CloseTime: -1,
            ActionFound: true,
            CounterTiles: new[] { new TileCoordinate(4, 18) }
        );

        // Pierre is at (4, 16) or (4, 10) in the house - satisfies Y <= actor.TileY - 1 (16 <= 18)
        // BUT is NOT in ownerArea (4, 17, 1, 1). AND logic must reject this and return closed.
        observer.ShopNpcsResolver = _ => new[] { new ShopNpcInfo("Pierre", new Point(4, 16)) };

        var scan = observer.ScanShop("SeedShop", actor);

        Assert.Equal("SeedShop", scan.ShopId);
        Assert.Equal("ok", scan.Status);
        Assert.False(scan.IsOpen);
        Assert.False(scan.OwnerPresent);
        Assert.Equal("Come back when Pierre's tending the shop.", scan.ClosedMessage);
    }

    [Fact]
    public void ScanShop_KnownShop_WhenCurrentOwnersEmpty_ReturnsOpenFalse()
    {
        var (observer, actor) = CreateHarness();
        actor.SetLocation("SeedShop", new TileCoordinate(4, 19));

        var shopData = new ShopData
        {
            Currency = 0,
            Owners = new List<ShopOwnerData>()
        };
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["SeedShop"] = shopData };
        // Empty current owners list from GetCurrentOwners
        observer.ShopOwnersResolver = _ => new List<ShopOwnerData>();
        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>();
        observer.ShopActionResolver = (_, _) => new ShopActionMetadata(
            LocationName: "SeedShop",
            OwnerArea: new Rectangle(4, 17, 1, 1),
            OpenTime: -1,
            CloseTime: -1,
            ActionFound: true,
            CounterTiles: new[] { new TileCoordinate(4, 18) }
        );
        // Pierre is at (4, 17), but GetCurrentOwners is empty (e.g. festival day / invalid schedule)
        observer.ShopNpcsResolver = _ => new[] { new ShopNpcInfo("Pierre", new Point(4, 17)) };

        var scan = observer.ScanShop("SeedShop", actor);

        Assert.Equal("SeedShop", scan.ShopId);
        Assert.Equal("ok", scan.Status);
        Assert.False(scan.IsOpen);
        Assert.False(scan.OwnerPresent);
    }

    [Fact]
    public void ScanShop_ThresholdFormula_MaxOwnerY_BoundaryConditions()
    {
        var (observer, actor) = CreateHarness();

        var shopData = new ShopData
        {
            Currency = 0,
            Owners = new List<ShopOwnerData>
            {
                new() { Id = "Pierre", Name = "Pierre" }
            }
        };
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["SeedShop"] = shopData };
        observer.ShopOwnersResolver = _ => new List<ShopOwnerData> { new() { Id = "Pierre", Name = "Pierre" } };
        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>();
        observer.ShopActionResolver = (_, _) => new ShopActionMetadata(
            LocationName: "SeedShop",
            OwnerArea: new Rectangle(4, 17, 1, 1),
            OpenTime: -1,
            CloseTime: -1,
            ActionFound: true,
            CounterTiles: new[] { new TileCoordinate(4, 18) }
        );
        // Pierre is exactly at (4, 17)
        observer.ShopNpcsResolver = _ => new[] { new ShopNpcInfo("Pierre", new Point(4, 17)) };

        // Subcase A: actor.Tile.Y - 1 == pierre.Y (18 - 1 == 17) -> open
        actor.SetLocation("SeedShop", new TileCoordinate(4, 18));
        var scanA = observer.ScanShop("SeedShop", actor);
        Assert.True(scanA.IsOpen);
        Assert.True(scanA.OwnerPresent);

        // Subcase B: actor.Tile.Y - 1 < pierre.Y (17 - 1 == 16 < 17) -> closed
        actor.SetLocation("SeedShop", new TileCoordinate(4, 17));
        var scanB = observer.ScanShop("SeedShop", actor);
        Assert.False(scanB.IsOpen);
        Assert.False(scanB.OwnerPresent);
    }

    [Fact]
    public void ScanShop_SeedShop_WithClosedFallbackAndPierreOutsideCounter_ReturnsOpenFalse()
    {
        var (observer, actor) = CreateHarness();
        actor.SetLocation("SeedShop", new TileCoordinate(4, 19));

        // Real Data/Shops SeedShop owners: Closed (Type: None with ClosedMessage) and Pierre (Type: NamedNpc)
        var closedOwner = new ShopOwnerData { Id = "Closed", ClosedMessage = "Come back when Pierre's tending the shop." };
        typeof(ShopOwnerData).GetField("<Type>k__BackingField", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)?.SetValue(closedOwner, ShopOwnerType.None);
        var pierreOwner = new ShopOwnerData { Id = "Pierre", Name = "Pierre" };

        var shopData = new ShopData
        {
            Currency = 0,
            Owners = new List<ShopOwnerData> { closedOwner, pierreOwner }
        };
        observer.ShopDataResolver = _ => new Dictionary<string, ShopData> { ["SeedShop"] = shopData };
        observer.ShopOwnersResolver = _ => shopData.Owners;
        observer.ShopStockResolver = (_, _) => new Dictionary<ISalable, ItemStockInformation>();
        observer.ShopActionResolver = (_, _) => new ShopActionMetadata(
            LocationName: "SeedShop",
            OwnerArea: new Rectangle(4, 17, 1, 1),
            OpenTime: -1,
            CloseTime: -1,
            ActionFound: true,
            CounterTiles: new[] { new TileCoordinate(4, 18) }
        );

        // Pierre is at (10, 20) outside counter standing area
        observer.ShopNpcsResolver = _ => new[] { new ShopNpcInfo("Pierre", new Point(10, 20)) };

        var scan = observer.ScanShop("SeedShop", actor);

        // Closed entry with Type=None must NOT be mistaken for an open unmanned shop
        Assert.Equal("SeedShop", scan.ShopId);
        Assert.Equal("ok", scan.Status);
        Assert.False(scan.IsOpen);
        Assert.False(scan.OwnerPresent);
        Assert.Equal("Come back when Pierre's tending the shop.", scan.ClosedMessage);
    }
}
