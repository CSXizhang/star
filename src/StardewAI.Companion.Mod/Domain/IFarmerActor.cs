using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Tools;

namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// Authoritative Mechanics Actor interface governing physical pose, continuous movement,
/// independent stamina, actual WateringCan, and inventory.
/// </summary>
public interface IFarmerActor
{
    string CompanionId { get; }
    float Stamina { get; set; }
    int MaxStamina { get; }
    int WaterLeft { get; set; }
    int MaxWater { get; }
    string LocationName { get; }
    Vector2 PixelPosition { get; set; }
    TileCoordinate Tile { get; }
    FacingDirection Facing { get; set; }
    bool IsExhausted { get; }
    bool IsWateringCanEmpty { get; }
    string? ActiveTaskId { get; }

    void SetActiveTask(string? taskId);
    void MovePixels(float dx, float dy);
    void Face(FacingDirection dir);
    void Halt();
    void SetLocation(string locationName, TileCoordinate tile);

    WateringCan? WateringCan { get; }
    Hoe? Hoe { get; }

    /// <summary>
    /// The companion's own tool of type <typeparamref name="T"/>, or null when it does
    /// not carry one. Tools are never granted by the Mod: they exist only when the
    /// player provided/obtained them, they live in the companion's inventory and they
    /// persist through <see cref="CapturePersistentState"/> exactly like any item.
    /// </summary>
    T? FindTool<T>() where T : StardewValley.Tool;

    /// <summary>
    /// Native tool class names the companion currently carries (e.g. "Axe", "Pickaxe",
    /// "MilkPail", "Shears"), used for observation and actionable errors.
    /// </summary>
    IReadOnlyList<string> GetToolNames();

    Farmer? GameFarmer { get; }

    bool IsUsingTool { get; }
    int CurrentFrame { get; }
    ToolAnimationPhase AnimationPhase { get; }

    void BeginUsingTool();
    ToolAnimationPhase UpdateToolAnimation(GameTime? time, long tickCount);
    void EndUsingTool();

    void ApplyPersistentState(CompanionActorState state);
    CompanionActorState CapturePersistentState();

    /// <summary>
    /// Total inventory slot capacity (farmer.MaxItems; 36 by default).
    /// </summary>
    int InventoryCapacity { get; }

    /// <summary>
    /// Number of currently empty inventory slots within capacity.
    /// </summary>
    int FreeInventorySlots { get; }

    /// <summary>
    /// Snapshot of non-empty inventory slots (with slot indices), ordered by slot.
    /// </summary>
    IReadOnlyList<InventoryItem> GetInventorySnapshot();

    /// <summary>
    /// Adds an item to the inventory through normal stacking rules:
    /// merges into existing compatible stacks first, then occupies the lowest free slot.
    /// Returns false when the item could not be fully added (no merge target and no free slot).
    /// </summary>
    bool TryAddItemToInventory(InventoryItem item);

    /// <summary>
    /// Adds a live game Item instance directly into companion inventory using game stacking rules.
    /// Merges into compatible existing stacks, or places in lowest free slot.
    /// </summary>
    bool TryAddItemToInventory(Item gameItem);

    /// <summary>
    /// Removes up to <paramref name="stackToRemove"/> units from the item at the given slot.
    /// Removes the slot's item entirely when the remaining stack would reach zero.
    /// Returns false (and changes nothing) when the slot is empty or out of range.
    /// </summary>
    bool TryRemoveItemAtSlot(int slotIndex, int stackToRemove, out InventoryItem? removed);

    /// <summary>
    /// Consumes up to <paramref name="count"/> units of the item matching <paramref name="itemId"/>
    /// from the companion inventory. Returns true if fully consumed, false if insufficient items.
    /// </summary>
    bool TryConsumeItem(string itemId, int count = 1);

    /// <summary>
    /// Extracts up to <paramref name="count"/> units of the item matching <paramref name="itemId"/>
    /// from the companion inventory as real Item instances (preserving quality, stack, identity).
    /// Returns true if fully extracted; false if insufficient items (inventory remains untouched).
    /// </summary>
    bool TryExtractItem(string itemId, int count, out List<Item> extractedItems);

    /// <summary>
    /// Returns the total stack count of items matching <paramref name="itemId"/> in inventory.
    /// </summary>
    int GetItemCount(string itemId);
}

public enum ToolAnimationPhase
{
    None,
    Windup,
    EffectPoint,
    FollowThrough,
    Completed
}

