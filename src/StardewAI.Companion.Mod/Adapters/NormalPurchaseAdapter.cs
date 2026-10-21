using StardewModdingAPI;
using StardewValley;
using StardewValley.Internal;
using StardewValley.GameData.Shops;
using StardewValley.Menus;
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
/// 9. Player-bound actors instead buy through the real ShopMenu natively
///    (TryOpenShopMenu -> tryToPurchaseItem -> heldItem delivered to the player,
///    menu closed on every path); finite stock is supported via the game's own
///    HandleSynchedItemPurchase bookkeeping.
/// </summary>
public sealed class NormalPurchaseAdapter : IPurchaseAdapter
{
    private static readonly System.Reflection.MethodInfo? TryToPurchaseItemMethod =
        typeof(ShopMenu).GetMethod("tryToPurchaseItem", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);

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
        if (ActorGuards.IsPlayerBound(actor)) {
            StardewValley.Menus.ShopMenu? menu=null;
            try {
                if (!StardewAI.Companion.Mod.PlayerMode.NativeShopInteraction.TryOpen(_observer,actor,shopId,out menu,out _)) return null;
                _observer.ScanShop(shopId,actor); // retain this actually observed quotation
                return menu!.itemPriceAndStock.ToDictionary(p=>p.Key,p=>p.Value);
            } finally {
                if (menu != null && ReferenceEquals(Game1.activeClickableMenu,menu)) menu.exitThisMenu();
            }
        }
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

    public bool TryPurchaseNative(IFarmerActor actor, string shopId, string itemId, int count, out string error)
    {
        error = string.Empty;
        if (!ActorGuards.IsPlayerBound(actor))
        {
            error = "Native purchase requires a player-bound actor.";
            return false;
        }
        if (string.IsNullOrWhiteSpace(shopId))
        {
            error = "shopId cannot be empty.";
            return false;
        }
        if (string.IsNullOrWhiteSpace(itemId))
        {
            error = "itemId cannot be empty.";
            return false;
        }
        if (count <= 0)
        {
            error = "count must be positive.";
            return false;
        }
        if (TryToPurchaseItemMethod is null)
        {
            error = "ShopMenu.tryToPurchaseItem is unavailable in this game version.";
            return false;
        }

        var player = actor.GameFarmer!;
        ShopMenu? menu = null;
        int moneyBefore = player.Money;
        try
        {
            // Open the real shop menu natively (sets Game1.activeClickableMenu).
            if (!StardewAI.Companion.Mod.PlayerMode.NativeShopInteraction.TryOpen(_observer,actor,shopId,out menu,out error))
            {
                error = $"Shop '{shopId}' is closed or unavailable.";
                return false;
            }
            if (Game1.activeClickableMenu is not ShopMenu opened)
            {
                error = $"Shop '{shopId}' did not open a shop menu.";
                return false;
            }
            menu = opened;

            ISalable? salable = null;
            foreach (var candidate in menu.forSale)
            {
                if (candidate != null && SalableIdMatches(candidate, itemId))
                {
                    salable = candidate;
                    break;
                }
            }
            if (salable is null)
            {
                error = $"Item '{itemId}' is not sold by shop '{shopId}'.";
                return false;
            }
            if (!menu.itemPriceAndStock.TryGetValue(salable, out var stockInfo))
            {
                error = $"Shop '{shopId}' has no price/stock entry for '{itemId}'.";
                return false;
            }
            if (!salable.CanBuyItem(player))
            {
                error = $"Item '{itemId}' cannot be bought by the player right now.";
                return false;
            }
            bool infinite = stockInfo.Stock == int.MaxValue || salable.IsInfiniteStock();
            if (!infinite && stockInfo.Stock < count)
            {
                error = $"Shop '{shopId}' has only {stockInfo.Stock} of '{itemId}' in stock (requested {count}).";
                return false;
            }

            // One native call buys at most one maximum-size stack.
            int maxStack = Math.Max(1, salable.GetSalableInstance().maximumStackSize());
            if (count > maxStack)
            {
                error = $"Requested count {count} exceeds the maximum stack size for one native purchase ({maxStack}).";
                return false;
            }
            int unitCount = Math.Max(1, salable.Stack);
            int totalCost = stockInfo.Price * count;
            if (player.Money < totalCost)
            {
                error = $"Insufficient funds: '{itemId}' x{count} costs {totalCost}g, player has {player.Money}g.";
                return false;
            }

            // Precise inventory-space precheck with the exact instance shape the menu
            // would deliver, so the post-purchase addItemToInventory cannot overflow.
            if (salable.GetSalableInstance() is not Item probe)
            {
                error = $"Item '{itemId}' cannot be delivered to the player inventory (non-item salable).";
                return false;
            }
            probe.Stack = count * unitCount;
            if (!player.couldInventoryAcceptThisItem(probe))
            {
                error = $"Player inventory cannot accept '{itemId}' x{probe.Stack}.";
                return false;
            }

            int centerX = menu.xPositionOnScreen + menu.width / 2;
            int centerY = menu.yPositionOnScreen + menu.height / 2;

            // Native purchase. The bool return is NOT a success signal (the IL returns
            // false even on a normal purchase while stock remains); success is
            // delivered through menu.heldItem plus the native chargePlayer deduction.
            _ = TryToPurchaseItemMethod.Invoke(menu, new object?[] { salable, null, count, centerX, centerY });

            var held = menu.heldItem;

            int charged = moneyBefore - player.Money;
            if (held is null)
            {
                error = charged > 0
                    ? $"Purchase of '{itemId}' charged {charged}g without delivering an item."
                    : $"Purchase of '{itemId}' did not complete (native purchase rejected).";
                return false;
            }
            menu.heldItem = player.addItemToInventory((Item)held);
            if (menu.heldItem is not null)
            {
                error = "Native purchase left an item on the shop cursor; the menu remains open for human recovery.";
                return false;
            }
            if (charged != totalCost)
            {
                error = $"Purchase delivered '{itemId}' but charged {charged}g, expected {totalCost}g.";
                return false;
            }

            return true;
        }
        catch (Exception ex)
        {
            error = $"Native purchase failed: {ex.Message}";
            Log($"TryPurchaseNative({shopId}, {itemId} x{count}) failed: {ex}", LogLevel.Error);
            return false;
        }
        finally
        {
            if (menu is not null && menu.heldItem is null && ReferenceEquals(Game1.activeClickableMenu, menu))
            {
                menu.exitThisMenu(false);
            }
        }
    }

    private static bool SalableIdMatches(ISalable salable, string itemId)
    {
        string qid = salable.QualifiedItemId;
        if (string.Equals(qid, itemId, StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }

        string rawId = salable is Item item
            ? item.ItemId
            : (qid.StartsWith("(", StringComparison.Ordinal) && qid.IndexOf(')') > 0
                ? qid.Substring(qid.IndexOf(')') + 1)
                : qid);
        if (string.Equals(rawId, itemId, StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }

        string normalized = SeedIdNormalizer.ToCropDataId(itemId);
        return !string.Equals(normalized, itemId, StringComparison.Ordinal) &&
               string.Equals(rawId, normalized, StringComparison.OrdinalIgnoreCase);
    }
}
