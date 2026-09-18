using StardewModdingAPI;
using StardewValley;
using StardewValley.Buildings;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Production shipping adapter executing shipping batch through normal game mechanics.
/// Enforces:
/// 1. Main-thread and Farm map execution only.
/// 2. Shipping bin footprint discovered dynamically from Farm.buildings (no hardcoded coordinates).
/// 3. Adjacency check to shipping bin footprint.
/// 4. Tools and non-shippable items strictly rejected.
/// 5. Real item instances extracted from companion inventory (TryExtractItem) and added
///    directly to Farm.getShippingBin(companionFarmer).
/// 6. Both-side delta verification: backpack decreased, shipping bin increased by exact count.
/// 7. Valuation calculated via Item.sellToStorePrice(companionId) and labeled as estimated.
/// 8. Human player isolation: Game1.player reference, stamina, and Money strictly preserved
///    (shipping revenue is settled overnight by game engine, never credited immediately).
/// </summary>
public sealed class NormalShippingAdapter : IShippingAdapter
{
    private readonly IWorldObserver _observer;
    private readonly IMonitor _monitor;

    public NormalShippingAdapter(
        IWorldObserver observer,
        IMonitor monitor)
    {
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _monitor = monitor ?? throw new ArgumentNullException(nameof(monitor));
    }

    public IReadOnlyList<TileCoordinate> GetShippingBinFootprint(string locationName)
    {
        if (!string.Equals(locationName, "Farm", StringComparison.OrdinalIgnoreCase))
        {
            return Array.Empty<TileCoordinate>();
        }

        var farm = Game1.getFarm();
        if (farm == null)
        {
            return Array.Empty<TileCoordinate>();
        }

        // 1. Check farm.buildings for ShippingBin
        var bin = farm.buildings.FirstOrDefault(b => b is StardewValley.Buildings.ShippingBin ||
            string.Equals(b.buildingType.Value, "Shipping Bin", StringComparison.OrdinalIgnoreCase));

        if (bin != null)
        {
            var tiles = new List<TileCoordinate>();
            int startX = bin.tileX.Value;
            int startY = bin.tileY.Value;
            int width = bin.tilesWide.Value > 0 ? bin.tilesWide.Value : 2;
            int height = bin.tilesHigh.Value > 0 ? bin.tilesHigh.Value : 1;

            for (int x = startX; x < startX + width; x++)
            {
                for (int y = startY; y < startY + height; y++)
                {
                    tiles.Add(new TileCoordinate(x, y));
                }
            }
            return tiles;
        }

        // 2. Fallback: starter farm map shipping bin position (2x1 footprint)
        if (farm.mapShippingBinPosition.HasValue)
        {
            var pos = farm.mapShippingBinPosition.Value;
            return new[]
            {
                new TileCoordinate((int)pos.X, (int)pos.Y),
                new TileCoordinate((int)pos.X + 1, (int)pos.Y)
            };
        }

        return Array.Empty<TileCoordinate>();
    }

    public bool CheckItemShippable(IFarmerActor actor, string itemId, out string? reason)
    {
        ArgumentNullException.ThrowIfNull(actor);

        if (actor.GameFarmer != null)
        {
            var item = actor.GameFarmer.Items.FirstOrDefault(i => i != null && FarmerMechanicsActor.ItemMatchesId(i, itemId));
            if (item == null)
            {
                reason = $"Item '{itemId}' not found in companion inventory.";
                return false;
            }

            if (item is StardewValley.Tool)
            {
                reason = $"Item '{item.Name}' is a tool and cannot be shipped.";
                return false;
            }

            if (!item.canBeShipped())
            {
                reason = $"Item '{item.Name}' cannot be shipped (not accepted in shipping bin).";
                return false;
            }
        }
        else
        {
            int count = actor.GetItemCount(itemId);
            if (count <= 0)
            {
                reason = $"Item '{itemId}' not found in companion inventory.";
                return false;
            }
        }

        reason = null;
        return true;
    }

    public int GetShippingBinTotalCount(IFarmerActor actor)
    {
        ArgumentNullException.ThrowIfNull(actor);
        var farm = Game1.getFarm();
        if (farm == null) return 0;
        var bin = farm.getShippingBin(actor.GameFarmer);
        if (bin == null) return 0;
        return bin.Sum(i => i?.Stack ?? 0);
    }

    public ShippingItemResult ShipItem(
        IFarmerActor actor,
        string locationName,
        string itemId,
        int count)
    {
        ArgumentNullException.ThrowIfNull(actor);
        if (string.IsNullOrWhiteSpace(itemId))
        {
            return ShippingItemResult.Failed("itemId cannot be null or empty.");
        }
        if (count < 1)
        {
            return ShippingItemResult.Failed("count must be at least 1.");
        }

        // 1. Thread safety invariant
        if (!_observer.IsMainThread)
        {
            return ShippingItemResult.Failed("Shipping operation rejected: must execute on game main thread.");
        }

        // 2. Map invariant: strictly Farm map
        if (!string.Equals(locationName, "Farm", StringComparison.OrdinalIgnoreCase))
        {
            return ShippingItemResult.PreconditionError(
                $"Shipping rejected: destination map must be 'Farm', got '{locationName}'.", skipReason: "invalid-location");
        }

        if (!string.Equals(_observer.CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
        {
            return ShippingItemResult.Failed(
                $"Shipping target map '{locationName}' does not match active map '{_observer.CurrentLocationName}'.");
        }

        // 3. Companion check
        if (actor.GameFarmer == null)
        {
            return ShippingItemResult.Failed("Companion actor has no GameFarmer instance.");
        }

        // 4. Shipping bin existence and adjacency check
        var footprint = GetShippingBinFootprint(locationName);
        if (footprint.Count == 0)
        {
            return ShippingItemResult.PreconditionError("No shipping bin found on farm.", skipReason: "shipping-bin-not-found");
        }

        bool isAdjacent = footprint.Any(t => actor.Tile.IsAdjacentTo(t));
        if (!isAdjacent)
        {
            return ShippingItemResult.PreconditionError(
                $"Actor at {actor.Tile} is not adjacent to shipping bin footprint.", skipReason: "not-adjacent");
        }

        // 5. Item shippability precheck
        if (!CheckItemShippable(actor, itemId, out var shippableReason))
        {
            return ShippingItemResult.PreconditionError(shippableReason ?? "Item cannot be shipped.", skipReason: "not-shippable");
        }

        int backpackCountBefore = actor.GetItemCount(itemId);
        if (backpackCountBefore < count)
        {
            return ShippingItemResult.PreconditionError(
                $"Companion has {backpackCountBefore} of '{itemId}', but {count} was requested.", skipReason: "insufficient-items");
        }

        // 6. Host baseline isolation BEFORE the transfer
        var playerBefore = _observer.GetPlayerSnapshot();
        var originalPlayerRef = Game1.player;
        int hostMoneyBefore = Game1.player?.Money ?? 0;

        var farm = Game1.getFarm();
        var shippingBin = farm.getShippingBin(actor.GameFarmer);
        int binStackBefore = shippingBin.Sum(i => i?.Stack ?? 0);

        try
        {
            // 7. Genuine item extraction from companion backpack
            if (!actor.TryExtractItem(itemId, count, out var extractedItems) || extractedItems.Count == 0)
            {
                return ShippingItemResult.Failed($"Failed to extract '{itemId}' from companion backpack.");
            }

            int extractedTotalStack = extractedItems.Sum(i => i.Stack);
            if (extractedTotalStack != count)
            {
                throw new InvalidOperationException(
                    $"Extracted item stack mismatch: expected {count}, got {extractedTotalStack}.");
            }

            // 8. Valuation and genuine placement into shipping bin
            int estimatedTotalValue = 0;
            int representativeUnitPrice = 0;
            long companionPlayerId = actor.GameFarmer.UniqueMultiplayerID;

            for (int i = 0; i < extractedItems.Count; i++)
            {
                var extracted = extractedItems[i];
                int unitPrice = extracted.sellToStorePrice(companionPlayerId);
                if (i == 0) representativeUnitPrice = unitPrice;
                estimatedTotalValue += unitPrice * extracted.Stack;
                shippingBin.Add(extracted);
            }

            // 9. Verify both-side deltas
            int backpackCountAfter = actor.GetItemCount(itemId);
            if (backpackCountBefore - backpackCountAfter != count)
            {
                throw new InvalidOperationException(
                    $"Companion backpack delta verification failed: before={backpackCountBefore}, after={backpackCountAfter}, expected reduction={count}.");
            }

            int binStackAfter = shippingBin.Sum(i => i?.Stack ?? 0);
            if (binStackAfter != binStackBefore + count)
            {
                throw new InvalidOperationException(
                    $"Shipping bin delta verification failed: before={binStackBefore}, after={binStackAfter}, expected increase={count}.");
            }

            var shippedInfo = new ShippedItemInfo(
                ItemId: itemId,
                Count: count,
                EstimatedUnitValue: representativeUnitPrice,
                EstimatedTotalValue: estimatedTotalValue,
                RemainingBackpackCount: backpackCountAfter
            );

            return ShippingItemResult.Succeeded(shippedInfo, binStackAfter);
        }
        finally
        {
            // 10. Human player resource and reference isolation check
            if (!object.ReferenceEquals(originalPlayerRef, Game1.player))
            {
                throw new InvalidOperationException("CRITICAL SAFETY VIOLATION: Game1.player reference was modified during shipping!");
            }

            var playerAfter = _observer.GetPlayerSnapshot();
            if (!playerBefore.EqualsPlayerResources(playerAfter))
            {
                throw new InvalidOperationException(
                    $"CRITICAL SAFETY VIOLATION: Human player resources changed during companion shipping! Before: {playerBefore}, After: {playerAfter}");
            }

            if (Game1.player != null && Game1.player.Money != hostMoneyBefore)
            {
                throw new InvalidOperationException(
                    $"CRITICAL SAFETY VIOLATION: Human player money changed during companion shipping! Before: {hostMoneyBefore}, After: {Game1.player.Money}. Shipping funds must only settle overnight.");
            }
        }
    }
}
