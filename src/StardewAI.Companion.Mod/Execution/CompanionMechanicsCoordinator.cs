using Microsoft.Xna.Framework;
using StardewValley;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>
/// Top-level coordinator implementing ITransportHandler to bridge the production
/// Transport subsystem with the independent Mechanics Actor and tick-driven execution
/// state machines (water-zone, harvest-zone, deposit-chest, organize-chest).
/// </summary>
public sealed class CompanionMechanicsCoordinator : ITransportHandler
{
    private const int MaxReportedMatureCrops = 64;
    private const int MaxReportedChests = 16;
    private const int MaxReportedMachines = 48;
    private const int MaxReportedAnimalBuildings = 12;
    private const int MaxReportedAnimals = 60;
    private const int MaxReportedRefillTiles = 16;
    private const int MaxReportedGroundItems = 48;
    private const int MaxReportedChoppableTrees = 24;
    private const int MaxReportedPlayerItems = 40;

    private readonly IFarmerActor _actor;
    private readonly CompanionAvatar? _avatar;
    private readonly IWorldObserver _observer;
    private readonly SameMapNavigator _navigator;
    private readonly IWateringCanAdapter _adapter;
    private readonly WaterZoneStateMachine _stateMachine;
    private readonly HarvestZoneStateMachine? _harvestMachine;
    private readonly ChestActionStateMachine? _chestMachine;
    private readonly IHoeAdapter? _hoeAdapter;
    private readonly IPlantAdapter? _plantAdapter;
    private readonly HoeZoneStateMachine? _hoeMachine;
    private readonly PlantZoneStateMachine? _plantMachine;
    private readonly IShippingAdapter? _shippingAdapter;
    private readonly ShippingStateMachine? _shippingMachine;
    private readonly IPurchaseAdapter? _purchaseAdapter;
    private readonly PurchaseStateMachine? _purchaseMachine;
    private readonly INativeActionAdapter? _nativeActionAdapter;
    private readonly NativeActionStateMachine? _nativeActionMachine;
    private readonly IWorldMapGraph _mapGraph;
    private readonly NavigationStateMachine _navigationMachine;
    private readonly object _taskLock = new();

    private ITransportServer? _transportServer;
    private EnvelopeDto? _currentExecuteEnvelope;
    private WaterZoneResult? _lastResult;
    private SkillResultPayload? _lastSkillResultPayload;

    private readonly HashSet<string> _cancelledTaskIds = new();

    public IFarmerActor Actor => _actor;
    public CompanionAvatar? Avatar => _avatar;
    public WaterZoneStateMachine StateMachine => _stateMachine;
    public HarvestZoneStateMachine? HarvestMachine => _harvestMachine;
    public ChestActionStateMachine? ChestMachine => _chestMachine;
    public HoeZoneStateMachine? HoeMachine => _hoeMachine;
    public PlantZoneStateMachine? PlantMachine => _plantMachine;
    public ShippingStateMachine? ShippingMachine => _shippingMachine;
    public PurchaseStateMachine? PurchaseMachine => _purchaseMachine;
    public NativeActionStateMachine? NativeActionMachine => _nativeActionMachine;
    public NavigationStateMachine NavigationMachine => _navigationMachine;
    public IWorldMapGraph MapGraph => _mapGraph;
    public WaterZoneResult? LastResult => _lastResult;
    public SkillResultPayload? LastSkillResultPayload => _lastSkillResultPayload;

    /// <summary>
    /// The currently executing state machine (any skill), or null when idle.
    /// </summary>
    public ISkillExecutionMachine? ActiveMachine
    {
        get
        {
            if (_stateMachine.IsExecuting) return _stateMachine;
            if (_harvestMachine?.IsExecuting == true) return _harvestMachine;
            if (_chestMachine?.IsExecuting == true) return _chestMachine;
            if (_hoeMachine?.IsExecuting == true) return _hoeMachine;
            if (_plantMachine?.IsExecuting == true) return _plantMachine;
            if (_shippingMachine?.IsExecuting == true) return _shippingMachine;
            if (_purchaseMachine?.IsExecuting == true) return _purchaseMachine;
            if (_nativeActionMachine?.IsExecuting == true) return _nativeActionMachine;
            if (_navigationMachine?.IsExecuting == true) return _navigationMachine;
            return null;
        }
    }

    /// <summary>
    /// Raised after ANY skill execution reaches a terminal state (used for actor-state persistence).
    /// </summary>
    public event Action? OnExecutionCompleted;

    public event Action<string>? OnNotification;

    /// <summary>
    /// Structured progress from the native action state machine (Q5): action, real
    /// phase, completed/total and reasonCode. Never a fabricated percentage.
    /// </summary>
    public event Action<NativeActionProgress>? OnNativeActionProgress;

    /// <summary>Last structured native progress observed, or null when none ran.</summary>
    public NativeActionProgress? LastNativeProgress => _nativeActionMachine?.LastProgress;

    public CompanionMechanicsCoordinator(
        IFarmerActor actor,
        IWorldObserver observer,
        SameMapNavigator navigator,
        IWateringCanAdapter adapter,
        CompanionAvatar? avatar = null,
        Action<string, StardewModdingAPI.LogLevel>? log = null,
        IHarvestAdapter? harvestAdapter = null,
        IChestAdapter? chestAdapter = null,
        IHoeAdapter? hoeAdapter = null,
        IPlantAdapter? plantAdapter = null,
        IShippingAdapter? shippingAdapter = null,
        IWorldMapGraph? mapGraph = null,
        IPurchaseAdapter? purchaseAdapter = null,
        INativeActionAdapter? nativeActionAdapter = null)
    {
        _actor = actor ?? throw new ArgumentNullException(nameof(actor));
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _navigator = navigator ?? throw new ArgumentNullException(nameof(navigator));
        _adapter = adapter ?? throw new ArgumentNullException(nameof(adapter));
        _avatar = avatar;
        _log = log;

        _stateMachine = new WaterZoneStateMachine(_actor, _observer, _navigator, _adapter, _avatar, log);
        _stateMachine.OnCompleted += HandleWaterZoneCompleted;
        _stateMachine.OnNotification += msg => OnNotification?.Invoke(msg);

        if (harvestAdapter is not null)
        {
            _harvestMachine = new HarvestZoneStateMachine(_actor, _observer, _navigator, harvestAdapter, _avatar, log);
            _harvestMachine.OnCompleted += HandleHarvestZoneCompleted;
            _harvestMachine.OnNotification += msg => OnNotification?.Invoke(msg);
        }

        if (chestAdapter is not null)
        {
            _chestMachine = new ChestActionStateMachine(_actor, _observer, _navigator, chestAdapter, _avatar, log);
            _chestMachine.OnCompleted += HandleChestActionCompleted;
            _chestMachine.OnNotification += msg => OnNotification?.Invoke(msg);
        }

        if (hoeAdapter is not null)
        {
            _hoeAdapter = hoeAdapter;
            _hoeMachine = new HoeZoneStateMachine(_actor, _observer, _navigator, hoeAdapter, _avatar, log);
            _hoeMachine.OnCompleted += HandleHoeZoneCompleted;
            _hoeMachine.OnNotification += msg => OnNotification?.Invoke(msg);
        }

        if (plantAdapter is not null)
        {
            _plantAdapter = plantAdapter;
            _plantMachine = new PlantZoneStateMachine(_actor, _observer, _navigator, plantAdapter, _avatar, log);
            _plantMachine.OnCompleted += HandlePlantZoneCompleted;
            _plantMachine.OnNotification += msg => OnNotification?.Invoke(msg);
        }

        if (shippingAdapter is not null)
        {
            _shippingAdapter = shippingAdapter;
            _shippingMachine = new ShippingStateMachine(_actor, _observer, _navigator, shippingAdapter, _avatar, log);
            _shippingMachine.OnCompleted += HandleShippingCompleted;
            _shippingMachine.OnNotification += msg => OnNotification?.Invoke(msg);
        }

        if (purchaseAdapter is not null)
        {
            _purchaseAdapter = purchaseAdapter;
            _purchaseMachine = new PurchaseStateMachine(_actor, _observer, purchaseAdapter, _avatar, log);
            _purchaseMachine.OnCompleted += HandlePurchaseCompleted;
            _purchaseMachine.OnNotification += msg => OnNotification?.Invoke(msg);
        }

        if (nativeActionAdapter is not null)
        {
            _nativeActionAdapter = nativeActionAdapter;
            _nativeActionMachine = new NativeActionStateMachine(_actor, _observer, _navigator, nativeActionAdapter, _avatar, log);
            _nativeActionMachine.OnCompleted += HandleNativeActionCompleted;
            _nativeActionMachine.OnNotification += msg => OnNotification?.Invoke(msg);
            _nativeActionMachine.OnProgress += progress => OnNativeActionProgress?.Invoke(progress);
        }

        _mapGraph = mapGraph ?? new WorldMapGraph(null, log);
        _navigationMachine = new NavigationStateMachine(_actor, _observer, _navigator, _mapGraph, _avatar, log);
        _navigationMachine.OnCompleted += HandleNavigationCompleted;
        _navigationMachine.OnNotification += msg => OnNotification?.Invoke(msg);
    }

    private readonly Action<string, StardewModdingAPI.LogLevel>? _log;

    private void Log(string message, StardewModdingAPI.LogLevel level = StardewModdingAPI.LogLevel.Info) => _log?.Invoke(message, level);

    public void SetTransportServer(ITransportServer server)
    {
        _transportServer = server ?? throw new ArgumentNullException(nameof(server));
    }

    /// <summary>
    /// Drives every execution state machine by one tick (each no-ops when idle).
    /// Called every game update tick from SMAPI UpdateTicked.
    /// </summary>
    public void Update(GameTime? time, long tickCount)
    {
        _stateMachine.Update(time, tickCount);
        _harvestMachine?.Update(time, tickCount);
        _chestMachine?.Update(time, tickCount);
        _hoeMachine?.Update(time, tickCount);
        _plantMachine?.Update(time, tickCount);
        _shippingMachine?.Update(time, tickCount);
        _purchaseMachine?.Update(time, tickCount);
        _nativeActionMachine?.Update(time, tickCount);
        _navigationMachine.Update(time, tickCount);
    }

    /// <summary>
    /// Core entry point to start a water-zone task on the actor and state machine.
    /// Shared by both WebSocket skill.execute and SMAPI console ai_water commands.
    /// </summary>
    public bool TryStartWaterZone(
        WaterZoneRequest request,
        EnvelopeDto? envelope,
        out WaterZoneResult? earlyResult,
        out string? rejectReason)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_taskLock)
        {
            // 0. Check cancellation tombstone (cancellation-before-action)
            if (_cancelledTaskIds.Contains(request.TaskId) || _cancelledTaskIds.Contains(request.CommandId))
            {
                rejectReason = "Task was cancelled prior to execution.";
                earlyResult = null;
                return false;
            }

            // 1. Enforce one active task invariant
            if (!TryCheckIdle(out rejectReason))
            {
                earlyResult = null;
                return false;
            }

            _currentExecuteEnvelope = envelope;

            // 2. Start tick-driven state machine
            if (!_stateMachine.Start(request, out earlyResult))
            {
                rejectReason = earlyResult?.ErrorMessage ?? "State machine start rejected.";
                Log($"water-zone rejected (commandId={request.CommandId}): {rejectReason}", StardewModdingAPI.LogLevel.Warn);
                return false;
            }

            Log($"water-zone accepted (commandId={request.CommandId}, taskId={request.TaskId}, tiles={request.TargetTiles.Count}).");
            rejectReason = null;
            return true;
        }
    }

    /// <summary>
    /// Core entry point to start a harvest-zone task.
    /// </summary>
    public bool TryStartHarvestZone(
        HarvestZoneRequest request,
        EnvelopeDto? envelope,
        out HarvestZoneResult? earlyResult,
        out string? rejectReason)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_taskLock)
        {
            if (_harvestMachine is null)
            {
                rejectReason = "Harvest skill is not available (no harvest adapter configured).";
                earlyResult = null;
                return false;
            }

            if (_cancelledTaskIds.Contains(request.TaskId) || _cancelledTaskIds.Contains(request.CommandId))
            {
                rejectReason = "Task was cancelled prior to execution.";
                earlyResult = null;
                return false;
            }

            if (!TryCheckIdle(out rejectReason))
            {
                earlyResult = null;
                return false;
            }

            _currentExecuteEnvelope = envelope;

            if (!_harvestMachine.Start(request, out earlyResult))
            {
                rejectReason = earlyResult?.ErrorMessage ?? "State machine start rejected.";
                Log($"harvest-zone rejected (commandId={request.CommandId}): {rejectReason}", StardewModdingAPI.LogLevel.Warn);
                return false;
            }

            Log($"harvest-zone accepted (commandId={request.CommandId}, taskId={request.TaskId}, tiles={request.TargetTiles.Count}).");
            rejectReason = null;
            return true;
        }
    }

    /// <summary>
    /// Core entry point to start a deposit-chest or organize-chest task.
    /// </summary>
    public bool TryStartChestAction(
        ChestActionRequest request,
        EnvelopeDto? envelope,
        out ChestActionResult? earlyResult,
        out string? rejectReason)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_taskLock)
        {
            if (_chestMachine is null)
            {
                rejectReason = "Chest skills are not available (no chest adapter configured).";
                earlyResult = null;
                return false;
            }

            if (_cancelledTaskIds.Contains(request.TaskId) || _cancelledTaskIds.Contains(request.CommandId))
            {
                rejectReason = "Task was cancelled prior to execution.";
                earlyResult = null;
                return false;
            }

            if (!TryCheckIdle(out rejectReason))
            {
                earlyResult = null;
                return false;
            }

            _currentExecuteEnvelope = envelope;

            if (!_chestMachine.Start(request, out earlyResult))
            {
                rejectReason = earlyResult?.ErrorMessage ?? "State machine start rejected.";
                Log($"{request.SkillId} rejected (commandId={request.CommandId}): {rejectReason}", StardewModdingAPI.LogLevel.Warn);
                return false;
            }

            Log($"{request.SkillId} accepted (commandId={request.CommandId}, taskId={request.TaskId}, chest=({request.ChestTile.X},{request.ChestTile.Y})).");
            rejectReason = null;
            return true;
        }
    }

    /// <summary>
    /// Core entry point to start a hoe-tiles task.
    /// </summary>
    public bool TryStartHoeZone(
        HoeZoneRequest request,
        EnvelopeDto? envelope,
        out HoeZoneResult? earlyResult,
        out string? rejectReason)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_taskLock)
        {
            if (_hoeMachine is null)
            {
                rejectReason = "Hoe skill is not available (no hoe adapter configured).";
                earlyResult = null;
                return false;
            }

            if (_cancelledTaskIds.Contains(request.TaskId) || _cancelledTaskIds.Contains(request.CommandId))
            {
                rejectReason = "Task was cancelled prior to execution.";
                earlyResult = null;
                return false;
            }

            if (!TryCheckIdle(out rejectReason))
            {
                earlyResult = null;
                return false;
            }

            _currentExecuteEnvelope = envelope;

            if (!_hoeMachine.Start(request, out earlyResult))
            {
                rejectReason = earlyResult?.ErrorMessage ?? "State machine start rejected.";
                Log($"hoe-tiles rejected (commandId={request.CommandId}): {rejectReason}", StardewModdingAPI.LogLevel.Warn);
                return false;
            }

            Log($"hoe-tiles accepted (commandId={request.CommandId}, taskId={request.TaskId}, tiles={request.TargetTiles.Count}).");
            rejectReason = null;
            return true;
        }
    }

    /// <summary>
    /// Core entry point to start a plant-seeds task.
    /// </summary>
    public bool TryStartPlantZone(
        PlantZoneRequest request,
        EnvelopeDto? envelope,
        out PlantZoneResult? earlyResult,
        out string? rejectReason)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_taskLock)
        {
            if (_plantMachine is null)
            {
                rejectReason = "Plant skill is not available (no plant adapter configured).";
                earlyResult = null;
                return false;
            }

            if (_cancelledTaskIds.Contains(request.TaskId) || _cancelledTaskIds.Contains(request.CommandId))
            {
                rejectReason = "Task was cancelled prior to execution.";
                earlyResult = null;
                return false;
            }

            if (!TryCheckIdle(out rejectReason))
            {
                earlyResult = null;
                return false;
            }

            _currentExecuteEnvelope = envelope;

            if (!_plantMachine.Start(request, out earlyResult))
            {
                rejectReason = earlyResult?.ErrorMessage ?? "State machine start rejected.";
                Log($"plant-seeds rejected (commandId={request.CommandId}): {rejectReason}", StardewModdingAPI.LogLevel.Warn);
                return false;
            }

            Log($"plant-seeds accepted (commandId={request.CommandId}, taskId={request.TaskId}, seed={request.SeedItemId}, tiles={request.TargetTiles.Count}).");
            rejectReason = null;
            return true;
        }
    }

    public bool TryStartShipping(
        ShippingRequest request,
        EnvelopeDto? envelope,
        out ShippingResult? earlyResult,
        out string? rejectReason)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_taskLock)
        {
            if (_cancelledTaskIds.Contains(request.TaskId) || _cancelledTaskIds.Contains(request.CommandId))
            {
                rejectReason = $"Task '{request.TaskId}' was cancelled before execution started.";
                earlyResult = null;
                return false;
            }

            if (_shippingMachine is null)
            {
                rejectReason = "Shipping skill is not available (no shipping adapter configured).";
                earlyResult = null;
                return false;
            }

            if (!TryCheckIdle(out rejectReason))
            {
                earlyResult = null;
                return false;
            }

            _currentExecuteEnvelope = envelope;

            if (!_shippingMachine.Start(request, out earlyResult))
            {
                rejectReason = earlyResult?.ErrorMessage ?? "State machine start rejected.";
                Log($"ship-items rejected (commandId={request.CommandId}): {rejectReason}", StardewModdingAPI.LogLevel.Warn);
                return false;
            }

            Log($"ship-items accepted (commandId={request.CommandId}, taskId={request.TaskId}, items={request.Items.Count}).");
            rejectReason = null;
            return true;
        }
    }

    public bool TryStartPurchase(
        PurchaseRequest request,
        EnvelopeDto? envelope,
        out PurchaseResult? earlyResult,
        out string? rejectReason)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_taskLock)
        {
            if (_cancelledTaskIds.Contains(request.TaskId) || _cancelledTaskIds.Contains(request.CommandId))
            {
                rejectReason = $"Task '{request.TaskId}' was cancelled before execution started.";
                earlyResult = null;
                return false;
            }

            if (_purchaseMachine is null)
            {
                rejectReason = "Purchase skill is not available (no purchase adapter configured).";
                earlyResult = null;
                return false;
            }

            if (!TryCheckIdle(out rejectReason))
            {
                earlyResult = null;
                return false;
            }

            _currentExecuteEnvelope = envelope;

            if (!_purchaseMachine.Start(request, out earlyResult))
            {
                rejectReason = earlyResult?.ErrorMessage ?? "State machine start rejected.";
                Log($"purchase-items rejected (commandId={request.CommandId}): {rejectReason}", StardewModdingAPI.LogLevel.Warn);
                return false;
            }

            Log($"purchase-items accepted (commandId={request.CommandId}, taskId={request.TaskId}, items={request.Items.Count}, budget={request.BudgetLimit}).");
            rejectReason = null;
            return true;
        }
    }

    public bool TryStartNavigation(
        NavigationRequest request,
        EnvelopeDto? envelope,
        out NavigationResult? earlyResult,
        out string? rejectReason)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_taskLock)
        {
            if (_cancelledTaskIds.Contains(request.TaskId) || _cancelledTaskIds.Contains(request.CommandId))
            {
                rejectReason = $"Task '{request.TaskId}' was cancelled before execution started.";
                earlyResult = null;
                return false;
            }

            if (!TryCheckIdle(out rejectReason))
            {
                earlyResult = null;
                return false;
            }

            _currentExecuteEnvelope = envelope;

            if (!_navigationMachine.Start(request, out earlyResult))
            {
                rejectReason = earlyResult?.ErrorMessage ?? "State machine start rejected.";
                Log($"navigate-to rejected (commandId={request.CommandId}): {rejectReason}", StardewModdingAPI.LogLevel.Warn);
                return false;
            }

            Log($"navigate-to accepted (commandId={request.CommandId}, taskId={request.TaskId}, target={request.LocationId}:{request.TargetTile}).");
            rejectReason = null;
            return true;
        }
    }

    private bool TryCheckIdle(out string? rejectReason)
    {
        if (_actor.ActiveTaskId is not null || _stateMachine.IsExecuting ||
            _harvestMachine?.IsExecuting == true || _chestMachine?.IsExecuting == true ||
            _hoeMachine?.IsExecuting == true || _plantMachine?.IsExecuting == true ||
            _shippingMachine?.IsExecuting == true || _purchaseMachine?.IsExecuting == true ||
            _nativeActionMachine?.IsExecuting == true ||
            _navigationMachine.IsExecuting)
        {
            rejectReason = $"Actor is busy executing active task '{_actor.ActiveTaskId ?? ActiveMachine?.ActiveTaskId}'.";
            return false;
        }

        rejectReason = null;
        return true;
    }

    public bool TryAcceptSkillExecute(
        SkillExecutePayload payload,
        EnvelopeDto envelope,
        out SkillResultPayload? rejectResult)
    {
        ArgumentNullException.ThrowIfNull(payload);
        ArgumentNullException.ThrowIfNull(envelope);

        lock (_taskLock)
        {
            string skillId = payload.SkillId ?? "";
            bool isWater = string.Equals(skillId, "water-zone", StringComparison.OrdinalIgnoreCase);
            bool isHarvest = string.Equals(skillId, "harvest-zone", StringComparison.OrdinalIgnoreCase);
            bool isDeposit = string.Equals(skillId, "deposit-chest", StringComparison.OrdinalIgnoreCase);
            bool isOrganize = string.Equals(skillId, "organize-chest", StringComparison.OrdinalIgnoreCase);
            bool isWithdraw = string.Equals(skillId, "withdraw-chest", StringComparison.OrdinalIgnoreCase);
            bool isHoe = string.Equals(skillId, "hoe-tiles", StringComparison.OrdinalIgnoreCase);
            bool isPlant = string.Equals(skillId, "plant-seeds", StringComparison.OrdinalIgnoreCase);
            bool isShip = string.Equals(skillId, "ship-items", StringComparison.OrdinalIgnoreCase);
            bool isNavigate = string.Equals(skillId, "navigate-to", StringComparison.OrdinalIgnoreCase);
            bool isPurchase = string.Equals(skillId, "purchase-items", StringComparison.OrdinalIgnoreCase);

            NativeActionKind? nativeKind = skillId.ToLowerInvariant() switch
            {
                "refill-watering-can" => NativeActionKind.RefillWateringCan,
                "apply-fertilizer" => NativeActionKind.ApplyFertilizer,
                "clear-debris" => NativeActionKind.ClearDebris,
                "pickup-items" => NativeActionKind.PickupItems,
                "insert-machine" => NativeActionKind.InsertMachine,
                "collect-machine" => NativeActionKind.CollectMachine,
                "pet-animal" => NativeActionKind.PetAnimal,
                "feed-animals" => NativeActionKind.FeedAnimals,
                "toggle-animal-door" => NativeActionKind.ToggleAnimalDoor,
                "collect-animal-produce" => NativeActionKind.CollectAnimalProduce,
                "chop-tree" => NativeActionKind.ChopTree,
                _ => null
            };

            // 1. Validate skill identifier
            if (!isWater && !isHarvest && !isDeposit && !isOrganize && !isWithdraw && !isHoe && !isPlant && !isShip && !isNavigate && !isPurchase && nativeKind is null)
            {
                rejectResult = BuildRejection(payload, "UNSUPPORTED_SKILL",
                    $"Skill '{payload.SkillId}' is not supported.", retryable: false);
                return false;
            }

            // 2. Parameter shape validation (defense-in-depth; transport validates first)
            if (payload.Parameters is null || string.IsNullOrWhiteSpace(payload.Parameters.LocationId))
            {
                rejectResult = BuildRejection(payload, "INVALID_PARAMETERS", "parameters.locationId is required.", retryable: false);
                return false;
            }

            if (isNavigate && payload.Parameters.Tile is null)
            {
                rejectResult = BuildRejection(payload, "INVALID_PARAMETERS", "parameters.tile is required for navigate-to.", retryable: false);
                return false;
            }

            if (isPurchase && (payload.Parameters.Items is null || payload.Parameters.Items.Count == 0))
            {
                rejectResult = BuildRejection(payload, "INVALID_PARAMETERS", "parameters.items is required and must not be empty for purchase-items.", retryable: false);
                return false;
            }

            if (isPurchase && (payload.Parameters.EffectiveBudgetLimit is null || payload.Parameters.EffectiveBudgetLimit.Value <= 0))
            {
                rejectResult = BuildRejection(payload, "INVALID_PARAMETERS", "parameters.budget_limit is required and must be a positive integer.", retryable: false);
                return false;
            }

            if ((isWater || isHarvest || isHoe || isPlant) && (payload.Parameters.Tiles is null || payload.Parameters.Tiles.Count == 0))
            {
                rejectResult = BuildRejection(payload, "INVALID_PARAMETERS", "parameters.tiles must contain at least one tile.", retryable: false);
                return false;
            }

            if (isPlant && string.IsNullOrWhiteSpace(payload.Parameters.SeedItemId))
            {
                rejectResult = BuildRejection(payload, "INVALID_PARAMETERS", "parameters.seedItemId is required for plant-seeds.", retryable: false);
                return false;
            }

            if ((isDeposit || isOrganize) && payload.Parameters.ChestTile is null)
            {
                rejectResult = BuildRejection(payload, "INVALID_PARAMETERS", "parameters.chestTile is required for chest skills.", retryable: false);
                return false;
            }

            if (isShip && (payload.Parameters.Items is null || payload.Parameters.Items.Count == 0))
            {
                rejectResult = BuildRejection(payload, "INVALID_PARAMETERS", "parameters.items must contain at least one item.", retryable: false);
                return false;
            }

            if (isShip && payload.Parameters.Items!.Any(i => string.IsNullOrWhiteSpace(i.ItemId) || i.Count < 1))
            {
                rejectResult = BuildRejection(payload, "INVALID_PARAMETERS", "parameters.items entries must specify a non-empty itemId and count >= 1.", retryable: false);
                return false;
            }

            if (nativeKind is not null)
            {
                bool needsTiles = nativeKind is NativeActionKind.RefillWateringCan
                    or NativeActionKind.ApplyFertilizer or NativeActionKind.ClearDebris
                    or NativeActionKind.PickupItems or NativeActionKind.CollectMachine
                    or NativeActionKind.PetAnimal or NativeActionKind.CollectAnimalProduce
                    or NativeActionKind.ToggleAnimalDoor or NativeActionKind.ChopTree;

                if (needsTiles && (payload.Parameters.Tiles is null || payload.Parameters.Tiles.Count == 0))
                {
                    rejectResult = BuildRejection(payload, "INVALID_PARAMETERS",
                        $"parameters.tiles must contain at least one tile for {payload.SkillId}.", retryable: false);
                    return false;
                }

                if (nativeKind == NativeActionKind.ApplyFertilizer && string.IsNullOrWhiteSpace(payload.Parameters.FertilizerItemId))
                {
                    rejectResult = BuildRejection(payload, "INVALID_PARAMETERS",
                        "parameters.fertilizerItemId is required for apply-fertilizer.", retryable: false);
                    return false;
                }

                if (nativeKind == NativeActionKind.InsertMachine &&
                    (payload.Parameters.Tile is null || string.IsNullOrWhiteSpace(payload.Parameters.ItemId)))
                {
                    rejectResult = BuildRejection(payload, "INVALID_PARAMETERS",
                        "parameters.tile and parameters.itemId are required for insert-machine.", retryable: false);
                    return false;
                }

                if (nativeKind is NativeActionKind.PetAnimal or NativeActionKind.CollectAnimalProduce &&
                    string.IsNullOrWhiteSpace(payload.Parameters.AnimalName))
                {
                    rejectResult = BuildRejection(payload, "INVALID_PARAMETERS",
                        $"parameters.animalName is required for {payload.SkillId}.", retryable: false);
                    return false;
                }

                if (nativeKind == NativeActionKind.FeedAnimals && string.IsNullOrWhiteSpace(payload.Parameters.BuildingName))
                {
                    rejectResult = BuildRejection(payload, "INVALID_PARAMETERS",
                        "parameters.buildingName (the animal building interior name) is required for feed-animals.", retryable: false);
                    return false;
                }
            }

            // 3. Dispatch to the matching execution path
            bool accepted;
            SkillResultPayload? earlyPayload = null;
            string? rejectReason = null;

            if (isWater)
            {
                var tileCoords = payload.Parameters.Tiles!
                    .Select(t => new TileCoordinate(t.X, t.Y))
                    .ToList();

                var request = new WaterZoneRequest(
                    CommandId: payload.CommandId,
                    TaskId: payload.TaskId,
                    LocationId: payload.Parameters.LocationId,
                    TargetTiles: tileCoords,
                    MaxStamina: payload.Budgets.MaxStamina,
                    MaxWater: payload.Budgets.MaxWater,
                    MaxGameMinutes: payload.Budgets.MaxGameMinutes,
                    CancelPolicy: payload.CancelPolicy,
                    IdempotencyKey: envelope.IdempotencyKey,
                    ExpectedWorldRevision: payload.ExpectedWorldRevision
                );

                accepted = TryStartWaterZone(request, envelope, out var earlyResult, out rejectReason);
                earlyPayload = earlyResult?.ToTransportPayload();
            }
            else if (isHarvest)
            {
                var tileCoords = payload.Parameters.Tiles!
                    .Select(t => new TileCoordinate(t.X, t.Y))
                    .ToList();

                var request = new HarvestZoneRequest(
                    CommandId: payload.CommandId,
                    TaskId: payload.TaskId,
                    LocationId: payload.Parameters.LocationId,
                    TargetTiles: tileCoords,
                    MaxStamina: payload.Budgets.MaxStamina,
                    MaxWater: payload.Budgets.MaxWater,
                    MaxGameMinutes: payload.Budgets.MaxGameMinutes,
                    CancelPolicy: payload.CancelPolicy,
                    IdempotencyKey: envelope.IdempotencyKey,
                    ExpectedWorldRevision: payload.ExpectedWorldRevision
                );

                accepted = TryStartHarvestZone(request, envelope, out var earlyResult, out rejectReason);
                earlyPayload = earlyResult?.ToTransportPayload();
            }
            else if (isHoe)
            {
                var tileCoords = payload.Parameters.Tiles!
                    .Select(t => new TileCoordinate(t.X, t.Y))
                    .ToList();

                var request = new HoeZoneRequest(
                    CommandId: payload.CommandId,
                    TaskId: payload.TaskId,
                    LocationId: payload.Parameters.LocationId,
                    TargetTiles: tileCoords,
                    MaxStamina: payload.Budgets.MaxStamina,
                    MaxWater: payload.Budgets.MaxWater,
                    MaxGameMinutes: payload.Budgets.MaxGameMinutes,
                    CancelPolicy: payload.CancelPolicy,
                    IdempotencyKey: envelope.IdempotencyKey,
                    ExpectedWorldRevision: payload.ExpectedWorldRevision
                );

                accepted = TryStartHoeZone(request, envelope, out var earlyResult, out rejectReason);
                earlyPayload = earlyResult?.ToTransportPayload();
            }
            else if (isPlant)
            {
                var tileCoords = payload.Parameters.Tiles!
                    .Select(t => new TileCoordinate(t.X, t.Y))
                    .ToList();

                var request = new PlantZoneRequest(
                    CommandId: payload.CommandId,
                    TaskId: payload.TaskId,
                    LocationId: payload.Parameters.LocationId,
                    SeedItemId: payload.Parameters.SeedItemId!,
                    TargetTiles: tileCoords,
                    MaxStamina: payload.Budgets.MaxStamina,
                    MaxWater: payload.Budgets.MaxWater,
                    MaxGameMinutes: payload.Budgets.MaxGameMinutes,
                    CancelPolicy: payload.CancelPolicy,
                    IdempotencyKey: envelope.IdempotencyKey,
                    ExpectedWorldRevision: payload.ExpectedWorldRevision
                );

                accepted = TryStartPlantZone(request, envelope, out var earlyResult, out rejectReason);
                earlyPayload = earlyResult?.ToTransportPayload();
            }
            else if (isShip)
            {
                var request = new ShippingRequest(
                    CommandId: payload.CommandId,
                    TaskId: payload.TaskId,
                    LocationId: payload.Parameters.LocationId,
                    Items: payload.Parameters.Items!,
                    MaxStamina: payload.Budgets.MaxStamina,
                    MaxWater: payload.Budgets.MaxWater,
                    MaxGameMinutes: payload.Budgets.MaxGameMinutes,
                    CancelPolicy: payload.CancelPolicy,
                    IdempotencyKey: envelope.IdempotencyKey,
                    ExpectedWorldRevision: payload.ExpectedWorldRevision
                );

                accepted = TryStartShipping(request, envelope, out var earlyResult, out rejectReason);
                earlyPayload = earlyResult?.ToTransportPayload();
            }
            else if (isNavigate)
            {
                var targetTile = payload.Parameters.Tile!;
                var request = new NavigationRequest(
                    CommandId: payload.CommandId,
                    TaskId: payload.TaskId,
                    LocationId: payload.Parameters.LocationId,
                    TargetTile: new TileCoordinate(targetTile.X, targetTile.Y),
                    MaxGameMinutes: payload.Budgets?.MaxGameMinutes > 0 ? payload.Budgets.MaxGameMinutes : 120,
                    MaxStamina: payload.Budgets?.MaxStamina > 0 ? payload.Budgets.MaxStamina : 50f,
                    MaxWater: payload.Budgets?.MaxWater ?? 0,
                    CancelPolicy: payload.CancelPolicy ?? "safe-point"
                );

                accepted = TryStartNavigation(request, envelope, out var earlyResult, out rejectReason);
                earlyPayload = earlyResult?.ToTransportPayload();
            }
            else if (isPurchase)
            {
                int budgetLimit = payload.Parameters.EffectiveBudgetLimit ?? 0;
                string shopId = !string.IsNullOrWhiteSpace(payload.Parameters.ShopId) ? payload.Parameters.ShopId : "SeedShop";
                var items = payload.Parameters.Items?.Select(i => new PurchaseItemRequest(i.ItemId, i.Count)).ToList()
                    ?? new List<PurchaseItemRequest>();

                var request = new PurchaseRequest(
                    CommandId: payload.CommandId,
                    TaskId: payload.TaskId,
                    ShopId: shopId,
                    LocationId: payload.Parameters.LocationId,
                    Items: items,
                    BudgetLimit: budgetLimit,
                    MaxStamina: payload.Budgets?.MaxStamina > 0 ? payload.Budgets.MaxStamina : 50f,
                    MaxWater: payload.Budgets?.MaxWater ?? 0,
                    MaxGameMinutes: payload.Budgets?.MaxGameMinutes > 0 ? payload.Budgets.MaxGameMinutes : 60,
                    CancelPolicy: payload.CancelPolicy ?? "safe-point",
                    IdempotencyKey: envelope.IdempotencyKey,
                    ExpectedWorldRevision: payload.ExpectedWorldRevision
                );

                accepted = TryStartPurchase(request, envelope, out var earlyResult, out rejectReason);
                earlyPayload = earlyResult?.ToTransportPayload();
            }
            else if (nativeKind is not null)
            {
                var targets = new List<NativeActionTarget>();
                if (payload.Parameters.Tiles is { Count: > 0 })
                {
                    foreach (var tile in payload.Parameters.Tiles)
                        targets.Add(new NativeActionTarget(new TileCoordinate(tile.X, tile.Y), payload.Parameters.AnimalName));
                }
                else if (payload.Parameters.Tile is { } single)
                {
                    targets.Add(new NativeActionTarget(new TileCoordinate(single.X, single.Y), null));
                }
                else if (nativeKind == NativeActionKind.FeedAnimals)
                {
                    targets.Add(new NativeActionTarget(_actor.Tile, payload.Parameters.BuildingName));
                }

                string nativeLocation = nativeKind == NativeActionKind.FeedAnimals &&
                    !string.IsNullOrWhiteSpace(payload.Parameters.BuildingName)
                    ? payload.Parameters.BuildingName!
                    : payload.Parameters.LocationId;

                var request = new NativeActionRequest(
                    CommandId: payload.CommandId,
                    TaskId: payload.TaskId,
                    LocationId: nativeLocation,
                    Kind: nativeKind.Value,
                    SkillId: skillId,
                    Targets: targets,
                    ItemId: nativeKind == NativeActionKind.ApplyFertilizer
                        ? payload.Parameters.FertilizerItemId
                        : payload.Parameters.ItemId,
                    ItemCount: payload.Parameters.ItemCount ?? 1,
                    MaxStamina: payload.Budgets?.MaxStamina > 0 ? payload.Budgets.MaxStamina : 50f,
                    MaxWater: payload.Budgets?.MaxWater ?? 0,
                    MaxGameMinutes: payload.Budgets?.MaxGameMinutes > 0 ? payload.Budgets.MaxGameMinutes : 60,
                    CancelPolicy: payload.CancelPolicy ?? "safe-point",
                    IdempotencyKey: envelope.IdempotencyKey,
                    ExpectedWorldRevision: payload.ExpectedWorldRevision
                );

                accepted = TryStartNativeAction(request, envelope, out var earlyResult, out rejectReason);
                earlyPayload = earlyResult?.ToTransportPayload();
            }
            else if (isWithdraw)
            {
                var chestTile = payload.Parameters.ChestTile!;
                var withdrawSpecs = new List<WithdrawItemSpec>();
                if (payload.Parameters.Items is { Count: > 0 })
                {
                    foreach (var it in payload.Parameters.Items)
                    {
                        if (!string.IsNullOrWhiteSpace(it.ItemId) && it.Count > 0)
                        {
                            withdrawSpecs.Add(new WithdrawItemSpec(it.ItemId, it.Count));
                        }
                    }
                }
                else if (!string.IsNullOrWhiteSpace(payload.Parameters.SeedItemId))
                {
                    withdrawSpecs.Add(new WithdrawItemSpec(payload.Parameters.SeedItemId, 1));
                }

                var request = new ChestActionRequest(
                    CommandId: payload.CommandId,
                    TaskId: payload.TaskId,
                    Kind: ChestActionKind.Withdraw,
                    LocationId: payload.Parameters.LocationId,
                    ChestTile: new TileCoordinate(chestTile.X, chestTile.Y),
                    WithdrawItems: withdrawSpecs,
                    MaxStamina: payload.Budgets.MaxStamina,
                    MaxWater: payload.Budgets.MaxWater,
                    MaxGameMinutes: payload.Budgets.MaxGameMinutes,
                    CancelPolicy: payload.CancelPolicy,
                    IdempotencyKey: envelope.IdempotencyKey,
                    ExpectedWorldRevision: payload.ExpectedWorldRevision
                );

                accepted = TryStartChestAction(request, envelope, out var earlyResult, out rejectReason);
                earlyPayload = earlyResult?.ToTransportPayload();
            }
            else
            {
                var chestTile = payload.Parameters.ChestTile!;
                var request = new ChestActionRequest(
                    CommandId: payload.CommandId,
                    TaskId: payload.TaskId,
                    Kind: isDeposit ? ChestActionKind.Deposit : ChestActionKind.Organize,
                    LocationId: payload.Parameters.LocationId,
                    ChestTile: new TileCoordinate(chestTile.X, chestTile.Y),
                    ItemIds: isDeposit ? payload.Parameters.ItemIds : null,
                    MaxStamina: payload.Budgets.MaxStamina,
                    MaxWater: payload.Budgets.MaxWater,
                    MaxGameMinutes: payload.Budgets.MaxGameMinutes,
                    CancelPolicy: payload.CancelPolicy,
                    IdempotencyKey: envelope.IdempotencyKey,
                    ExpectedWorldRevision: payload.ExpectedWorldRevision
                );

                accepted = TryStartChestAction(request, envelope, out var earlyResult, out rejectReason);
                earlyPayload = earlyResult?.ToTransportPayload();
            }

            if (!accepted)
            {
                if (_cancelledTaskIds.Contains(payload.TaskId) || _cancelledTaskIds.Contains(payload.CommandId))
                {
                    rejectResult = new SkillResultPayload(
                        CommandId: payload.CommandId,
                        TaskId: payload.TaskId,
                        TerminalState: "cancelled",
                        CompletedCount: 0,
                        SkippedCount: 0,
                        FailedCount: 0,
                        FinalWorldRevision: _observer.WorldRevision,
                        Effects: new List<Dictionary<string, object>>(),
                        Error: new SkillResultError("CANCELLED_BEFORE_EXECUTE", "Task was cancelled prior to execution.", null, false),
                        SkillId: payload.SkillId
                    );
                    return false;
                }

                if (_actor.ActiveTaskId is not null)
                {
                    rejectResult = BuildRejection(payload, "CONFLICT",
                        rejectReason ?? $"Actor is busy executing active task '{_actor.ActiveTaskId}'.", retryable: true);
                    return false;
                }

                rejectResult = earlyPayload ?? BuildRejection(payload, "REJECTED",
                    rejectReason ?? "Execution rejected.", retryable: false);
                return false;
            }

            rejectResult = null;
            return true;
        }
    }

    /// <summary>
    /// Core entry point to start one explicit native agricultural/husbandry action.
    /// </summary>
    public bool TryStartNativeAction(
        NativeActionRequest request,
        EnvelopeDto? envelope,
        out NativeActionResult? earlyResult,
        out string? rejectReason)
    {
        ArgumentNullException.ThrowIfNull(request);

        lock (_taskLock)
        {
            if (_nativeActionMachine is null)
            {
                rejectReason = "Native farming/husbandry actions are not available (no native action adapter configured).";
                earlyResult = null;
                return false;
            }

            if (_cancelledTaskIds.Contains(request.TaskId) || _cancelledTaskIds.Contains(request.CommandId))
            {
                rejectReason = "Task was cancelled prior to execution.";
                earlyResult = null;
                return false;
            }

            if (!TryCheckIdle(out rejectReason))
            {
                earlyResult = null;
                return false;
            }

            _currentExecuteEnvelope = envelope;

            if (!_nativeActionMachine.Start(request, out earlyResult))
            {
                rejectReason = earlyResult?.ErrorMessage ?? "State machine start rejected.";
                Log($"{request.SkillId} rejected (commandId={request.CommandId}): {rejectReason}", StardewModdingAPI.LogLevel.Warn);
                return false;
            }

            Log($"{request.SkillId} accepted (commandId={request.CommandId}, taskId={request.TaskId}, targets={request.Targets.Count}).");
            rejectReason = null;
            return true;
        }
    }

    private SkillResultPayload BuildRejection(SkillExecutePayload payload, string code, string message, bool retryable)    {
        return new SkillResultPayload(
            CommandId: payload.CommandId,
            TaskId: payload.TaskId,
            TerminalState: "rejected",
            CompletedCount: 0,
            SkippedCount: 0,
            FailedCount: 0,
            FinalWorldRevision: _observer.WorldRevision,
            Effects: new List<Dictionary<string, object>>(),
            Error: new SkillResultError(code, message, null, retryable),
            SkillId: payload.SkillId
        );
    }

    private void HandleWaterZoneCompleted(WaterZoneResult result)
    {
        lock (_taskLock)
        {
            _lastResult = result;
            CompleteExecution(result.ToTransportPayload());
        }
    }

    private void HandleHarvestZoneCompleted(HarvestZoneResult result)
    {
        lock (_taskLock)
        {
            CompleteExecution(result.ToTransportPayload());
        }
    }

    private void HandleChestActionCompleted(ChestActionResult result)
    {
        lock (_taskLock)
        {
            CompleteExecution(result.ToTransportPayload());
        }
    }

    private void HandleHoeZoneCompleted(HoeZoneResult result)
    {
        lock (_taskLock)
        {
            CompleteExecution(result.ToTransportPayload());
        }
    }

    private void HandlePlantZoneCompleted(PlantZoneResult result)
    {
        lock (_taskLock)
        {
            CompleteExecution(result.ToTransportPayload());
        }
    }

    private void HandleShippingCompleted(ShippingResult result)
    {
        lock (_taskLock)
        {
            CompleteExecution(result.ToTransportPayload());
        }
    }

    private void HandlePurchaseCompleted(PurchaseResult result)
    {
        lock (_taskLock)
        {
            CompleteExecution(result.ToTransportPayload());
        }
    }

    private void HandleNavigationCompleted(NavigationResult result)
    {
        lock (_taskLock)
        {
            CompleteExecution(result.ToTransportPayload());
        }
    }

    private void HandleNativeActionCompleted(NativeActionResult result)
    {
        lock (_taskLock)
        {
            CompleteExecution(result.ToTransportPayload());
        }
    }

    /// <summary>
    /// Unified completion path for every skill: emit skill.result, bump the world
    /// revision, push a fresh snapshot, and raise the persistence signal.
    /// </summary>
    private void CompleteExecution(SkillResultPayload payload)
    {
        _lastSkillResultPayload = payload;

        if (_transportServer != null && _currentExecuteEnvelope != null)
        {
            _ = _transportServer.SendSkillResultAsync(
                payload,
                _currentExecuteEnvelope.MessageId,
                _currentExecuteEnvelope.IdempotencyKey
            );
        }

        // Push a fresh world snapshot after each completed execution so clients
        // (MCP query_farm_work, planners) always see current world state and revision.
        if (_transportServer != null)
        {
            _observer.BumpRevision();
            var freshSnapshot = CaptureCurrentSnapshot(_observer.WorldRevision);
            _ = _transportServer.SendSnapshotAsync(freshSnapshot, _observer.WorldRevision);
        }

        OnExecutionCompleted?.Invoke();
    }

    public void OnCancelSkill(SkillCancelPayload payload, EnvelopeDto envelope)
    {
        lock (_taskLock)
        {
            if (!string.IsNullOrEmpty(payload.CommandId))
            {
                _cancelledTaskIds.Add(payload.CommandId);
            }
            if (envelope != null && !string.IsNullOrEmpty(envelope.CorrelationId))
            {
                _cancelledTaskIds.Add(envelope.CorrelationId);
            }
            ActiveMachine?.RequestCancel(payload.Reason);
        }
    }

    public void OnPauseSkill(SkillPausePayload payload, EnvelopeDto envelope)
    {
        lock (_taskLock)
        {
            ActiveMachine?.RequestPause();
        }
    }

    public void OnResumeSkill(SkillResumePayload payload, EnvelopeDto envelope)
    {
        lock (_taskLock)
        {
            ActiveMachine?.Resume();
        }
    }

    /// <summary>
    /// Pushes a periodic data refresh snapshot on game time changes (every 10-minute game tick).
    /// Keeps worldRevision unchanged so task completion / revision-based concurrency semantics are unaffected.
    /// </summary>
    public void OnTimeChanged(int newTime)
    {
        if (_transportServer != null)
        {
            var freshSnapshot = CaptureCurrentSnapshot(_observer.WorldRevision);
            _ = _transportServer.SendSnapshotAsync(freshSnapshot, _observer.WorldRevision);
            _log?.Invoke($"Periodic snapshot refresh on TimeChanged ({newTime}) pushed at revision {_observer.WorldRevision}.", StardewModdingAPI.LogLevel.Trace);
        }
    }

    /// <summary>
    /// §2.4 Player backpack aggregate for milestone-completion verification:
    /// stacks summed per distinct item name, empty/null slots skipped, capped at
    /// <paramref name="maxItems"/>. Never throws: a partially readable inventory
    /// degrades to fewer (or zero) entries and verification just lacks evidence.
    /// </summary>
    private static List<PlayerItemDto> BuildPlayerItemsSnapshot(int maxItems)
    {
        var result = new List<PlayerItemDto>();
        if (maxItems <= 0) return result;

        try
        {
            var items = Game1.player?.Items;
            if (items == null) return result;

            var index = new Dictionary<string, int>(StringComparer.Ordinal);
            foreach (var item in items)
            {
                if (item == null) continue;
                string name = item.Name;
                if (string.IsNullOrWhiteSpace(name)) continue;
                if (index.TryGetValue(name, out int pos))
                {
                    long summed = (long)result[pos].Quantity + item.Stack;
                    result[pos] = result[pos] with { Quantity = (int)Math.Min(summed, int.MaxValue) };
                }
                else
                {
                    index[name] = result.Count;
                    result.Add(new PlayerItemDto(name, Math.Max(item.Stack, 1)));
                    if (result.Count >= maxItems) break;
                }
            }
        }
        catch
        {
            // Verification simply runs without item evidence.
        }
        return result;
    }

    public WorldSnapshotPayload CaptureCurrentSnapshot(long worldRevision)
    {
        lock (_taskLock)
        {
            var activeMachine = ActiveMachine;
            // Companion wallet is a real per-save fact the compact decision
            // context needs; read it strictly read-only and keep it unknown when
            // it cannot be confirmed (never a fabricated zero).
            int? companionMoney = null;
            string? companionMoneyStatus = null;
            try
            {
                var gameFarmer = _actor.GameFarmer;
                if (gameFarmer != null && Game1.player?.team != null)
                {
                    companionMoney = Game1.player.team.GetMoney(gameFarmer).Value;
                    companionMoneyStatus = "ok";
                }
                else
                {
                    companionMoneyStatus = "missing";
                }
            }
            catch
            {
                companionMoney = null;
                companionMoneyStatus = "missing";
            }

            var companion = new CompanionSnapshot(
                LocationId: _actor.LocationName,
                TileX: _actor.Tile.X,
                TileY: _actor.Tile.Y,
                FacingDirection: (int)_actor.Facing,
                Stamina: _actor.Stamina,
                MaxStamina: (int)_actor.MaxStamina,
                WaterCanLevel: _actor.WaterLeft,
                MaxWaterCanLevel: _actor.MaxWater,
                HasWateringCan: true,
                Activity: activeMachine is not null ? DescribeActivity(activeMachine) : "idle",
                AvailableMoney: companionMoney,
                MoneyStatus: companionMoneyStatus
            );

            // Scan farm work from observer for current location
            FarmWorkSnapshot farmWork;
            string locationName = _actor.LocationName ?? _observer.CurrentLocationName ?? "Farm";
            try
            {
                var workItems = _observer.ScanFarmWork(locationName);
                if (workItems.Count > 0)
                {
                    var unwatered = workItems
                        .Where(w => !w.IsWatered)
                        .Select(w => new TileCoord(w.Tile.X, w.Tile.Y))
                        .ToList();

                    var cropUnwatered = workItems
                        .Where(w => !w.IsWatered && w.HasCrop)
                        .Select(w => new TileCoord(w.Tile.X, w.Tile.Y))
                        .ToList();

                    int totalUnwatered = unwatered.Count;
                    const int maxReportedTiles = 64;
                    bool isTruncated = totalUnwatered > maxReportedTiles;
                    var reportedTiles = isTruncated ? unwatered.Take(maxReportedTiles).ToList() : unwatered;

                    var matureCrops = workItems
                        .Where(w => w.IsHarvestable)
                        .Select(w => new MatureCropTile(w.Tile.X, w.Tile.Y, w.CropId ?? ""))
                        .ToList();
                    int matureCount = matureCrops.Count;
                    bool matureTruncated = matureCount > MaxReportedMatureCrops;
                    var reportedMature = matureTruncated
                        ? matureCrops.Take(MaxReportedMatureCrops).ToList()
                        : matureCrops;

                    bool cropTruncated = cropUnwatered.Count > maxReportedTiles;
                    var reportedCropUnwatered = cropTruncated
                        ? cropUnwatered.Take(maxReportedTiles).ToList()
                        : cropUnwatered;

                    farmWork = new FarmWorkSnapshot(
                        TilledUnwateredTiles: reportedTiles,
                        TilledUnwateredCount: totalUnwatered,
                        IsTruncated: isTruncated,
                        MatureCropCount: matureCount,
                        MatureCrops: reportedMature,
                        MatureCropsTruncated: matureTruncated,
                        CropUnwateredTiles: reportedCropUnwatered,
                        CropUnwateredCount: cropUnwatered.Count,
                        CropUnwateredTruncated: cropTruncated
                    );
                }
                else
                {
                    farmWork = FarmWorkSnapshot.CreateEmpty();
                }
            }
            catch
            {
                farmWork = FarmWorkSnapshot.CreateEmpty();
            }

            var world = new WorldStateSnapshot(
                CurrentLocation: _observer.CurrentLocationName ?? locationName,
                TimeOfDay: _observer.TimeOfDay > 0 ? _observer.TimeOfDay : 600,
                Season: _observer.CurrentSeason ?? "spring",
                DayOfMonth: _observer.DayOfMonth > 0 ? _observer.DayOfMonth : 1,
                IsRaining: _observer.IsRaining,
                FarmWork: farmWork,
                Year: Game1.year > 0 ? Game1.year : 1,
                WeatherIcon: _observer.WeatherIcon,
                PlayerItems: BuildPlayerItemsSnapshot(MaxReportedPlayerItems),
                PlayerMoney: Game1.player?.Money,
                PlayerStamina: Game1.player?.Stamina,
                PlayerMaxStamina: Game1.player?.MaxStamina
            );

            // Companion inventory section
            CompanionInventorySnapshot inventory;
            try
            {
                var slots = _actor.GetInventorySnapshot()
                    .Select(i => new InventorySlotSnapshot(i.SlotIndex, i.ItemId, i.Name, i.DisplayName, i.Stack, i.Quality, i.IsTool))
                    .ToList();
                inventory = new CompanionInventorySnapshot(
                    Capacity: _actor.InventoryCapacity,
                    FreeSlots: _actor.FreeInventorySlots,
                    Slots: slots
                );
            }
            catch
            {
                inventory = new CompanionInventorySnapshot(0, 0, new List<InventorySlotSnapshot>());
            }

            // Farm chests section (normal chests only, capped)
            ChestsSnapshot chests;
            try
            {
                var scanned = _observer.ScanChests("Farm");
                bool chestsTruncated = scanned.Count > MaxReportedChests;
                var reportedChests = (chestsTruncated ? scanned.Take(MaxReportedChests) : scanned)
                    .Select(c => new ChestSnapshot(
                        Tile: new TileCoord(c.Tile.X, c.Tile.Y),
                        Capacity: c.Capacity,
                        FreeSlots: c.FreeSlots,
                        Contents: c.Contents
                            .Select(s => new ChestSlotSnapshot(s.Slot, s.ItemId, s.Name, s.DisplayName, s.Stack, s.Quality, s.IsSeed, s.Seasons?.ToList()))
                            .ToList()
                    ))
                    .ToList();
                chests = new ChestsSnapshot(chestsTruncated, reportedChests);
            }
            catch
            {
                chests = new ChestsSnapshot(false, new List<ChestSnapshot>());
            }

            // Planting scan section (ready to plant, tillable soil, companion seeds)
            PlantingSnapshot? planting = null;
            try
            {
                var scan = _observer.ScanPlantingOptions(locationName, _actor.Tile, 15, _actor);
                var seeds = scan.Seeds
                    .Select(s => new SeedItemSnapshot(
                        ItemId: s.ItemId,
                        Name: s.Name,
                        Stack: s.Stack,
                        CanPlantCurrentSeason: s.CanPlantCurrentSeason,
                        Seasons: s.Seasons.ToList(),
                        GrowthDays: s.GrowthDays,
                        Regrows: s.Regrows,
                        IsRaised: s.IsRaised
                    ))
                    .ToList();

                var tilledEmpty = scan.TilledEmptyTiles
                    .Select(t => new TileCoord(t.X, t.Y))
                    .ToList();
                var tillable = scan.TillableTiles
                    .Select(t => new TileCoord(t.X, t.Y))
                    .ToList();

                var candidateTiles = new CandidateTilesSnapshot(
                    TilledEmptyCount: scan.TotalTilledEmptyCount,
                    TilledEmptyTiles: tilledEmpty,
                    TilledEmptyTruncated: scan.TilledEmptyTruncated,
                    TillableCount: scan.TotalTillableCount,
                    TillableTiles: tillable,
                    TillableTruncated: scan.TillableTruncated
                );

                var bounds = new SearchBoundsSnapshot(
                    Center: new TileCoord(scan.SearchCenter.X, scan.SearchCenter.Y),
                    Radius: scan.SearchRadius
                );

                planting = new PlantingSnapshot(
                    Season: scan.Season,
                    DayOfMonth: scan.DayOfMonth,
                    CompanionHasHoe: scan.CompanionHasHoe,
                    Seeds: seeds,
                    CandidateTiles: candidateTiles,
                    SearchBounds: bounds
                );
            }
            catch
            {
                planting = null;
            }

            // Shop scan section (default Pierre's General Store "SeedShop")
            ShopSnapshot? shop = null;            try
            {
                var shopScan = _observer.ScanShop("SeedShop", _actor);
                var shopItems = shopScan.Items
                    .Select(i => new ShopItemSnapshot(
                        ItemId: i.ItemId,
                        Name: i.Name,
                        Price: i.Price,
                        Stock: i.Stock,
                        IsInfiniteStock: i.IsInfiniteStock,
                        TradeItem: i.TradeItem,
                        TradeItemCount: i.TradeItemCount,
                        LimitedStockMode: i.LimitedStockMode,
                        ActionsOnPurchase: i.ActionsOnPurchase?.ToList(),
                        Category: i.Category,
                        IsSeed: i.IsSeed
                    ))
                    .ToList();

                shop = new ShopSnapshot(
                    ShopId: shopScan.ShopId,
                    Status: shopScan.Status,
                    IsOpen: shopScan.IsOpen,
                    OwnerPresent: shopScan.OwnerPresent,
                    ClosedMessage: shopScan.ClosedMessage,
                    Owners: shopScan.Owners.ToList(),
                    Currency: shopScan.Currency,
                    AvailableMoney: shopScan.AvailableMoney,
                    MoneyStatus: shopScan.MoneyStatus,
                    ItemsCount: shopScan.Items.Count,
                    Items: shopItems,
                    ErrorMessage: shopScan.ErrorMessage,
                    LocationId: shopScan.LocationId,
                    InteractionTile: shopScan.InteractionTile.HasValue
                        ? new TileCoord(shopScan.InteractionTile.Value.X, shopScan.InteractionTile.Value.Y)
                        : null
                );
            }
            catch
            {
                shop = null;
            }

            // On-demand observation groups: machines and livestock are published as
            // bounded, nullable sections so a model can read each group separately
            // without dragging the whole backpack/chest picture into a farm tool.
            MachinesSnapshot? machines = null;
            try
            {
                var scan = _observer.ScanMachines(locationName, MaxReportedMachines + 1);
                bool truncated = scan.Count > MaxReportedMachines;
                var reported = (truncated ? scan.Take(MaxReportedMachines) : scan)
                    .Select(m => new MachineSnapshot(
                        Tile: new TileCoord(m.Tile.X, m.Tile.Y),
                        ItemId: m.ItemId,
                        Name: m.Name,
                        IsReady: m.IsReady,
                        MinutesUntilReady: m.MinutesUntilReady,
                        OutputItemId: m.OutputItemId,
                        OutputName: m.OutputName,
                        OutputStack: m.OutputStack,
                        OutputQuality: m.OutputQuality,
                        LastInputItemId: m.LastInputItemId,
                        HasInput: m.HasInput
                    ))
                    .ToList();
                machines = new MachinesSnapshot(truncated, reported);
            }
            catch
            {
                machines = null;
            }

            LivestockSnapshot? livestock = null;
            try
            {
                var scan = _observer.ScanLivestock(locationName, MaxReportedAnimalBuildings, MaxReportedAnimals);
                livestock = new LivestockSnapshot(
                    BuildingsTruncated: scan.BuildingsTruncated,
                    AnimalsTruncated: scan.AnimalsTruncated,
                    Buildings: scan.Buildings
                        .Select(b => new AnimalBuildingSnapshot(
                            BuildingType: b.BuildingType,
                            IndoorsName: b.IndoorsName,
                            Location: b.LocationName,
                            Tile: new TileCoord(b.Tile.X, b.Tile.Y),
                            DoorTile: new TileCoord(b.DoorTile.X, b.DoorTile.Y),
                            AnimalDoorOpen: b.AnimalDoorOpen,
                            DoorStateKnown: b.DoorStateKnown,
                            AnimalCount: b.AnimalCount,
                            AnimalLimit: b.AnimalLimit,
                            HayCount: b.HayCount,
                            HayCapacity: b.HayCapacity,
                            SiloHayCount: b.SiloHayCount,
                            Animals: b.Animals.Select(ToAnimalSnapshot).ToList()
                        ))
                        .ToList(),
                    RoamingAnimals: scan.RoamingAnimals.Select(ToAnimalSnapshot).ToList()
                );
            }
            catch
            {
                livestock = null;
            }

            FarmingSnapshot? farming = null;
            try
            {
                var refillTiles = _observer.FindWaterRefillTiles(locationName, _actor.Tile, 12, MaxReportedRefillTiles + 1);
                bool refillTruncated = refillTiles.Count > MaxReportedRefillTiles;
                var reportingRefill = (refillTruncated ? refillTiles.Take(MaxReportedRefillTiles) : refillTiles)
                    .Select(t => new TileCoord(t.X, t.Y))
                    .ToList();

                var groundItems = _observer.ScanGroundItems(locationName, _actor.Tile, 16, MaxReportedGroundItems + 1);
                bool groundTruncated = groundItems.Count > MaxReportedGroundItems;
                var reportedGround = (groundTruncated ? groundItems.Take(MaxReportedGroundItems) : groundItems)
                    .Select(g => new GroundItemSnapshot(
                        Tile: new TileCoord(g.Tile.X, g.Tile.Y),
                        Kind: g.Kind,
                        ItemId: g.ItemId,
                        Name: g.Name,
                        Stack: g.Stack,
                        IsDropped: g.IsDropped,
                        IsWeed: g.IsWeed,
                        CanBeGrabbed: g.CanBeGrabbed,
                        ClearTool: g.ClearTool,
                        IsStone: g.IsStone,
                        IsTwig: g.IsTwig
                    ))
                    .ToList();

                var fertilized = new List<TileCoord>();
                foreach (var workItem in _observer.ScanFarmWork(locationName))
                {
                    if (_observer.HasFertilizer(locationName, workItem.Tile))
                        fertilized.Add(new TileCoord(workItem.Tile.X, workItem.Tile.Y));
                }

                List<string> companionTools;
                try { companionTools = _actor.GetToolNames().OrderBy(n => n).ToList(); }
                catch { companionTools = new List<string>(); }

                var choppable = _observer.ScanChoppableTrees(locationName, _actor.Tile, 16, MaxReportedChoppableTrees + 1);
                bool choppableTruncated = choppable.Count > MaxReportedChoppableTrees;
                var reportedChoppable = (choppableTruncated ? choppable.Take(MaxReportedChoppableTrees) : choppable)
                    .Select(t => new ChoppableTreeSnapshot(
                        Tile: new TileCoord(t.Tile.X, t.Tile.Y),
                        Kind: t.Kind,
                        GrowthStage: t.GrowthStage,
                        Tapped: t.Tapped,
                        Width: t.Width,
                        Height: t.Height
                    ))
                    .ToList();

                farming = new FarmingSnapshot(
                    Location: locationName,
                    RefillWaterTiles: reportingRefill,
                    GroundItems: reportedGround,
                    GroundItemsTruncated: groundTruncated,
                    FertilizedTiles: fertilized,
                    CompanionTools: companionTools,
                    ChoppableTrees: reportedChoppable,
                    ChoppableTreesTruncated: choppableTruncated
                );
            }
            catch
            {
                farming = null;
            }

            return new WorldSnapshotPayload(
                CapturedRevision: worldRevision,
                Companion: companion,
                World: world,
                FarmWork: farmWork,
                Inventory: inventory,
                Chests: chests,
                Planting: planting,
                Shop: shop,
                Machines: machines,
                Livestock: livestock,
                Farming: farming
            );
        }
    }

    private static AnimalSnapshot ToAnimalSnapshot(AnimalScanInfo animal) => new(
        Name: animal.Name,
        AnimalType: animal.AnimalType,
        DisplayType: animal.DisplayType,
        Age: animal.Age,
        Happiness: animal.Happiness,
        Fullness: animal.Fullness,
        Friendship: animal.Friendship,
        ProduceReady: animal.ProduceReady,
        CurrentProduceId: animal.CurrentProduceId,
        HarvestType: animal.HarvestType,
        RequiredTool: animal.RequiredTool,
        BuildingType: animal.BuildingType,
        Location: animal.LocationName,
        Tile: new TileCoord(animal.Tile.X, animal.Tile.Y),
        WasPetToday: animal.WasPetToday,
        WasAutoPetToday: animal.WasAutoPetToday
    );

    private static string DescribeActivity(ISkillExecutionMachine machine)    {
        return machine switch
        {
            WaterZoneStateMachine => "watering",
            HarvestZoneStateMachine => "harvesting",
            ChestActionStateMachine chest => chest.CurrentRequest?.Kind == ChestActionKind.Organize ? "organizing" : chest.CurrentRequest?.Kind == ChestActionKind.Withdraw ? "withdrawing" : "depositing",
            HoeZoneStateMachine => "hoeing",
            PlantZoneStateMachine => "planting",
            ShippingStateMachine => "shipping",
            PurchaseStateMachine => "purchasing",
            NativeActionStateMachine native => DescribeNativeActivity(native.CurrentRequest?.Kind),
            NavigationStateMachine => "navigating",
            _ => "working"
        };
    }

    private static string DescribeNativeActivity(NativeActionKind? kind) => kind switch
    {
        NativeActionKind.RefillWateringCan => "refilling",
        NativeActionKind.ApplyFertilizer => "fertilizing",
        NativeActionKind.ClearDebris => "clearing",
        NativeActionKind.PickupItems => "picking-up",
        NativeActionKind.InsertMachine => "loading-machine",
        NativeActionKind.CollectMachine => "collecting-machine",
        NativeActionKind.PetAnimal => "petting",
        NativeActionKind.FeedAnimals => "feeding",
        NativeActionKind.ToggleAnimalDoor => "toggling-door",
        NativeActionKind.CollectAnimalProduce => "collecting-produce",
        NativeActionKind.ChopTree => "chopping",
        _ => "working"
    };

    public string GetActivityStatus()
    {
        var machine = ActiveMachine;
        if (machine is null) return "idle";
        string activity = DescribeActivity(machine);
        if (machine.IsPaused) return "paused";
        return machine.TotalTargets > 0
            ? $"{activity} {Math.Clamp(machine.CurrentTargetIndex + 1, 1, machine.TotalTargets)}/{machine.TotalTargets}"
            : activity;
    }

    public void OnTransportDisconnected(string reason)
    {
        lock (_taskLock)
        {
            ActiveMachine?.RequestCancel($"Transport disconnected: {reason}");
        }
    }
}
