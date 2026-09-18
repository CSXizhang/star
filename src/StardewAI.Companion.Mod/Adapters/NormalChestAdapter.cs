using StardewModdingAPI;
using StardewValley;
using StardewValley.Objects;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Production chest adapter executing deposit and in-chest organize through normal game mechanics.
/// Enforces:
/// 1. Main-thread and current-map execution only.
/// 2. Only normal chests (no fridge, no special chest types) are valid targets.
/// 3. Deposit uses the game's own chest transfer primitive (Chest.addItem, the same
///    stacking/partial-transfer logic the chest UI uses); the companion-side bookkeeping
///    mirrors Chest.grabItemFromInventory. Note: Chest.grabItemFromInventory itself cannot
///    be called outside an open ItemGrabMenu (it dereferences Game1.activeClickableMenu and
///    mutates the menu's held item), so the adapter uses the identical underlying transfer
///    call and verifies both-side deltas explicitly.
/// 4. Tools are never deposited; the human player's inventory is never touched and is
///    verified unchanged after each operation.
/// 5. Organize merges only same (QualifiedItemId, quality) stacks into the earliest slot via
///    Item.addToStack, never reorders, and verifies per-(itemId, quality) conservation.
/// </summary>
public sealed class NormalChestAdapter : IChestAdapter
{
    private readonly IWorldObserver _observer;
    private readonly IMonitor _monitor;

    public NormalChestAdapter(
        IWorldObserver observer,
        IMonitor monitor)
    {
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _monitor = monitor ?? throw new ArgumentNullException(nameof(monitor));
    }

    public ChestDepositResult DepositItem(
        IFarmerActor actor,
        string locationName,
        TileCoordinate chestTile,
        int inventorySlotIndex)
    {
        ArgumentNullException.ThrowIfNull(actor);

        if (!_observer.IsMainThread)
        {
            return ChestDepositResult.Failed("Chest operation rejected: must execute on the game main thread.");
        }

        if (!string.Equals(_observer.CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
        {
            return ChestDepositResult.Failed(
                $"Chest target map '{locationName}' does not match active map '{_observer.CurrentLocationName}'.");
        }

        if (!actor.Tile.IsAdjacentTo(chestTile))
        {
            return ChestDepositResult.PreconditionError(
                $"Actor at {actor.Tile} is not adjacent to chest tile {chestTile}.");
        }

        if (actor.GameFarmer is null)
        {
            return ChestDepositResult.Failed("Companion actor has no GameFarmer instance.");
        }

        var chest = ResolveNormalChest(locationName, chestTile);
        if (chest is null)
        {
            return ChestDepositResult.PreconditionError($"No normal chest found at tile {chestTile}.");
        }

        if (inventorySlotIndex < 0 || inventorySlotIndex >= actor.GameFarmer.Items.Count)
        {
            return ChestDepositResult.PreconditionError($"Inventory slot {inventorySlotIndex} is out of range.");
        }

        var item = actor.GameFarmer.Items[inventorySlotIndex];
        if (item is null)
        {
            return ChestDepositResult.PreconditionError($"Inventory slot {inventorySlotIndex} is empty.");
        }

        if (item is Tool)
        {
            return ChestDepositResult.PreconditionError(
                $"Item '{item.Name}' is a tool and is never deposited.", skipReason: "tool-excluded");
        }

        string itemId = item.QualifiedItemId ?? item.ItemId ?? item.Name;
        string itemName = item.Name;
        int quality = item.Quality;
        int stackBefore = item.Stack;

        // Human player baseline isolation BEFORE the transfer
        var playerBefore = _observer.GetPlayerSnapshot();
        var originalPlayerRef = Game1.player;
        int chestTotalBefore = ChestTotalStack(chest);

        try
        {
            // Genuine game transfer: identical stacking/partial-transfer semantics to the
            // chest UI (Chest.grabItemFromInventory delegates to Chest.addItem for the move).
            Item leftover = chest.addItem(item);
            int moved = leftover is null ? stackBefore : stackBefore - item.Stack;

            if (leftover is null)
            {
                // Fully moved: the item object now lives in the chest; clear the companion slot.
                // (Companion-side bookkeeping mirroring grabItemFromInventory's removal branch.)
                if (inventorySlotIndex < actor.GameFarmer.Items.Count &&
                    object.ReferenceEquals(actor.GameFarmer.Items[inventorySlotIndex], item))
                {
                    actor.GameFarmer.Items[inventorySlotIndex] = null;
                }
            }

            bool chestFull = leftover is not null;

            // Verify both-side deltas: chest gained exactly what the companion lost.
            int chestTotalAfter = ChestTotalStack(chest);
            if (chestTotalAfter != chestTotalBefore + moved)
            {
                throw new InvalidOperationException(
                    $"Chest deposit delta verification failed at {chestTile}: chest total {chestTotalBefore} -> {chestTotalAfter}, moved {moved}.");
            }

            if (moved <= 0)
            {
                return ChestDepositResult.PreconditionError(
                    $"Chest at {chestTile} is full; nothing was deposited.", skipReason: "chest-full");
            }

            return ChestDepositResult.Succeeded(moved, chestFull, itemId, itemName, quality);
        }
        finally
        {
            if (!object.ReferenceEquals(originalPlayerRef, Game1.player))
            {
                throw new InvalidOperationException("CRITICAL SAFETY VIOLATION: Game1.player reference was modified during chest deposit!");
            }

            var playerAfter = _observer.GetPlayerSnapshot();
            if (!playerBefore.EqualsPlayerResources(playerAfter))
            {
                throw new InvalidOperationException(
                    $"CRITICAL SAFETY VIOLATION: Human player resources changed during companion chest deposit! Before: {playerBefore}, After: {playerAfter}");
            }
        }
    }

    public ChestOrganizeResult OrganizeChest(
        IFarmerActor actor,
        string locationName,
        TileCoordinate chestTile)
    {
        ArgumentNullException.ThrowIfNull(actor);

        if (!_observer.IsMainThread)
        {
            return ChestOrganizeResult.Failed("Chest operation rejected: must execute on the game main thread.");
        }

        if (!string.Equals(_observer.CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
        {
            return ChestOrganizeResult.Failed(
                $"Chest target map '{locationName}' does not match active map '{_observer.CurrentLocationName}'.");
        }

        if (!actor.Tile.IsAdjacentTo(chestTile))
        {
            return ChestOrganizeResult.PreconditionError(
                $"Actor at {actor.Tile} is not adjacent to chest tile {chestTile}.");
        }

        var chest = ResolveNormalChest(locationName, chestTile);
        if (chest is null)
        {
            return ChestOrganizeResult.PreconditionError($"No normal chest found at tile {chestTile}.");
        }

        var playerBefore = _observer.GetPlayerSnapshot();
        var originalPlayerRef = Game1.player;
        var totalsBefore = ChestTotalsByKey(chest);

        try
        {
            var merges = new List<ChestMergeInfo>();
            var items = chest.Items;

            // Current merge target slot per (QualifiedItemId, quality) key.
            var targets = new Dictionary<(string ItemId, int Quality), int>();

            for (int slot = 0; slot < items.Count; slot++)
            {
                var source = items[slot];
                if (source is null) continue;

                var key = (source.QualifiedItemId ?? source.ItemId ?? source.Name, source.Quality);
                if (!targets.TryGetValue(key, out int targetSlot))
                {
                    targets[key] = slot;
                    continue;
                }

                var target = items[targetSlot];
                if (target is null)
                {
                    targets[key] = slot;
                    continue;
                }

                int sourceBefore = source.Stack;
                int leftover = target.addToStack(source);
                int moved = sourceBefore - leftover;

                if (moved > 0)
                {
                    merges.Add(new ChestMergeInfo(
                        ItemId: key.Item1,
                        ItemName: source.Name,
                        Quality: key.Quality,
                        MergedStack: moved,
                        FromSlot: slot,
                        ToSlot: targetSlot
                    ));
                }

                if (leftover <= 0)
                {
                    items[slot] = null;
                    if (target.Stack >= target.maximumStackSize())
                    {
                        targets.Remove(key);
                    }
                }
                else
                {
                    // Target is full; the partially merged source becomes the new earliest target.
                    source.Stack = leftover;
                    targets[key] = slot;
                }
            }

            // Conservation check: per-(itemId, quality) totals must be identical before and after.
            var totalsAfter = ChestTotalsByKey(chest);
            if (!TotalsEqual(totalsBefore, totalsAfter))
            {
                throw new InvalidOperationException(
                    $"Chest organize conservation check failed at {chestTile}: per-item totals changed during merge.");
            }

            return ChestOrganizeResult.Succeeded(merges);
        }
        finally
        {
            if (!object.ReferenceEquals(originalPlayerRef, Game1.player))
            {
                throw new InvalidOperationException("CRITICAL SAFETY VIOLATION: Game1.player reference was modified during chest organize!");
            }

            var playerAfter = _observer.GetPlayerSnapshot();
            if (!playerBefore.EqualsPlayerResources(playerAfter))
            {
                throw new InvalidOperationException(
                    $"CRITICAL SAFETY VIOLATION: Human player resources changed during companion chest organize! Before: {playerBefore}, After: {playerAfter}");
            }
        }
    }

    public ChestWithdrawResult WithdrawItem(
        IFarmerActor actor,
        string locationName,
        TileCoordinate chestTile,
        string itemId,
        int count)
    {
        ArgumentNullException.ThrowIfNull(actor);
        if (string.IsNullOrWhiteSpace(itemId))
        {
            throw new ArgumentException("itemId must not be null or whitespace.", nameof(itemId));
        }
        if (count <= 0)
        {
            return ChestWithdrawResult.PreconditionError("Requested count must be greater than zero.");
        }

        if (!_observer.IsMainThread)
        {
            return ChestWithdrawResult.Failed("Chest operation rejected: must execute on the game main thread.");
        }

        if (!string.Equals(_observer.CurrentLocationName, locationName, StringComparison.OrdinalIgnoreCase))
        {
            return ChestWithdrawResult.Failed(
                $"Chest target map '{locationName}' does not match active map '{_observer.CurrentLocationName}'.");
        }

        if (!actor.Tile.IsAdjacentTo(chestTile))
        {
            return ChestWithdrawResult.PreconditionError(
                $"Actor at {actor.Tile} is not adjacent to chest tile {chestTile}.");
        }

        var chest = ResolveNormalChest(locationName, chestTile);
        if (chest is null)
        {
            return ChestWithdrawResult.PreconditionError($"No normal chest found at tile {chestTile}.");
        }

        int matchingSlot = -1;
        Item? foundItem = null;
        for (int i = 0; i < chest.Items.Count; i++)
        {
            var it = chest.Items[i];
            if (it is null) continue;

            string qid = it.QualifiedItemId ?? "";
            string rawId = it.ItemId ?? "";
            string name = it.Name ?? "";
            string disp = it.DisplayName ?? "";

            if (string.Equals(qid, itemId, StringComparison.OrdinalIgnoreCase) ||
                string.Equals(rawId, itemId, StringComparison.OrdinalIgnoreCase) ||
                string.Equals(disp, itemId, StringComparison.OrdinalIgnoreCase) ||
                string.Equals(name, itemId, StringComparison.OrdinalIgnoreCase) ||
                string.Equals(SeedIdNormalizer.ToCropDataId(qid), SeedIdNormalizer.ToCropDataId(itemId), StringComparison.OrdinalIgnoreCase))
            {
                matchingSlot = i;
                foundItem = it;
                break;
            }
        }

        if (foundItem is null || matchingSlot < 0)
        {
            return ChestWithdrawResult.PreconditionError(
                $"Item '{itemId}' was not found in chest at {chestTile}.", skipReason: "item-not-found");
        }

        string matchedItemId = foundItem.QualifiedItemId ?? foundItem.ItemId ?? foundItem.Name ?? "";
        string matchedItemName = foundItem.Name ?? "";
        int matchedQuality = foundItem.Quality;

        // Human player baseline isolation BEFORE the transfer
        var playerBefore = _observer.GetPlayerSnapshot();
        var originalPlayerRef = Game1.player;
        int chestTotalBefore = ChestTotalStack(chest);

        try
        {
            int toWithdraw = Math.Min(count, foundItem.Stack);
            Item itemToTransfer;
            bool entireStack = (toWithdraw == foundItem.Stack);

            if (entireStack)
            {
                itemToTransfer = foundItem;
            }
            else
            {
                itemToTransfer = foundItem.getOne();
                itemToTransfer.Stack = toWithdraw;
            }

            bool added = actor.TryAddItemToInventory(itemToTransfer);
            if (!added)
            {
                return ChestWithdrawResult.PreconditionError(
                    "Companion inventory is full; cannot withdraw item.", skipReason: "backpack-full");
            }

            if (entireStack)
            {
                chest.Items[matchingSlot] = null;
                chest.clearNulls();
            }
            else
            {
                foundItem.Stack -= toWithdraw;
                if (foundItem.Stack <= 0)
                {
                    chest.Items[matchingSlot] = null;
                    chest.clearNulls();
                }
            }

            // Verify both-side deltas
            int chestTotalAfter = ChestTotalStack(chest);
            if (chestTotalAfter != chestTotalBefore - toWithdraw)
            {
                throw new InvalidOperationException(
                    $"Chest withdraw delta verification failed at {chestTile}: chest total {chestTotalBefore} -> {chestTotalAfter}, expected deduction {toWithdraw}.");
            }

            bool backpackFullAfter = actor.FreeInventorySlots <= 0;
            return ChestWithdrawResult.Succeeded(toWithdraw, backpackFullAfter, matchedItemId, matchedItemName, matchedQuality);
        }
        finally
        {
            if (!object.ReferenceEquals(originalPlayerRef, Game1.player))
            {
                throw new InvalidOperationException("CRITICAL SAFETY VIOLATION: Game1.player reference was modified during chest withdraw!");
            }

            var playerAfter = _observer.GetPlayerSnapshot();
            if (!playerBefore.EqualsPlayerResources(playerAfter))
            {
                throw new InvalidOperationException(
                    $"CRITICAL SAFETY VIOLATION: Human player resources changed during companion chest withdraw! Before: {playerBefore}, After: {playerAfter}");
            }
        }
    }

    private Chest? ResolveNormalChest(string locationName, TileCoordinate chestTile)
    {
        var location = Game1.getLocationFromName(locationName) ?? Game1.currentLocation;
        if (location is null)
            return null;

        var v = new Microsoft.Xna.Framework.Vector2(chestTile.X, chestTile.Y);
        if (location.objects.TryGetValue(v, out var obj) && obj is Chest chest &&
            !chest.fridge.Value && chest.SpecialChestType == Chest.SpecialChestTypes.None)
        {
            return chest;
        }
        return null;
    }

    private static int ChestTotalStack(Chest chest)
    {
        int total = 0;
        foreach (var item in chest.Items)
        {
            if (item is not null) total += item.Stack;
        }
        return total;
    }

    private static Dictionary<(string ItemId, int Quality), int> ChestTotalsByKey(Chest chest)
    {
        var totals = new Dictionary<(string, int), int>();
        foreach (var item in chest.Items)
        {
            if (item is null) continue;
            var key = (item.QualifiedItemId ?? item.ItemId ?? item.Name, item.Quality);
            totals.TryGetValue(key, out int current);
            totals[key] = current + item.Stack;
        }
        return totals;
    }

    private static bool TotalsEqual(
        Dictionary<(string ItemId, int Quality), int> before,
        Dictionary<(string ItemId, int Quality), int> after)
    {
        if (before.Count != after.Count) return false;
        foreach (var (key, value) in before)
        {
            if (!after.TryGetValue(key, out int other) || other != value) return false;
        }
        return true;
    }
}
