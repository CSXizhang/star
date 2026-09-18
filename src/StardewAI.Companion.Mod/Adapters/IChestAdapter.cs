using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Result of depositing one companion inventory item into a chest.
/// </summary>
public sealed class ChestDepositResult
{
    public bool Success { get; }
    public string? ErrorMessage { get; }
    public bool PreconditionFailed { get; }

    /// <summary>
    /// Set when the item must be skipped rather than failed: "tool-excluded" or "chest-full".
    /// </summary>
    public string? SkipReason { get; }

    /// <summary>Units actually moved from the companion inventory into the chest.</summary>
    public int MovedStack { get; }

    /// <summary>True when the chest could not accept the full stack (partial or zero transfer).</summary>
    public bool ChestFull { get; }

    public string? ItemId { get; }
    public string? ItemName { get; }
    public int Quality { get; }

    private ChestDepositResult(
        bool success,
        string? errorMessage,
        bool preconditionFailed,
        string? skipReason,
        int movedStack,
        bool chestFull,
        string? itemId,
        string? itemName,
        int quality)
    {
        Success = success;
        ErrorMessage = errorMessage;
        PreconditionFailed = preconditionFailed;
        SkipReason = skipReason;
        MovedStack = movedStack;
        ChestFull = chestFull;
        ItemId = itemId;
        ItemName = itemName;
        Quality = quality;
    }

    public static ChestDepositResult Succeeded(int movedStack, bool chestFull, string? itemId, string? itemName, int quality) =>
        new(true, null, false, null, movedStack, chestFull, itemId, itemName, quality);

    public static ChestDepositResult PreconditionError(string reason, string? skipReason = null) =>
        new(false, reason, true, skipReason, 0, false, null, null, 0);

    public static ChestDepositResult Failed(string reason) =>
        new(false, reason, false, null, 0, false, null, null, 0);
}

/// <summary>
/// Record of one genuine stack merge performed inside a chest.
/// </summary>
public sealed record ChestMergeInfo(
    string ItemId,
    string ItemName,
    int Quality,
    int MergedStack,
    int FromSlot,
    int ToSlot
);

/// <summary>
/// Result of an in-chest organize (same-type stack merge) operation.
/// </summary>
public sealed class ChestOrganizeResult
{
    public bool Success { get; }
    public string? ErrorMessage { get; }
    public bool PreconditionFailed { get; }
    public IReadOnlyList<ChestMergeInfo> Merges { get; }

    private ChestOrganizeResult(bool success, string? errorMessage, bool preconditionFailed, IReadOnlyList<ChestMergeInfo> merges)
    {
        Success = success;
        ErrorMessage = errorMessage;
        PreconditionFailed = preconditionFailed;
        Merges = merges;
    }

    public static ChestOrganizeResult Succeeded(IReadOnlyList<ChestMergeInfo> merges) =>
        new(true, null, false, merges);

    public static ChestOrganizeResult PreconditionError(string reason) =>
        new(false, reason, true, new List<ChestMergeInfo>());

    public static ChestOrganizeResult Failed(string reason) =>
        new(false, reason, false, new List<ChestMergeInfo>());
}

/// <summary>
/// Adapter contract for chest interactions (deposit and in-chest organize)
/// executed through normal game mechanics.
/// </summary>
public interface IChestAdapter
{
    /// <summary>
    /// Moves the item at the given companion inventory slot into the chest at the target tile,
    /// using the game's own chest transfer logic (stack merging and partial transfer included).
    /// Both-side deltas (companion inventory decrease, chest contents increase) are verified.
    /// Tools are never deposited. The human player's inventory is never touched.
    /// </summary>
    ChestDepositResult DepositItem(
        IFarmerActor actor,
        string locationName,
        TileCoordinate chestTile,
        int inventorySlotIndex);

    /// <summary>
    /// Merges same (QualifiedItemId, quality) stacks inside the chest at the target tile into
    /// the earliest slot using Item.addToStack. Never reorders, never moves across chests,
    /// and verifies per-(itemId, quality) total conservation before and after.
    /// </summary>
    ChestOrganizeResult OrganizeChest(
        IFarmerActor actor,
        string locationName,
        TileCoordinate chestTile);

    /// <summary>
    /// Withdraws up to count items of the given itemId from the chest at chestTile into the companion backpack.
    /// Deducts from chest stack, adds to companion backpack natively, and verifies conservation deltas.
    /// Human player inventory is verified untouched.
    /// </summary>
    ChestWithdrawResult WithdrawItem(
        IFarmerActor actor,
        string locationName,
        TileCoordinate chestTile,
        string itemId,
        int count);
}

/// <summary>
/// Result of withdrawing an item from a chest into companion inventory.
/// </summary>
public sealed class ChestWithdrawResult
{
    public bool Success { get; }
    public string? ErrorMessage { get; }
    public bool PreconditionFailed { get; }
    public string? SkipReason { get; }
    public int MovedStack { get; }
    public bool BackpackFull { get; }
    public string? ItemId { get; }
    public string? ItemName { get; }
    public int Quality { get; }

    private ChestWithdrawResult(
        bool success,
        string? errorMessage,
        bool preconditionFailed,
        string? skipReason,
        int movedStack,
        bool backpackFull,
        string? itemId,
        string? itemName,
        int quality)
    {
        Success = success;
        ErrorMessage = errorMessage;
        PreconditionFailed = preconditionFailed;
        SkipReason = skipReason;
        MovedStack = movedStack;
        BackpackFull = backpackFull;
        ItemId = itemId;
        ItemName = itemName;
        Quality = quality;
    }

    public static ChestWithdrawResult Succeeded(int movedStack, bool backpackFull, string? itemId, string? itemName, int quality) =>
        new(true, null, false, null, movedStack, backpackFull, itemId, itemName, quality);

    public static ChestWithdrawResult PreconditionError(string reason, string? skipReason = null) =>
        new(false, reason, true, skipReason, 0, false, null, null, 0);

    public static ChestWithdrawResult Failed(string reason) =>
        new(false, reason, false, null, 0, false, null, null, 0);
}
