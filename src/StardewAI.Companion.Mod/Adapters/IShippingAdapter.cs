using StardewValley;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Adapters;

public sealed class ShippingItemResult
{
    public bool Success { get; }
    public string? ErrorMessage { get; }
    public bool PreconditionFailed { get; }
    public string? SkipReason { get; }
    public ShippedItemInfo? ShippedItem { get; }
    public int ShippingBinTotalCount { get; }

    private ShippingItemResult(
        bool success,
        string? errorMessage,
        bool preconditionFailed,
        string? skipReason,
        ShippedItemInfo? shippedItem,
        int shippingBinTotalCount)
    {
        Success = success;
        ErrorMessage = errorMessage;
        PreconditionFailed = preconditionFailed;
        SkipReason = skipReason;
        ShippedItem = shippedItem;
        ShippingBinTotalCount = shippingBinTotalCount;
    }

    public static ShippingItemResult Succeeded(ShippedItemInfo shippedItem, int shippingBinTotalCount) =>
        new(true, null, false, null, shippedItem, shippingBinTotalCount);

    public static ShippingItemResult PreconditionError(string reason, string? skipReason = null) =>
        new(false, reason, true, skipReason, null, 0);

    public static ShippingItemResult Failed(string reason) =>
        new(false, reason, false, null, null, 0);
}

public interface IShippingAdapter
{
    IReadOnlyList<TileCoordinate> GetShippingBinFootprint(string locationName);
    bool CheckItemShippable(IFarmerActor actor, string itemId, out string? reason);
    int GetShippingBinTotalCount(IFarmerActor actor);
    ShippingItemResult ShipItem(
        IFarmerActor actor,
        string locationName,
        string itemId,
        int count);
}
