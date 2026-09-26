using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace StardewAI.Companion.Mod.Transport;

public sealed record TileCoord(
    [property: JsonPropertyName("x")] int X,
    [property: JsonPropertyName("y")] int Y
);

public sealed record EnvelopeDto(
    [property: JsonPropertyName("protocolVersion")] string ProtocolVersion,
    [property: JsonPropertyName("messageType")] string MessageType,
    [property: JsonPropertyName("messageId")] string MessageId,
    [property: JsonPropertyName("senderInstanceId")] string SenderInstanceId,
    [property: JsonPropertyName("sequenceNumber")] long SequenceNumber,
    [property: JsonPropertyName("worldRevision")] long WorldRevision,
    [property: JsonPropertyName("sentAt")] DateTimeOffset SentAt,
    [property: JsonPropertyName("payload")] JsonObject Payload,
    [property: JsonPropertyName("correlationId")] string? CorrelationId = null,
    [property: JsonPropertyName("saveId")] string? SaveId = null,
    [property: JsonPropertyName("gameSessionId")] string? GameSessionId = null,
    [property: JsonPropertyName("expiresAt")] DateTimeOffset? ExpiresAt = null,
    [property: JsonPropertyName("idempotencyKey")] string? IdempotencyKey = null
);

public sealed record RuntimeHelloPayload(
    [property: JsonPropertyName("supportedProtocolVersions")] string[] SupportedProtocolVersions,
    [property: JsonPropertyName("runtimeVersion")] string RuntimeVersion,
    [property: JsonPropertyName("sessionToken")] string? SessionToken,
    [property: JsonPropertyName("features")] Dictionary<string, bool> Features
);

public sealed record ModWelcomePayload(
    [property: JsonPropertyName("selectedProtocolVersion")] string SelectedProtocolVersion,
    [property: JsonPropertyName("modVersion")] string ModVersion,
    [property: JsonPropertyName("gameVersion")] string GameVersion,
    [property: JsonPropertyName("smapiVersion")] string SmapiVersion,
    [property: JsonPropertyName("lifecycleState")] string LifecycleState,
    [property: JsonPropertyName("skills")] string[] Skills
);

public sealed record CompanionSnapshot(
    [property: JsonPropertyName("locationId")] string LocationId,
    [property: JsonPropertyName("tileX")] int TileX,
    [property: JsonPropertyName("tileY")] int TileY,
    [property: JsonPropertyName("facingDirection")] int FacingDirection,
    [property: JsonPropertyName("stamina")] float Stamina,
    [property: JsonPropertyName("maxStamina")] int MaxStamina,
    [property: JsonPropertyName("waterCanLevel")] int WaterCanLevel,
    [property: JsonPropertyName("maxWaterCanLevel")] int MaxWaterCanLevel,
    [property: JsonPropertyName("hasWateringCan")] bool HasWateringCan,
    [property: JsonPropertyName("activity")] string Activity,
    // Thin-observer additions required by the compact decision context: the
    // companion wallet is a real per-save fact. Nullable so an older serialized
    // snapshot stays "unknown" instead of being read as a fabricated zero.
    [property: JsonPropertyName("availableMoney"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? AvailableMoney = null,
    [property: JsonPropertyName("moneyStatus"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? MoneyStatus = null
);

public sealed record MatureCropTile(
    [property: JsonPropertyName("x")] int X,
    [property: JsonPropertyName("y")] int Y,
    [property: JsonPropertyName("cropId")] string CropId
);

public sealed record FarmWorkSnapshot(
    [property: JsonPropertyName("tilledUnwateredTiles")] List<TileCoord> TilledUnwateredTiles,
    [property: JsonPropertyName("tilledUnwateredCount")] int TilledUnwateredCount,
    [property: JsonPropertyName("isTruncated")] bool IsTruncated,
    [property: JsonPropertyName("matureCropCount")] int MatureCropCount,
    [property: JsonPropertyName("matureCrops")] List<MatureCropTile>? MatureCrops = null,
    [property: JsonPropertyName("matureCropsTruncated")] bool MatureCropsTruncated = false,
    [property: JsonPropertyName("cropUnwateredTiles")] List<TileCoord>? CropUnwateredTiles = null,
    [property: JsonPropertyName("cropUnwateredCount")] int? CropUnwateredCount = null,
    [property: JsonPropertyName("cropUnwateredTruncated")] bool CropUnwateredTruncated = false
)
{
    [JsonPropertyName("truncated")]
    public bool Truncated => IsTruncated;

    public static FarmWorkSnapshot CreateEmpty() => new(
        TilledUnwateredTiles: new List<TileCoord>(),
        TilledUnwateredCount: 0,
        IsTruncated: false,
        MatureCropCount: 0,
        MatureCrops: new List<MatureCropTile>(),
        MatureCropsTruncated: false,
        CropUnwateredTiles: new List<TileCoord>(),
        CropUnwateredCount: 0,
        CropUnwateredTruncated: false
    );
}

public sealed record InventorySlotSnapshot(
    [property: JsonPropertyName("index")] int Index,
    [property: JsonPropertyName("itemId")] string ItemId,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("displayName"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? DisplayName = null,
    [property: JsonPropertyName("stack")] int Stack = 1,
    [property: JsonPropertyName("quality")] int Quality = 0,
    [property: JsonPropertyName("isTool"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingDefault)] bool IsTool = false
);

public sealed record CompanionInventorySnapshot(
    [property: JsonPropertyName("capacity")] int Capacity,
    [property: JsonPropertyName("freeSlots")] int FreeSlots,
    [property: JsonPropertyName("slots")] List<InventorySlotSnapshot> Slots
);

public sealed record ChestSlotSnapshot(
    [property: JsonPropertyName("slot")] int Slot,
    [property: JsonPropertyName("itemId")] string ItemId,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("displayName"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? DisplayName = null,
    [property: JsonPropertyName("stack")] int Stack = 1,
    [property: JsonPropertyName("quality")] int Quality = 0,
    [property: JsonPropertyName("isSeed"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingDefault)] bool IsSeed = false,
    [property: JsonPropertyName("seasons"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] List<string>? Seasons = null
);

public sealed record ChestSnapshot(
    [property: JsonPropertyName("tile")] TileCoord Tile,
    [property: JsonPropertyName("capacity")] int Capacity,
    [property: JsonPropertyName("freeSlots")] int FreeSlots,
    [property: JsonPropertyName("contents")] List<ChestSlotSnapshot> Contents
);

public sealed record ChestsSnapshot(
    [property: JsonPropertyName("truncated")] bool Truncated,
    [property: JsonPropertyName("items")] List<ChestSnapshot> Items
);

/// <summary>
/// Player backpack aggregate (§2.4): one entry per distinct item name with the
/// summed stack, used by the runtime to verify milestone completion.
/// </summary>
public sealed record PlayerItemDto(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("quantity")] int Quantity
);

public sealed record WorldStateSnapshot(
    [property: JsonPropertyName("currentLocation")] string CurrentLocation,
    [property: JsonPropertyName("timeOfDay")] int TimeOfDay,
    [property: JsonPropertyName("season")] string Season,
    [property: JsonPropertyName("dayOfMonth")] int DayOfMonth,
    [property: JsonPropertyName("isRaining")] bool IsRaining,
    [property: JsonPropertyName("farmWork")] FarmWorkSnapshot? FarmWork = null,
    [property: JsonPropertyName("year")] int Year = 1,
    // Real native weather icon id (Stardew's weatherIcon). isRaining=false does
    // NOT imply clear (snow/storm/etc), so the actual weather is published.
    // Nullable: a snapshot written before this field existed stays unknown rather
    // than guessing "clear".
    [property: JsonPropertyName("weatherIcon"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? WeatherIcon = null,
    // §2.4 player backpack aggregate for milestone-completion verification.
    // Nullable: snapshots written before this field existed must stay readable
    // and simply leave verification without item evidence.
    [property: JsonPropertyName("playerItems"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] List<PlayerItemDto>? PlayerItems = null,
    [property: JsonPropertyName("playerMoney"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? PlayerMoney = null,
    [property: JsonPropertyName("playerStamina"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] float? PlayerStamina = null,
    [property: JsonPropertyName("playerMaxStamina"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? PlayerMaxStamina = null
);

public sealed record SeedItemSnapshot(
    [property: JsonPropertyName("itemId")] string ItemId,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("stack")] int Stack,
    [property: JsonPropertyName("canPlantCurrentSeason")] bool CanPlantCurrentSeason,
    [property: JsonPropertyName("seasons")] List<string> Seasons,
    [property: JsonPropertyName("growthDays")] int? GrowthDays = null,
    [property: JsonPropertyName("regrows")] bool? Regrows = null,
    [property: JsonPropertyName("isRaised")] bool? IsRaised = null
);

public sealed record CandidateTilesSnapshot(
    [property: JsonPropertyName("tilledEmptyCount")] int TilledEmptyCount,
    [property: JsonPropertyName("tilledEmptyTiles")] List<TileCoord> TilledEmptyTiles,
    [property: JsonPropertyName("tilledEmptyTruncated")] bool TilledEmptyTruncated,
    [property: JsonPropertyName("tillableCount")] int TillableCount,
    [property: JsonPropertyName("tillableTiles")] List<TileCoord> TillableTiles,
    [property: JsonPropertyName("tillableTruncated")] bool TillableTruncated
);

public sealed record SearchBoundsSnapshot(
    [property: JsonPropertyName("center")] TileCoord Center,
    [property: JsonPropertyName("radius")] int Radius
);

public sealed record PlantingSnapshot(
    [property: JsonPropertyName("season")] string Season,
    [property: JsonPropertyName("dayOfMonth")] int DayOfMonth,
    [property: JsonPropertyName("companionHasHoe")] bool CompanionHasHoe,
    [property: JsonPropertyName("seeds")] List<SeedItemSnapshot> Seeds,
    [property: JsonPropertyName("candidateTiles")] CandidateTilesSnapshot CandidateTiles,
    [property: JsonPropertyName("searchBounds")] SearchBoundsSnapshot SearchBounds
);

public sealed record ShopItemSnapshot(
    [property: JsonPropertyName("itemId")] string ItemId,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("price")] int Price,
    [property: JsonPropertyName("stock")] int Stock,
    [property: JsonPropertyName("isInfiniteStock")] bool IsInfiniteStock,
    [property: JsonPropertyName("tradeItem"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? TradeItem = null,
    [property: JsonPropertyName("tradeItemCount"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? TradeItemCount = null,
    [property: JsonPropertyName("limitedStockMode"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? LimitedStockMode = null,
    [property: JsonPropertyName("actionsOnPurchase"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] List<string>? ActionsOnPurchase = null,
    [property: JsonPropertyName("category"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? Category = null,
    [property: JsonPropertyName("isSeed")] bool IsSeed = false
);

public sealed record ShopSnapshot(
    [property: JsonPropertyName("shopId")] string ShopId,
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("isOpen")] bool IsOpen,
    [property: JsonPropertyName("ownerPresent")] bool OwnerPresent,
    [property: JsonPropertyName("closedMessage"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? ClosedMessage = null,
    [property: JsonPropertyName("owners")] List<string> Owners = null!,
    [property: JsonPropertyName("currency")] int Currency = 0,
    [property: JsonPropertyName("availableMoney")] int? AvailableMoney = null,
    [property: JsonPropertyName("moneyStatus")] string MoneyStatus = "missing",
    [property: JsonPropertyName("itemsCount")] int ItemsCount = 0,
    [property: JsonPropertyName("items")] List<ShopItemSnapshot> Items = null!,
    [property: JsonPropertyName("errorMessage"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? ErrorMessage = null,
    [property: JsonPropertyName("locationId"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? LocationId = null,
    [property: JsonPropertyName("interactionTile"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] TileCoord? InteractionTile = null
);

public sealed record WorldSnapshotPayload(
    [property: JsonPropertyName("capturedRevision")] long CapturedRevision,
    [property: JsonPropertyName("companion")] CompanionSnapshot Companion,
    [property: JsonPropertyName("world")] WorldStateSnapshot World,
    [property: JsonPropertyName("farmWork")] FarmWorkSnapshot? FarmWork = null,
    [property: JsonPropertyName("inventory"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] CompanionInventorySnapshot? Inventory = null,
    [property: JsonPropertyName("chests"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] ChestsSnapshot? Chests = null,
    [property: JsonPropertyName("planting"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] PlantingSnapshot? Planting = null,
    [property: JsonPropertyName("shop"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] ShopSnapshot? Shop = null,
    [property: JsonPropertyName("machines"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] MachinesSnapshot? Machines = null,
    [property: JsonPropertyName("livestock"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] LivestockSnapshot? Livestock = null,
    [property: JsonPropertyName("farming"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] FarmingSnapshot? Farming = null
);

// ---------------------------------------------------------------------------
// On-demand grouped observation sections (farming helpers / machines / livestock).
// Nullable on WorldSnapshotPayload so an older reader/writer stays compatible.
// ---------------------------------------------------------------------------

public sealed record MachineSnapshot(
    [property: JsonPropertyName("tile")] TileCoord Tile,
    [property: JsonPropertyName("itemId")] string ItemId,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("isReady")] bool IsReady,
    [property: JsonPropertyName("minutesUntilReady")] int MinutesUntilReady,
    [property: JsonPropertyName("outputItemId"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? OutputItemId = null,
    [property: JsonPropertyName("outputName"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? OutputName = null,
    [property: JsonPropertyName("outputStack")] int OutputStack = 0,
    [property: JsonPropertyName("outputQuality")] int OutputQuality = 0,
    [property: JsonPropertyName("lastInputItemId"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? LastInputItemId = null,
    [property: JsonPropertyName("hasInput")] bool HasInput = false
);

public sealed record MachinesSnapshot(
    [property: JsonPropertyName("truncated")] bool Truncated,
    [property: JsonPropertyName("items")] List<MachineSnapshot> Items
);

public sealed record AnimalSnapshot(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("animalType")] string AnimalType,
    [property: JsonPropertyName("displayType")] string DisplayType,
    [property: JsonPropertyName("age")] int Age,
    [property: JsonPropertyName("happiness")] int Happiness,
    [property: JsonPropertyName("fullness")] int Fullness,
    [property: JsonPropertyName("friendship")] int Friendship,
    [property: JsonPropertyName("produceReady")] bool ProduceReady,
    [property: JsonPropertyName("currentProduceId"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? CurrentProduceId = null,
    [property: JsonPropertyName("harvestType"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? HarvestType = null,
    [property: JsonPropertyName("requiredTool"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? RequiredTool = null,
    [property: JsonPropertyName("buildingType"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? BuildingType = null,
    [property: JsonPropertyName("location"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Location = null,
    [property: JsonPropertyName("tile"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] TileCoord? Tile = null,
    [property: JsonPropertyName("wasPetToday")] bool WasPetToday = false,
    [property: JsonPropertyName("wasAutoPetToday")] bool WasAutoPetToday = false
);

public sealed record AnimalBuildingSnapshot(
    [property: JsonPropertyName("buildingType")] string BuildingType,
    [property: JsonPropertyName("indoorsName")] string IndoorsName,
    [property: JsonPropertyName("location"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Location,
    [property: JsonPropertyName("tile")] TileCoord Tile,
    [property: JsonPropertyName("doorTile")] TileCoord DoorTile,
    [property: JsonPropertyName("animalDoorOpen")] bool AnimalDoorOpen,
    [property: JsonPropertyName("doorStateKnown")] bool DoorStateKnown,
    [property: JsonPropertyName("animalCount")] int AnimalCount,
    [property: JsonPropertyName("animalLimit")] int AnimalLimit,
    [property: JsonPropertyName("hayCount")] int HayCount,
    [property: JsonPropertyName("hayCapacity")] int HayCapacity,
    [property: JsonPropertyName("siloHayCount")] int SiloHayCount,
    [property: JsonPropertyName("animals")] List<AnimalSnapshot> Animals
);

public sealed record LivestockSnapshot(
    [property: JsonPropertyName("buildingsTruncated")] bool BuildingsTruncated,
    [property: JsonPropertyName("animalsTruncated")] bool AnimalsTruncated,
    [property: JsonPropertyName("buildings")] List<AnimalBuildingSnapshot> Buildings,
    [property: JsonPropertyName("roamingAnimals")] List<AnimalSnapshot> RoamingAnimals
);

public sealed record GroundItemSnapshot(
    [property: JsonPropertyName("tile")] TileCoord Tile,
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("itemId")] string ItemId,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("stack")] int Stack,
    [property: JsonPropertyName("isDropped")] bool IsDropped,
    [property: JsonPropertyName("isWeed")] bool IsWeed,
    [property: JsonPropertyName("canBeGrabbed")] bool CanBeGrabbed,
    [property: JsonPropertyName("clearTool"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? ClearTool = null,
    [property: JsonPropertyName("isStone")] bool IsStone = false,
    [property: JsonPropertyName("isTwig")] bool IsTwig = false
);

public sealed record ChoppableTreeSnapshot(
    [property: JsonPropertyName("tile")] TileCoord Tile,
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("growthStage")] int GrowthStage,
    [property: JsonPropertyName("tapped")] bool Tapped,
    [property: JsonPropertyName("width")] int Width = 1,
    [property: JsonPropertyName("height")] int Height = 1
);

public sealed record FarmingSnapshot(
    [property: JsonPropertyName("location")] string Location,
    [property: JsonPropertyName("refillWaterTiles")] List<TileCoord> RefillWaterTiles,
    [property: JsonPropertyName("groundItems")] List<GroundItemSnapshot> GroundItems,
    [property: JsonPropertyName("groundItemsTruncated")] bool GroundItemsTruncated,
    [property: JsonPropertyName("fertilizedTiles")] List<TileCoord> FertilizedTiles,
    // Tools the companion really carries (from its own inventory). Observation only:
    // the Mod never grants tools, and the model needs to know what it can act with.
    [property: JsonPropertyName("companionTools")] List<string> CompanionTools,
    [property: JsonPropertyName("choppableTrees")] List<ChoppableTreeSnapshot>? ChoppableTrees = null,
    [property: JsonPropertyName("choppableTreesTruncated")] bool ChoppableTreesTruncated = false
);


/// <summary>
public sealed record ShippingItemRequest(
    [property: JsonPropertyName("itemId")] string ItemId,
    [property: JsonPropertyName("count")] int Count
);

/// <summary>
/// Generalized skill.execute parameters DTO. Water-zone, harvest-zone, hoe-tiles,
/// and plant-seeds use <see cref="Tiles"/>; chest skills use <see cref="ChestTile"/>
/// (and optional <see cref="ItemIds"/> for deposit-chest). Plant skills use
/// <see cref="SeedItemId"/>. Ship skills use <see cref="Items"/>. The historical
/// class name is kept for wire compatibility.
/// </summary>
public sealed record WaterZoneParameters(
    [property: JsonPropertyName("locationId")] string LocationId,
    [property: JsonPropertyName("tiles")] List<TileCoord>? Tiles = null
)
{
    [JsonPropertyName("includeEmptyTiles")]
    public bool IncludeEmptyTiles { get; init; }
    [JsonPropertyName("tile")]
    public TileCoord? Tile { get; init; }

    [JsonPropertyName("chestTile")]
    public TileCoord? ChestTile { get; init; }

    [JsonPropertyName("itemIds")]
    public List<string>? ItemIds { get; init; }

    [JsonPropertyName("seedItemId")]
    public string? SeedItemId { get; init; }

    [JsonPropertyName("items")]
    public List<ShippingItemRequest>? Items { get; init; }

    [JsonPropertyName("shopId")]
    public string? ShopId { get; init; }

    [JsonPropertyName("fertilizerItemId")]
    public string? FertilizerItemId { get; init; }

    [JsonPropertyName("itemId")]
    public string? ItemId { get; init; }

    [JsonPropertyName("itemCount")]
    public int? ItemCount { get; init; }

    [JsonPropertyName("animalName")]
    public string? AnimalName { get; init; }

    [JsonPropertyName("buildingName")]
    public string? BuildingName { get; init; }

    [JsonPropertyName("budgetLimit")]
    public int? BudgetLimit { get; init; }

    [JsonPropertyName("budget_limit")]
    public int? BudgetLimitSnakeCase { get; init; }

    [JsonIgnore]
    public int? EffectiveBudgetLimit => BudgetLimit ?? BudgetLimitSnakeCase;
}

public sealed record ExecutionBudgets(
    [property: JsonPropertyName("maxGameMinutes")] int MaxGameMinutes,
    [property: JsonPropertyName("maxStamina")] float MaxStamina,
    [property: JsonPropertyName("maxWater")] int MaxWater
);

public sealed record SkillExecutePayload(
    [property: JsonPropertyName("commandId")] string CommandId,
    [property: JsonPropertyName("taskId")] string TaskId,
    [property: JsonPropertyName("skillId")] string SkillId,
    [property: JsonPropertyName("skillVersion")] string SkillVersion,
    [property: JsonPropertyName("expectedWorldRevision")] long ExpectedWorldRevision,
    [property: JsonPropertyName("parameters")] WaterZoneParameters Parameters,
    [property: JsonPropertyName("budgets")] ExecutionBudgets Budgets,
    [property: JsonPropertyName("cancelPolicy")] string CancelPolicy,
    [property: JsonPropertyName("policyDecisionId")] string PolicyDecisionId
);

public sealed record SkillCancelPayload(
    [property: JsonPropertyName("commandId")] string CommandId,
    [property: JsonPropertyName("reason")] string Reason,
    [property: JsonPropertyName("requestedByPlayer")] bool RequestedByPlayer,
    [property: JsonPropertyName("cancelPolicy")] string CancelPolicy
);

public sealed record SkillPausePayload(
    [property: JsonPropertyName("commandId")] string CommandId,
    [property: JsonPropertyName("reason")] string Reason
);

public sealed record SkillResumePayload(
    [property: JsonPropertyName("commandId")] string CommandId
);

public sealed record SkillResultResources(
    [property: JsonPropertyName("staminaUsed")] float StaminaUsed,
    [property: JsonPropertyName("waterUsed")] int WaterUsed,
    [property: JsonPropertyName("gameMinutesElapsed")] int GameMinutesElapsed
);

public sealed record SkillResultError(
    [property: JsonPropertyName("code")] string Code,
    [property: JsonPropertyName("message")] string Message,
    [property: JsonPropertyName("details")] string? Details = null,
    [property: JsonPropertyName("retryable")] bool Retryable = false
);

public sealed record SkillResultPayload(
    [property: JsonPropertyName("commandId")] string CommandId,
    [property: JsonPropertyName("taskId")] string TaskId,
    [property: JsonPropertyName("terminalState")] string TerminalState,
    [property: JsonPropertyName("completedCount")] int CompletedCount,
    [property: JsonPropertyName("skippedCount")] int SkippedCount,
    [property: JsonPropertyName("failedCount")] int FailedCount,
    [property: JsonPropertyName("finalWorldRevision")] long FinalWorldRevision,
    [property: JsonPropertyName("effects")] List<Dictionary<string, object>> Effects,
    [property: JsonPropertyName("resources")] SkillResultResources? Resources = null,
    [property: JsonPropertyName("error")] SkillResultError? Error = null,
    [property: JsonPropertyName("retryRecommended")] bool RetryRecommended = false,
    [property: JsonPropertyName("playerActionRequired")] bool PlayerActionRequired = false,
    [property: JsonPropertyName("skillId"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? SkillId = null,
    [property: JsonPropertyName("details"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] Dictionary<string, object>? Details = null,
    [property: JsonPropertyName("targetAdjusted"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] bool? TargetAdjusted = null,
    [property: JsonPropertyName("requestedTile"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] TileCoord? RequestedTile = null
);

public sealed record ProtocolErrorPayload(
    [property: JsonPropertyName("code")] string Code,
    [property: JsonPropertyName("message")] string Message,
    [property: JsonPropertyName("details")] string? Details = null
);

public sealed record ChatSubmitPayload(
    [property: JsonPropertyName("requestId")] string RequestId,
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("source")] string Source = "text",
    [property: JsonPropertyName("saveId")] string? SaveId = null
);

public sealed record ChatReplyPayload(
    [property: JsonPropertyName("requestId")] string RequestId,
    [property: JsonPropertyName("status")] string Status, // "processing", "completed", "failed", "cancelled"
    [property: JsonPropertyName("replyText")] string ReplyText,
    [property: JsonPropertyName("tokensUsed")] long? TokensUsed = null,
    [property: JsonPropertyName("promptTokens")] long? PromptTokens = null,
    [property: JsonPropertyName("outputTokens")] long? OutputTokens = null,
    [property: JsonPropertyName("cachedTokens")] long? CachedTokens = null,
    [property: JsonPropertyName("conversationId")] string? ConversationId = null,
    [property: JsonPropertyName("error")] string? Error = null,
    [property: JsonPropertyName("usageSource")] string? UsageSource = null,
    // Live progress + save partition: the chat bridge reports the current tool and
    // the originating save so a stale reply can never overwrite the visible state.
    [property: JsonPropertyName("toolName"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? ToolName = null,
    [property: JsonPropertyName("saveId"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? SaveId = null,
    // Provider usage split (never a fabricated total): input / cache read / cache write / output.
    [property: JsonPropertyName("cacheReadTokens")] long? CacheReadTokens = null,
    [property: JsonPropertyName("cacheWriteTokens")] long? CacheWriteTokens = null,
    [property: JsonPropertyName("modelCalls")] int? ModelCalls = null,
    // A logical player instruction spans provider turns and native jobs.
    // Null retains the legacy per-request terminal semantics.
    [property: JsonPropertyName("commandId"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? CommandId = null,
    [property: JsonPropertyName("commandComplete"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] bool? CommandComplete = null
);

public sealed record ChatCancelPayload(
    [property: JsonPropertyName("requestId")] string? RequestId = null,
    [property: JsonPropertyName("reason")] string Reason = "player_cancelled"
);

public sealed record AutonomyControlPayload(
    [property: JsonPropertyName("requestId")] string RequestId,
    [property: JsonPropertyName("saveId")] string SaveId,
    [property: JsonPropertyName("action")] string Action,
    [property: JsonPropertyName("parameters")] JsonObject? Parameters = null
);

public sealed record AutonomyStatePayload(
    [property: JsonPropertyName("requestId")] string RequestId,
    [property: JsonPropertyName("saveId")] string SaveId,
    [property: JsonPropertyName("mode")] string Mode,
    [property: JsonPropertyName("paused")] bool Paused,
    [property: JsonPropertyName("preferences")] JsonObject? Preferences,
    [property: JsonPropertyName("dailySpend")] int DailySpend,
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("reason")] string? Reason = null,
    // F8 progress: the bridge already sends these; without the fields the menu
    // could not draw the fixed current-action / wait-reason lines.
    [property: JsonPropertyName("preferencesRevision")] int? PreferencesRevision = null,
    [property: JsonPropertyName("decisionEpoch")] int? DecisionEpoch = null,
    [property: JsonPropertyName("gameDate")] string? GameDate = null,
    [property: JsonPropertyName("lastPlanAction")] JsonObject? LastPlanAction = null,
    [property: JsonPropertyName("planWaitReason")] string? PlanWaitReason = null,
    [property: JsonPropertyName("waitingConditions")] List<WaitingConditionPayload>? WaitingConditions = null,
    [property: JsonPropertyName("hasExecutableWork")] bool? HasExecutableWork = null
);

// Same object schema as WorkStore.wait_conditions; never a list of display strings.
public sealed record WaitingConditionPayload(
    [property: JsonPropertyName("taskId")] string? TaskId,
    [property: JsonPropertyName("taskTitle")] string? TaskTitle,
    [property: JsonPropertyName("stepId")] string? StepId,
    [property: JsonPropertyName("operation")] string? Operation,
    [property: JsonPropertyName("waitCondition")] JsonObject? WaitCondition,
    [property: JsonPropertyName("waitDescription")] string? WaitDescription,
    [property: JsonPropertyName("reasonCode")] string? ReasonCode
)
{
    public string DisplayText => $"{Operation ?? TaskTitle ?? "等待"}：{WaitDescription ?? ReasonCode ?? "条件待确认"}";
}

// ---------------------------------------------------------------------------
// §1 Life / companion-day message DTOs
// ---------------------------------------------------------------------------

/// <summary>
/// §1.1 life.chat.submit (C#→Py).
/// Initiates a life-chat exchange with the companion.
/// </summary>
public sealed record LifeChatSubmitPayload(
    [property: JsonPropertyName("requestId")] string RequestId,
    [property: JsonPropertyName("saveId")] string SaveId,
    [property: JsonPropertyName("mode")] string Mode,      // "chat" | "plan"
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("source")] string Source = "life-menu"
);

/// <summary>
/// §1.2 life.chat.reply (Py→C#).
/// Status: "processing" | "queued" | "completed" | "failed".
/// </summary>
public sealed record LifeChatReplyPayload(
    [property: JsonPropertyName("requestId")] string RequestId,
    [property: JsonPropertyName("saveId")] string SaveId,
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("profileRevision")] int ProfileRevision,
    [property: JsonPropertyName("memoryRevision")] int MemoryRevision,
    [property: JsonPropertyName("replyText"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? ReplyText = null,
    [property: JsonPropertyName("queuePosition"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? QueuePosition = null,
    [property: JsonPropertyName("error"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Error = null
);

/// <summary>
/// §1.3 life.profile.get (C#→Py).
/// Requests the current companion profile state.
/// </summary>
public sealed record LifeProfileGetPayload(
    [property: JsonPropertyName("requestId")] string RequestId,
    [property: JsonPropertyName("saveId")] string SaveId
);

/// <summary>
/// Companion profile sub-object within <see cref="LifeProfileStatePayload"/>.
/// Null when the profile has not been configured yet.
/// </summary>
public sealed record CompanionProfileDto(
    [property: JsonPropertyName("onboarded")] bool Onboarded,
    [property: JsonPropertyName("skipped")] bool Skipped,
    [property: JsonPropertyName("playStyle")] string PlayStyle,
    [property: JsonPropertyName("personality")] string Personality,
    [property: JsonPropertyName("careFrequency")] string CareFrequency,
    [property: JsonPropertyName("companionName")] string CompanionName
);

/// <summary>
/// Work-state sub-object within <see cref="LifeProfileStatePayload"/>.
/// Read-only projection of the autonomy + WorkStore state.
/// </summary>
public sealed record CompanionWorkStateDto(
    [property: JsonPropertyName("mode")] string Mode,
    [property: JsonPropertyName("paused")] bool Paused,
    [property: JsonPropertyName("goal"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Goal = null,
    [property: JsonPropertyName("dailySpendLimit"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? DailySpendLimit = null,
    [property: JsonPropertyName("boxPreference"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? BoxPreference = null,
    [property: JsonPropertyName("dailySpend"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? DailySpend = null,
    [property: JsonPropertyName("hasExecutableWork")] bool HasExecutableWork = false,
    [property: JsonPropertyName("lastPlanAction"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? LastPlanAction = null,
    [property: JsonPropertyName("planWaitReason"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? PlanWaitReason = null,
    [property: JsonPropertyName("lastSettledDay"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? LastSettledDay = null,
    [property: JsonPropertyName("activeGoals"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] List<ActiveGoalDto>? ActiveGoals = null,
    [property: JsonPropertyName("recentTodos"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] List<RecentTodoDto>? RecentTodos = null,
    [property: JsonPropertyName("waitingConditions"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] List<string>? WaitingConditions = null
);

/// <summary>Active goal entry within <see cref="CompanionWorkStateDto"/>.</summary>
public sealed record ActiveGoalDto(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("status")] string Status
);

/// <summary>Recent todo entry within <see cref="CompanionWorkStateDto"/>.</summary>
public sealed record RecentTodoDto(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("intent")] string Intent,
    [property: JsonPropertyName("status")] string Status
);

/// <summary>
/// §1.3 life.profile.state (Py→C#).
/// Carries the full companion profile and work projection.
/// </summary>
public sealed record LifeProfileStatePayload(
    [property: JsonPropertyName("requestId")] string RequestId,
    [property: JsonPropertyName("saveId")] string SaveId,
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("profileRevision")] int ProfileRevision,
    [property: JsonPropertyName("work")] CompanionWorkStateDto Work,
    [property: JsonPropertyName("profile"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] CompanionProfileDto? Profile = null,
    [property: JsonPropertyName("reason"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Reason = null
);

/// <summary>
/// §1.4 life.profile.set (C#→Py).
/// Patch-updates the companion profile; responds with life.profile.state.
/// </summary>
public sealed record LifeProfileSetPayload(
    [property: JsonPropertyName("requestId")] string RequestId,
    [property: JsonPropertyName("saveId")] string SaveId,
    [property: JsonPropertyName("expectedRevision")] int ExpectedRevision,
    [property: JsonPropertyName("patch")] LifeProfilePatchDto Patch
);

/// <summary>Partial update to the companion profile (all fields optional).</summary>
public sealed record LifeProfilePatchDto(
    [property: JsonPropertyName("onboarded"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] bool? Onboarded = null,
    [property: JsonPropertyName("skipped"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] bool? Skipped = null,
    [property: JsonPropertyName("playStyle"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? PlayStyle = null,
    [property: JsonPropertyName("personality"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Personality = null,
    [property: JsonPropertyName("careFrequency"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? CareFrequency = null,
    [property: JsonPropertyName("companionName"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? CompanionName = null
);

/// <summary>
/// §1.5 life.memory.list (C#→Py).
/// Requests the memory entry list.
/// </summary>
public sealed record LifeMemoryListPayload(
    [property: JsonPropertyName("requestId")] string RequestId,
    [property: JsonPropertyName("saveId")] string SaveId
);

/// <summary>Single memory entry within <see cref="LifeMemoryStatePayload"/>.</summary>
public sealed record MemoryEntryDto(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("source")] string Source,
    [property: JsonPropertyName("gameDate")] string GameDate,
    [property: JsonPropertyName("createdAt")] string CreatedAt
);

/// <summary>
/// §1.5 life.memory.state (Py→C#).
/// Carries the full memory entry list and current revision.
/// </summary>
public sealed record LifeMemoryStatePayload(
    [property: JsonPropertyName("requestId")] string RequestId,
    [property: JsonPropertyName("saveId")] string SaveId,
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("memoryRevision")] int MemoryRevision,
    [property: JsonPropertyName("entries")] List<MemoryEntryDto> Entries,
    [property: JsonPropertyName("reason"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Reason = null
);

/// <summary>
/// §1.6 life.memory.edit (C#→Py).
/// Add, correct, or delete a memory entry; responds with life.memory.state.
/// </summary>
public sealed record LifeMemoryEditPayload(
    [property: JsonPropertyName("requestId")] string RequestId,
    [property: JsonPropertyName("saveId")] string SaveId,
    [property: JsonPropertyName("expectedRevision")] int ExpectedRevision,
    [property: JsonPropertyName("op")] string Op,  // "add" | "correct" | "delete"
    [property: JsonPropertyName("id"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Id = null,
    [property: JsonPropertyName("kind"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Kind = null,
    [property: JsonPropertyName("text"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Text = null
);

/// <summary>
/// §1.7 life.care (Py→C#, single-direction push).
/// Companion-initiated care hint; no response required.
/// </summary>
public sealed record LifeCarePayload(
    [property: JsonPropertyName("saveId")] string SaveId,
    [property: JsonPropertyName("eventKey")] string EventKey,
    [property: JsonPropertyName("gameDate")] string GameDate,
    [property: JsonPropertyName("kind")] string Kind,   // "morning" | "work-done" | "evening" | "milestone"
    [property: JsonPropertyName("text")] string Text
);

/// <summary>
/// §2.1 life.milestones.get (C#→Py).
/// Requests the milestone node snapshot for the current save.
/// </summary>
public sealed record LifeMilestonesGetPayload(
    [property: JsonPropertyName("requestId")] string RequestId,
    [property: JsonPropertyName("saveId")] string SaveId
);

/// <summary>
/// §2.2 life.milestones.state (Py→C#, reply or proactive push with requestId "").
/// Status: "ok" | "failed". Nodes are runtime-ordered (adopted → suggested by
/// daysUntil → deferred → completed/missed most-recent-first), capped at 12.
/// </summary>
public sealed record LifeMilestonesStatePayload(
    [property: JsonPropertyName("requestId")] string RequestId,
    [property: JsonPropertyName("saveId")] string SaveId,
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("gameDate"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? GameDate = null,
    [property: JsonPropertyName("nodes"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] List<MilestoneNodeDto>? Nodes = null,
    [property: JsonPropertyName("error"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Error = null
);

/// <summary>Single preparation item within <see cref="MilestoneNodeDto"/>.</summary>
public sealed record MilestonePrepItemDto(
    [property: JsonPropertyName("key")] string Key,
    [property: JsonPropertyName("label")] string Label,
    [property: JsonPropertyName("support")] string Support,   // "manual" | "capability"
    [property: JsonPropertyName("status")] string Status,     // "pending" | "done" | "unknown"
    [property: JsonPropertyName("note"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Note = null
);

/// <summary>Single milestone node within <see cref="LifeMilestonesStatePayload"/>.</summary>
public sealed record MilestoneNodeDto(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("title")] string Title,
    [property: JsonPropertyName("status")] string Status,   // "suggested" | "adopted" | "deferred" | "completed" | "missed"
    [property: JsonPropertyName("verification"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Verification = null,
    [property: JsonPropertyName("targetDate"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? TargetDate = null,
    [property: JsonPropertyName("daysUntil"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? DaysUntil = null,
    [property: JsonPropertyName("summary"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Summary = null,
    [property: JsonPropertyName("sourceUrl"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? SourceUrl = null,
    [property: JsonPropertyName("prepItems"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] List<MilestonePrepItemDto>? PrepItems = null,
    [property: JsonPropertyName("reservedFunds"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? ReservedFunds = null,
    [property: JsonPropertyName("plannedCount"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? PlannedCount = null,
    [property: JsonPropertyName("termsNote"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? TermsNote = null,
    [property: JsonPropertyName("updatedAt"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] double? UpdatedAt = null
);
