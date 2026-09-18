namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// Versioned persistent state for the Companion Mechanics Actor.
/// </summary>
public sealed class CompanionActorState
{
    public const int CurrentSchemaVersion = 1;
    public const float DefaultMaxStamina = 270.0f;
    public const int DefaultMaxWater = 40;

    /// <summary>
    /// Schema format version.
    /// </summary>
    public int Version { get; set; } = CurrentSchemaVersion;

    /// <summary>
    /// Stable companion identifier across save sessions.
    /// </summary>
    public string CompanionId { get; set; } = "default-companion";

    /// <summary>
    /// Current stamina of the actor.
    /// </summary>
    public float Stamina { get; set; } = DefaultMaxStamina;

    /// <summary>
    /// Maximum stamina capacity.
    /// </summary>
    public float MaxStamina { get; set; } = DefaultMaxStamina;

    /// <summary>
    /// Current water remaining in actor's watering can.
    /// </summary>
    public int Water { get; set; } = DefaultMaxWater;

    /// <summary>
    /// Maximum water capacity of the watering can.
    /// </summary>
    public int MaxWater { get; set; } = DefaultMaxWater;

    /// <summary>
    /// Authoritative pose of the actor (location and facing).
    /// </summary>
    public AuthoritativePose Pose { get; set; } = new("Farm", 64, 15, FacingDirection.Down);

    /// <summary>
    /// Independent inventory items.
    /// </summary>
    public List<InventoryItem> Inventory { get; set; } = new();

    /// <summary>
    /// Timestamp of last save in UTC ISO 8601.
    /// </summary>
    public string LastSavedUtc { get; set; } = DateTime.UtcNow.ToString("o");

    /// <summary>
    /// Stored active task ID on disk (if any). Must NEVER be auto-resumed on reload!
    /// </summary>
    public string? PersistedTaskId { get; set; }

    /// <summary>
    /// Creates a fresh instance with initial default values.
    /// </summary>
    public static CompanionActorState CreateDefault(string companionId = "default-companion", string locationName = "Farm", int tileX = 64, int tileY = 15)
    {
        return new CompanionActorState
        {
            Version = CurrentSchemaVersion,
            CompanionId = companionId,
            Stamina = DefaultMaxStamina,
            MaxStamina = DefaultMaxStamina,
            Water = DefaultMaxWater,
            MaxWater = DefaultMaxWater,
            Pose = new AuthoritativePose(locationName, tileX, tileY, FacingDirection.Down),
            Inventory = new List<InventoryItem>
            {
                new InventoryItem(itemId: "(T)WateringCan", name: "Watering Can", stack: 1, slotIndex: 0, isTool: true, waterLeft: DefaultMaxWater),
                new InventoryItem(itemId: "(T)Hoe", name: "Hoe", stack: 1, slotIndex: 1, isTool: true)
            },
            LastSavedUtc = DateTime.UtcNow.ToString("o"),
            PersistedTaskId = null
        };
    }
}
