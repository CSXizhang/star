using StardewModdingAPI;
using StardewValley;
using StardewValley.Internal;
using StardewValley.GameData.Shops;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Production purchase adapter executing shop purchasing through normal game mechanics.
/// Enforces:
/// 1. Dynamic counter discovery from indoor map Buy tile actions without hardcoded coordinates.
/// 2. Shop operating threshold re-evaluated at the exact moment of purchase.
/// 3. Dynamic stock & price lookup via ShopBuilder.GetShopStock.
/// 4. Limited stock items strictly rejected (skipReason: "limited-stock-unsupported").
/// 5. Single-transaction explicit budget limit enforcement.
/// 6. Game native team wallet rules via team.GetMoney(f).Value and team.AddIndividualMoney(f, -totalCost).
/// 7. New Item instance created from salable key ISalable.GetSalableInstance(), stack set, and
///    delivered via FarmerMechanicsActor.TryAddItemToInventory.
/// 8. Two-sided balance and backpack verification with rollback on mismatch.
/// </summary>
public sealed class NormalPurchaseAdapter : IPurchaseAdapter
{
    private readonly IWorldObserver _observer;
    private readonly IMonitor? _monitor;

    public Func<Farmer, int>? MoneyGetter { get; set; }
    public Action<Farmer, int>? MoneyAdder { get; set; }
    public Func<string, IFarmerActor, ShopScanInfo>? ShopScanOverride { get; set; }
    public Func<string, string, IReadOnlyList<TileCoordinate>>? CounterTilesResolver { get; set; }
    public Func<string, IFarmerActor, IReadOnlyDictionary<ISalable, ItemStockInformation>?>? ShopStockResolver { get; set; }

    public NormalPurchaseAdapter(IWorldObserver observer, IMonitor? monitor = null)
    {
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _monitor = monitor;
    }

    private void Log(string message, LogLevel level = LogLevel.Info) => _monitor?.Log(message, level);

    public IReadOnlyList<TileCoordinate> GetShopCounterTiles(string shopId, string locationName)
    {
        if (CounterTilesResolver != null)
        {
            return CounterTilesResolver(shopId, locationName);
        }

        return _observer.GetShopCounterTiles(shopId, locationName);
    }

    public ShopScanInfo ReevaluateShop(string shopId, IFarmerActor actor)
    {
        if (ShopScanOverride != null)
        {
            return ShopScanOverride(shopId, actor);
        }
        return _observer.ScanShop(shopId, actor);
    }

    public int? GetCompanionMoney(IFarmerActor actor)
    {
        if (actor?.GameFarmer != null)
        {
            if (MoneyGetter != null)
            {
                return MoneyGetter(actor.GameFarmer);
            }

            try
            {
                if (Game1.player?.team != null)
                {
                    return Game1.player.team.GetMoney(actor.GameFarmer).Value;
                }
            }
            catch (Exception ex)
            {
                Log($"Error getting companion money: {ex.Message}", LogLevel.Warn);
            }
        }
        return null;
    }

    public bool TryDeductCompanionMoney(IFarmerActor actor, int amount, out int balanceAfter)
    {
        balanceAfter = 0;
        if (actor?.GameFarmer != null)
        {
            if (MoneyAdder != null)
            {
                MoneyAdder(actor.GameFarmer, -amount);
                balanceAfter = MoneyGetter != null
                    ? MoneyGetter(actor.GameFarmer)
                    : (Game1.player?.team?.GetMoney(actor.GameFarmer).Value ?? 0);
                return true;
            }

            try
            {
                if (Game1.player?.team != null)
                {
                    Game1.player.team.AddIndividualMoney(actor.GameFarmer, -amount);
                    balanceAfter = Game1.player.team.GetMoney(actor.GameFarmer).Value;
                    return true;
                }
            }
            catch (Exception ex)
            {
                Log($"Error deducting companion money: {ex.Message}", LogLevel.Error);
                return false;
            }
        }
        return false;
    }

    public void RefundCompanionMoney(IFarmerActor actor, int amount)
    {
        if (actor?.GameFarmer != null)
        {
            if (MoneyAdder != null)
            {
                MoneyAdder(actor.GameFarmer, amount);
                return;
            }

            try
            {
                if (Game1.player?.team != null)
                {
                    Game1.player.team.AddIndividualMoney(actor.GameFarmer, amount);
                }
            }
            catch (Exception ex)
            {
                Log($"Error refunding companion money: {ex.Message}", LogLevel.Error);
            }
        }
    }

    public IReadOnlyDictionary<ISalable, ItemStockInformation>? GetRawShopStock(string shopId, IFarmerActor actor)
    {
        if (ShopStockResolver != null)
        {
            return ShopStockResolver(shopId, actor);
        }

        try
        {
            var shops = DataLoader.Shops(Game1.content);
            if (shops != null && shops.TryGetValue(shopId, out var shopData) && shopData != null)
            {
                return ShopBuilder.GetShopStock(shopId, shopData);
            }
        }
        catch (Exception ex)
        {
            Log($"Error reading raw shop stock for '{shopId}': {ex.Message}", LogLevel.Warn);
        }

        return null;
    }

    public int CountItemInBackpack(IFarmerActor actor, string itemId)
    {
        return actor.GetItemCount(itemId);
    }
}
