using Microsoft.Xna.Framework;
using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Observation;

public sealed record ShopNpcInfo(string Name, Point TilePoint);

public sealed record ShopItemInfo(
    string ItemId,
    string Name,
    int Price,
    int Stock,
    bool IsInfiniteStock,
    string? TradeItem = null,
    int? TradeItemCount = null,
    string? LimitedStockMode = null,
    IReadOnlyList<string>? ActionsOnPurchase = null,
    int? Category = null,
    bool IsSeed = false
);

public sealed record ShopScanInfo(
    string ShopId,
    string Status,
    bool IsOpen,
    bool OwnerPresent,
    string? ClosedMessage,
    IReadOnlyList<string> Owners,
    int Currency,
    int? AvailableMoney,
    string MoneyStatus,
    IReadOnlyList<ShopItemInfo> Items,
    string? ErrorMessage = null,
    string? LocationId = null,
    TileCoordinate? InteractionTile = null
);
