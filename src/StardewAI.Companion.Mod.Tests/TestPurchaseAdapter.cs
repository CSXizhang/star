using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.GameData.Shops;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Tests;

public sealed class TestPurchaseSalable : ISalable
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
    public void drawInMenu(Microsoft.Xna.Framework.Graphics.SpriteBatch spriteBatch, Vector2 location, float scaleSize, float transparency, float layerDepth, StackDrawType drawStackNumber, Color color, bool drawShadow) { }
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

public sealed class TestPurchaseAdapter : IPurchaseAdapter
{
    public List<TileCoordinate> CounterTiles { get; set; } = new()
    {
        new TileCoordinate(10, 11)
    };

    public ShopScanInfo? CustomShopScan { get; set; }
    public Dictionary<ISalable, ItemStockInformation>? Stock { get; set; }
    public int AvailableMoney { get; set; } = 1000;
    public bool FailDeductMoney { get; set; } = false;
    public int TotalDeducted { get; private set; } = 0;
    public int TotalRefunded { get; private set; } = 0;

    public IReadOnlyList<TileCoordinate> GetShopCounterTiles(string shopId, string locationName)
    {
        return CounterTiles;
    }

    public ShopScanInfo ReevaluateShop(string shopId, IFarmerActor actor)
    {
        if (CustomShopScan != null)
        {
            return CustomShopScan;
        }

        return new ShopScanInfo(
            ShopId: shopId,
            Status: "ok",
            IsOpen: true,
            OwnerPresent: true,
            ClosedMessage: null,
            Owners: new[] { "Pierre" },
            Currency: 0,
            AvailableMoney: AvailableMoney,
            MoneyStatus: "ok",
            Items: Array.Empty<ShopItemInfo>()
        );
    }

    public int? GetCompanionMoney(IFarmerActor actor)
    {
        return AvailableMoney;
    }

    public bool TryDeductCompanionMoney(IFarmerActor actor, int amount, out int balanceAfter)
    {
        balanceAfter = AvailableMoney;
        if (FailDeductMoney)
            return false;

        AvailableMoney -= amount;
        TotalDeducted += amount;
        balanceAfter = AvailableMoney;
        return true;
    }

    public void RefundCompanionMoney(IFarmerActor actor, int amount)
    {
        AvailableMoney += amount;
        TotalRefunded += amount;
    }

    public IReadOnlyDictionary<ISalable, ItemStockInformation>? GetRawShopStock(string shopId, IFarmerActor actor)
    {
        return Stock;
    }

    public int CountItemInBackpack(IFarmerActor actor, string itemId)
    {
        return actor.GetItemCount(itemId);
    }
}
