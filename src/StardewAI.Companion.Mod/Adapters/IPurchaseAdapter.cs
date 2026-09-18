using StardewValley;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Interface decoupling purchase skill execution from direct Stardew Valley game state,
/// supporting both live production gameplay and deterministic mock testing.
/// </summary>
public interface IPurchaseAdapter
{
    /// <summary>
    /// Discovers all counter interaction tiles configured with action verb 'Buy' (e.g. 'Action Buy General')
    /// in the target indoor location.
    /// </summary>
    IReadOnlyList<TileCoordinate> GetShopCounterTiles(string shopId, string locationName);

    /// <summary>
    /// Re-evaluates shop operating status (hours, owner attendance, counter presence)
    /// at the exact purchase instant without using stale cached results.
    /// </summary>
    ShopScanInfo ReevaluateShop(string shopId, IFarmerActor actor);

    /// <summary>
    /// Returns the companion's current authoritative money balance via team wallet rules.
    /// </summary>
    int? GetCompanionMoney(IFarmerActor actor);

    /// <summary>
    /// Deducts authorized purchase funds using game native wallet mechanics (AddIndividualMoney).
    /// </summary>
    bool TryDeductCompanionMoney(IFarmerActor actor, int amount, out int balanceAfter);

    /// <summary>
    /// Refunds deducted money during a rollback scenario.
    /// </summary>
    void RefundCompanionMoney(IFarmerActor actor, int amount);

    /// <summary>
    /// Retrieves active shop stock dictionary mapping ISalable to ItemStockInformation.
    /// </summary>
    IReadOnlyDictionary<ISalable, ItemStockInformation>? GetRawShopStock(string shopId, IFarmerActor actor);

    /// <summary>
    /// Counts total units of the specified qualified or unqualified item in companion inventory.
    /// </summary>
    int CountItemInBackpack(IFarmerActor actor, string itemId);
}
