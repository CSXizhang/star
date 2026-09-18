using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>
/// In-memory test double for IChestAdapter.
/// </summary>
public sealed class TestChestAdapter : IChestAdapter
{
    private readonly SimulatedWorldObserver _observer;
    public Dictionary<string, int> StoredItems { get; } = new(StringComparer.OrdinalIgnoreCase);
    public bool SimulateBackpackFull { get; set; } = false;

    public TestChestAdapter(SimulatedWorldObserver observer)
    {
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
    }

    public ChestDepositResult DepositItem(
        IFarmerActor actor,
        string locationName,
        TileCoordinate chestTile,
        int inventorySlotIndex)
    {
        return ChestDepositResult.Succeeded(1, false, "(O)24", "Parsnip", 0);
    }

    public ChestOrganizeResult OrganizeChest(
        IFarmerActor actor,
        string locationName,
        TileCoordinate chestTile)
    {
        return ChestOrganizeResult.Succeeded(Array.Empty<ChestMergeInfo>());
    }

    public ChestWithdrawResult WithdrawItem(
        IFarmerActor actor,
        string locationName,
        TileCoordinate chestTile,
        string itemId,
        int count)
    {
        ArgumentNullException.ThrowIfNull(actor);

        if (SimulateBackpackFull || actor.FreeInventorySlots <= 0)
        {
            return ChestWithdrawResult.PreconditionError("Backpack full", skipReason: "backpack-full");
        }

        if (!StoredItems.TryGetValue(itemId, out int available) || available <= 0)
        {
            return ChestWithdrawResult.PreconditionError($"Item {itemId} not in chest", skipReason: "item-not-found");
        }

        int toTake = Math.Min(count, available);
        StoredItems[itemId] -= toTake;
        if (StoredItems[itemId] <= 0)
        {
            StoredItems.Remove(itemId);
        }

        actor.TryAddItemToInventory(new InventoryItem(itemId, itemId, toTake));
        bool backpackFullAfter = actor.FreeInventorySlots <= 0;
        return ChestWithdrawResult.Succeeded(toTake, backpackFullAfter, itemId, itemId, 0);
    }
}
