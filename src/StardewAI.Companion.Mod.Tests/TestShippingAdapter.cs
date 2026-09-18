using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>
/// In-memory test double for IShippingAdapter.
/// Simulates shipping bin footprint, shippability checks, valuation, and item extraction.
/// </summary>
public sealed class TestShippingAdapter : IShippingAdapter
{
    private readonly SimulatedWorldObserver _observer;
    private readonly List<ShippedItemInfo> _shippedHistory = new();

    public List<TileCoordinate> Footprint { get; set; } = new()
    {
        new TileCoordinate(71, 14),
        new TileCoordinate(72, 14)
    };

    public HashSet<string> UnshippableItemIds { get; } = new(StringComparer.OrdinalIgnoreCase);
    public int FixedUnitPrice { get; set; } = 35;
    public int TotalShippedStack => _shippedHistory.Sum(s => s.Count);
    public IReadOnlyList<ShippedItemInfo> ShippedHistory => _shippedHistory;

    public TestShippingAdapter(SimulatedWorldObserver observer)
    {
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
    }

    public IReadOnlyList<TileCoordinate> GetShippingBinFootprint(string locationName)
    {
        if (!string.Equals(locationName, "Farm", StringComparison.OrdinalIgnoreCase))
            return Array.Empty<TileCoordinate>();
        return Footprint;
    }

    public bool CheckItemShippable(IFarmerActor actor, string itemId, out string? reason)
    {
        ArgumentNullException.ThrowIfNull(actor);

        if (UnshippableItemIds.Contains(itemId) || itemId.StartsWith("(T)", StringComparison.OrdinalIgnoreCase))
        {
            reason = $"Item '{itemId}' is not shippable.";
            return false;
        }

        int count = actor.GetItemCount(itemId);
        if (count <= 0)
        {
            reason = $"Item '{itemId}' not found in companion inventory.";
            return false;
        }

        reason = null;
        return true;
    }

    public int GetShippingBinTotalCount(IFarmerActor actor)
    {
        return TotalShippedStack;
    }

    public ShippingItemResult ShipItem(
        IFarmerActor actor,
        string locationName,
        string itemId,
        int count)
    {
        ArgumentNullException.ThrowIfNull(actor);

        if (!_observer.IsMainThread)
        {
            return ShippingItemResult.Failed("Shipping rejected: must execute on main thread.");
        }

        if (!string.Equals(locationName, "Farm", StringComparison.OrdinalIgnoreCase))
        {
            return ShippingItemResult.PreconditionError($"Shipping only allowed on Farm, got '{locationName}'.", skipReason: "invalid-location");
        }

        if (!string.Equals(_observer.CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
        {
            return ShippingItemResult.Failed($"Location mismatch: current '{_observer.CurrentLocationName}', target '{locationName}'.");
        }

        if (!Footprint.Any(t => actor.Tile.IsAdjacentTo(t)))
        {
            return ShippingItemResult.PreconditionError($"Actor at {actor.Tile} is not adjacent to shipping bin.", skipReason: "not-adjacent");
        }

        if (!CheckItemShippable(actor, itemId, out var shippableReason))
        {
            return ShippingItemResult.PreconditionError(shippableReason ?? "Item not shippable.", skipReason: "not-shippable");
        }

        int countBefore = actor.GetItemCount(itemId);
        if (countBefore < count)
        {
            return ShippingItemResult.PreconditionError($"Insufficient count for '{itemId}': has {countBefore}, requested {count}.", skipReason: "insufficient-items");
        }

        bool extracted = actor.TryExtractItem(itemId, count, out _);
        if (!extracted)
        {
            return ShippingItemResult.Failed($"Failed to extract '{itemId}' from actor.");
        }

        int countAfter = actor.GetItemCount(itemId);
        int estTotal = FixedUnitPrice * count;
        var shippedInfo = new ShippedItemInfo(itemId, count, FixedUnitPrice, estTotal, countAfter);
        _shippedHistory.Add(shippedInfo);

        return ShippingItemResult.Succeeded(shippedInfo, TotalShippedStack);
    }
}
