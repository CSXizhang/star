namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// Serializable summary of an item in the Mechanics Actor inventory.
/// </summary>
public sealed record InventoryItem
{
    public int SlotIndex { get; init; } = 0;
    public string ItemId { get; init; } = "";
    public string Name { get; init; } = "";
    public int Stack { get; init; } = 1;
    public int Quality { get; init; } = 0;
    public string Category { get; init; } = "";
    public int UpgradeLevel { get; init; } = 0;
    public int WaterLeft { get; init; } = 0;

    /// <summary>
    /// True when the item is a tool (StardewValley.Tool). Tools are never deposited into chests.
    /// </summary>
    public bool IsTool { get; init; } = false;

    /// <summary>
    /// Localized display name (e.g. "胡萝卜种子" or "Carrot Seeds").
    /// </summary>
    public string DisplayName { get; init; } = "";

    public InventoryItem() { }

    public InventoryItem(
        string itemId,
        string name,
        int stack = 1,
        int quality = 0,
        string category = "",
        int upgradeLevel = 0,
        int waterLeft = 0,
        int slotIndex = 0,
        bool isTool = false,
        string? displayName = null)
    {
        SlotIndex = slotIndex;
        ItemId = itemId;
        Name = name;
        Stack = stack;
        Quality = quality;
        Category = category;
        UpgradeLevel = upgradeLevel;
        WaterLeft = waterLeft;
        IsTool = isTool;
        DisplayName = displayName ?? name;
    }
}
