using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Tools;

namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// Phase-1 binding of the sole native local player (<see cref="Game1.player"/>) as the
/// mechanics actor. Every member proxies native farmer semantics: movement is a held
/// intention injected via <see cref="Farmer.setMoving(byte)"/> and applied by the game's
/// own Farmer.Update/MovePosition (collision, wall sliding and warp triggers included),
/// cross-map travel is a native <see cref="Game1.warpFarmer(string, int, int, bool)"/>,
/// and inventory mutations go through the real farmer primitives. Nothing here teleports
/// the player, synthesizes a tool lifecycle, or grants resources.
///
/// Binding is explicit and fail-fast: the constructor throws unless the farmer IS
/// <see cref="Game1.player"/>, and throws in multiplayer. A remote farmhand actor is the
/// documented future extension point (per-player action leases); until then any non-local
/// binding is unsupported by design.
/// </summary>
public sealed class PlayerActor : IFarmerActor
{
    private readonly object _lock = new();
    private readonly Farmer _player;
    private readonly long _boundPlayerId;
    private readonly string? _boundSaveId;
    private readonly Action<string, StardewModdingAPI.LogLevel>? _log;
    private string? _activeTaskId;
    private int? _heldDirection;
    public int? HeldDirection => _heldDirection;
    private int _animationPulses;


    private void Log(string message, StardewModdingAPI.LogLevel level = StardewModdingAPI.LogLevel.Info) => _log?.Invoke(message, level);

    /// <summary>
    /// Binds the actor to the native local player. Phase-1 actor id for the sole native
    /// player; a remote/farmhand farmer is rejected explicitly.
    /// </summary>
    /// <exception cref="ArgumentException">The farmer is not <see cref="Game1.player"/>.</exception>
    /// <exception cref="InvalidOperationException">The game is in multiplayer (phase 1 is single-player only).</exception>
    public PlayerActor(Farmer player, Action<string, StardewModdingAPI.LogLevel>? log = null)
    {
        ArgumentNullException.ThrowIfNull(player);
        if (!ReferenceEquals(player, Game1.player))
        {
            throw new ArgumentException(
                "PlayerActor binds only to the native local player (Game1.player). " +
                "Remote/farmhand binding is unsupported in phase 1 (future per-player leases).",
                nameof(player));
        }
        if (Game1.IsMultiplayer)
        {
            throw new InvalidOperationException(
                "PlayerActor is single-player only in phase 1; multiplayer requires per-player action leases.");
        }
        _player = player;
        _boundPlayerId=player.UniqueMultiplayerID;
        _boundSaveId=StardewModdingAPI.Constants.SaveFolderName;
        _log = log;
    }

    /// <summary>Phase-1 actor id for the sole native player.</summary>
    public string CompanionId => "player";

    public bool BindingValid => ReferenceEquals(Game1.player,_player) && _player.UniqueMultiplayerID == _boundPlayerId && StardewModdingAPI.Constants.SaveFolderName == _boundSaveId;
    public Farmer? GameFarmer => BindingValid ? _player : null;

    /// <summary>Native farmer stamina; the setter is the native farmer stamina write (unused by player-mode flows, kept for semantic parity).</summary>
    public float Stamina
    {
        get => _player.Stamina;
        set => throw new InvalidOperationException("Player stamina is settled only by native game actions.");
    }

    public int MaxStamina => _player.MaxStamina;

    private WateringCan? PlayerWateringCan => _player.Items.OfType<WateringCan>().FirstOrDefault();

    public int WaterLeft
    {
        get => PlayerWateringCan?.WaterLeft ?? 0;
        set
        {
            throw new InvalidOperationException("Player water is settled only by native watering/refill actions.");
        }
    }

    public int MaxWater
    {
        get
        {
            var can = PlayerWateringCan;
            return can != null && can.waterCanMax > 0 ? can.waterCanMax : CompanionActorState.DefaultMaxWater;
        }
    }

    public string LocationName => _player.currentLocation?.NameOrUniqueName ?? string.Empty;

    public Vector2 PixelPosition
    {
        get => _player.Position;
        set { } // Player positions are only advanced by native movement/collision.
    }

    public TileCoordinate Tile
    {
        get
        {
            var tile = _player.Tile;
            return new TileCoordinate((int)tile.X, (int)tile.Y);
        }
    }

    public FacingDirection Facing
    {
        get => (FacingDirection)_player.FacingDirection;
        set => _player.faceDirection((int)value);
    }

    public bool IsExhausted => _player.exhausted.Value;

    public bool IsWateringCanEmpty => WaterLeft <= 0;

    public string? ActiveTaskId => _activeTaskId;

    public void SetActiveTask(string? taskId)
    {
        lock (_lock)
        {
            _activeTaskId = taskId;
        }
    }

    // Farmer.setMoving encoding (IL-verified): press = 1 << dir, release = press | 32,
    // 64 halts all. Dirs are 0=up, 1=right, 2=down, 3=left (same values as FacingDirection).
    private const int ArrivalMicroAlignPixels = 8;

    /// <summary>
    /// Translates a per-tick delta into a held movement intention for the game's own
    /// Farmer.Update to apply (no synchronous position write). A zero delta releases the
    /// held direction; state machines call <see cref="Halt"/> when navigation completes.
    /// </summary>
    public void MovePixels(float dx, float dy)
    {
        lock (_lock)
        {
            int? desired = ResolveDirection(dx, dy);
            if (desired == _heldDirection)
            {
                return;
            }
            // The intent hook rewrites setMoving to the currently held input.
            // Releasing while that intent is still set re-presses the OLD key,
            // leaving two native directions held after a turn. Clear the intent
            // before native Halt, then press only the newly requested direction.
            _heldDirection = null;
            _player.Halt();
            _heldDirection = desired;
            if (desired.HasValue)
            {
                _player.setMoving((byte)(1 << desired.Value));
            }
        }
    }

    private static int? ResolveDirection(float dx, float dy)
    {
        if (dx == 0f && dy == 0f)
        {
            return null;
        }
        bool horizontalDominant = Math.Abs(dx) >= Math.Abs(dy);
        // Near-aligned on the dominant axis while the other still has distance: use the other.
        if (horizontalDominant && Math.Abs(dx) <= ArrivalMicroAlignPixels && Math.Abs(dy) > ArrivalMicroAlignPixels)
        {
            horizontalDominant = false;
        }
        else if (!horizontalDominant && Math.Abs(dy) <= ArrivalMicroAlignPixels && Math.Abs(dx) > ArrivalMicroAlignPixels)
        {
            horizontalDominant = true;
        }
        if (horizontalDominant)
        {
            return dx > 0f ? (int)FacingDirection.Right : (int)FacingDirection.Left;
        }
        return dy > 0f ? (int)FacingDirection.Down : (int)FacingDirection.Up;
    }

    public void Halt()
    {
        lock (_lock)
        {
            _heldDirection = null;
            _player.Halt();
        }
    }

    /// <summary>
    /// Native cross-map transition (the exact call a warp tile makes). The actual map
    /// change completes after the native fade; callers poll <see cref="LocationName"/>.
    /// </summary>
    public void SetLocation(string locationName, TileCoordinate tile)
    {
        Halt();
        Game1.warpFarmer(locationName, tile.X, tile.Y, false);
    }

    public void Face(FacingDirection dir)
    {
        _player.faceDirection((int)dir);
    }

    public WateringCan? WateringCan => PlayerWateringCan;

    public Hoe? Hoe => _player.Items.OfType<Hoe>().FirstOrDefault();

    /// <summary>
    /// Real inventory tool lookup on the player (never a granted/phantom tool).
    /// </summary>
    public T? FindTool<T>() where T : Tool
    {
        return _player.Items.OfType<T>().FirstOrDefault();
    }

    public IReadOnlyList<string> GetToolNames()
    {
        return _player.Items.OfType<Tool>()
            .Select(t => t is MeleeWeapon weapon && weapon.isScythe() ? "Scythe" : t.GetType().Name)
            .Distinct()
            .ToList();
    }

    public bool IsUsingTool => _player.UsingTool;

    public int CurrentFrame => _player.FarmerSprite?.CurrentFrame ?? 0;

    public ToolAnimationPhase AnimationPhase => _animationPulses switch
    {
        0 => ToolAnimationPhase.Windup,
        1 => ToolAnimationPhase.EffectPoint,
        _ => ToolAnimationPhase.Completed,
    };

    /// <summary>
    /// No synthetic tool lifecycle on the real player: nothing here touches CanMove,
    /// UsingTool or CurrentToolIndex (state machines Face and the adapters equip; the
    /// physical effect happens once at the EffectPoint through the adapter DoFunction).
    /// </summary>
    public void BeginUsingTool() => BeginUsingTool("Watering Can");

    public float ToolStartStamina { get; private set; }
    public int ToolStartWater { get; private set; }
    private bool _nativeReleaseSent;
    private bool _nativeStarted;
    public void BeginUsingTool(string toolName)
    {
        var tool = _player.Items.OfType<Tool>().FirstOrDefault(t =>
            toolName == "Weapon" ? t is MeleeWeapon combatWeapon && !combatWeapon.isScythe() : toolName == "Scythe" ? t is MeleeWeapon weapon && weapon.isScythe() : toolName == "Watering Can" ? t is WateringCan : t.GetType().Name == toolName.Replace(" ",""));
        if (tool == null) throw new InvalidOperationException("Missing native tool: " + toolName);
        Halt();
        ToolStartStamina = _player.Stamina; ToolStartWater = WaterLeft;
        _animationPulses=0; _nativeReleaseSent=false; _nativeStarted=false;
        _player.CurrentToolIndex=_player.Items.IndexOf(tool);
        var offset = Facing switch { FacingDirection.Up => new Point(0,-1), FacingDirection.Down => new Point(0,1), FacingDirection.Left => new Point(-1,0), _ => new Point(1,0) };
        _player.lastClick=new Vector2((Tile.X+offset.X)*64+32,(Tile.Y+offset.Y)*64+32);
        _player.BeginUsingTool(); // Native net event starts the genuine lifecycle next update.
    }
    public ToolAnimationPhase UpdateToolAnimation(GameTime? time,long tickCount)
    {
        if (_player.UsingTool) {
            _nativeStarted=true;
            if (!_nativeReleaseSent && _player.canReleaseTool) {
                _nativeReleaseSent=true; _player.CurrentTool.endUsing(_player.currentLocation,_player);
            }
            return ToolAnimationPhase.Windup;
        }
        if (!_nativeStarted) return ToolAnimationPhase.Windup;
        if (_animationPulses++ == 0) return ToolAnimationPhase.EffectPoint;
        return ToolAnimationPhase.Completed;
    }

    public void EndUsingTool()
    {
        lock (_lock)
        {
            _animationPulses = 0;
        }
    }

    public int InventoryCapacity => _player.MaxItems;

    public int FreeInventorySlots
    {
        get
        {
            int capacity = InventoryCapacity;
            int occupied = 0;
            int bound = Math.Min(capacity, _player.Items.Count);
            for (int slot = 0; slot < bound; slot++)
            {
                if (_player.Items[slot] != null)
                {
                    occupied++;
                }
            }
            return Math.Max(0, capacity - occupied);
        }
    }

    public IReadOnlyList<InventoryItem> GetInventorySnapshot()
    {
        var snapshot = new List<InventoryItem>();
        int bound = Math.Min(InventoryCapacity, _player.Items.Count);
        for (int slot = 0; slot < bound; slot++)
        {
            var item = _player.Items[slot];
            if (item is null)
            {
                continue;
            }
            snapshot.Add(ToInventoryItem(item, slot));
        }
        return snapshot;
    }

    /// <summary>Item -> InventoryItem DTO mapping shared by the snapshot/removal paths (mirrors FarmerMechanicsActor).</summary>
    private static InventoryItem ToInventoryItem(Item item, int slot) => new(
        itemId: item.QualifiedItemId ?? item.ItemId ?? item.Name,
        name: item.Name,
        stack: item.Stack,
        quality: item.Quality,
        category: item.getCategoryName() ?? "",
        upgradeLevel: item is Tool tool ? tool.UpgradeLevel : 0,
        waterLeft: item is WateringCan can ? can.WaterLeft : 0,
        slotIndex: slot,
        isTool: item is Tool,
        displayName: item.DisplayName ?? item.Name
    );

    public bool TryAddItemToInventory(Item gameItem)
    {
        ArgumentNullException.ThrowIfNull(gameItem);
        if (gameItem.Stack <= 0)
        {
            return false;
        }

        bool canAccept;
        try
        {
            canAccept = _player.couldInventoryAcceptThisItem(gameItem);
        }
        catch (Exception ex)
        {
            Log($"couldInventoryAcceptThisItem check failed: {ex.Message}", StardewModdingAPI.LogLevel.Warn);
            return false;
        }
        if (!canAccept)
        {
            return false;
        }

        Item? leftover = _player.addItemToInventory(gameItem);
        if (leftover != null)
        {
            Log($"Native add partially accepted '{gameItem.Name}' (leftover stack {leftover.Stack}); returning false, the inventory already changed natively.", StardewModdingAPI.LogLevel.Warn);
            return false;
        }
        return true;
    }

    public bool TryAddItemToInventory(InventoryItem item)
    {
        ArgumentNullException.ThrowIfNull(item);
        if (string.IsNullOrEmpty(item.ItemId) || item.Stack <= 0)
        {
            return false;
        }

        Item? gameItem;
        try
        {
            gameItem = ItemRegistry.Create(item.ItemId, item.Stack, item.Quality);
        }
        catch
        {
            return false;
        }
        if (gameItem is null)
        {
            return false;
        }
        if (gameItem is Tool tool && item.UpgradeLevel > 0)
        {
            tool.UpgradeLevel = item.UpgradeLevel;
        }
        if (gameItem is WateringCan can)
        {
            // Preserve zero water exactly: do not convert 0 to a full can.
            can.WaterLeft = item.WaterLeft;
        }
        return TryAddItemToInventory(gameItem);
    }

    public bool TryRemoveItemAtSlot(int slotIndex, int stackToRemove, out InventoryItem? removed)
    {
        removed = null;
        if (stackToRemove <= 0)
        {
            return false;
        }
        if (slotIndex < 0 || slotIndex >= _player.Items.Count)
        {
            return false;
        }
        var item = _player.Items[slotIndex];
        if (item is null)
        {
            return false;
        }

        int removedStack = Math.Min(stackToRemove, item.Stack);
        removed = ToInventoryItem(item, slotIndex) with { Stack = removedStack };
        if (stackToRemove >= item.Stack)
        {
            _player.removeItemFromInventory(item);
        }
        else
        {
            item.Stack -= stackToRemove;
        }
        return true;
    }

    public bool TryConsumeItem(string itemId, int count = 1)
    {
        if (count <= 0)
        {
            return true;
        }
        // All-or-nothing (mirrors FarmerMechanicsActor): verify availability first.
        int total = GetItemCount(itemId);
        if (total < count)
        {
            return false;
        }

        int needed = count;
        for (int i = 0; i < _player.Items.Count && needed > 0; i++)
        {
            var item = _player.Items[i];
            if (item == null)
            {
                continue;
            }
            if (!FarmerMechanicsActor.ItemMatchesId(item, itemId))
            {
                continue;
            }
            if (item.Stack > needed)
            {
                item.Stack -= needed;
                needed = 0;
            }
            else
            {
                needed -= item.Stack;
                _player.removeItemFromInventory(item);
            }
        }
        return true;
    }

    public bool TryExtractItem(string itemId, int count, out List<Item> extractedItems)
    {
        extractedItems = new List<Item>();
        if (count <= 0)
        {
            return true;
        }
        int total = GetItemCount(itemId);
        if (total < count)
        {
            return false;
        }

        int needed = count;
        for (int i = 0; i < _player.Items.Count && needed > 0; i++)
        {
            var item = _player.Items[i];
            if (item == null)
            {
                continue;
            }
            if (!FarmerMechanicsActor.ItemMatchesId(item, itemId))
            {
                continue;
            }
            if (item.Stack > needed)
            {
                var extracted = item.getOne();
                extracted.Stack = needed;
                item.Stack -= needed;
                extractedItems.Add(extracted);
                needed = 0;
            }
            else
            {
                needed -= item.Stack;
                extractedItems.Add(item);
                _player.removeItemFromInventory(item);
            }
        }
        return true;
    }

    public int GetItemCount(string itemId)
    {
        int total = 0;
        foreach (var item in _player.Items)
        {
            if (item == null)
            {
                continue;
            }
            if (FarmerMechanicsActor.ItemMatchesId(item, itemId))
            {
                total += item.Stack;
            }
        }
        return total;
    }

    /// <summary>
    /// NO-OP by design: in player mode the native save owns all player state. Never touches
    /// player items or position.
    /// </summary>
    public void ApplyPersistentState(CompanionActorState state)
    {
        ArgumentNullException.ThrowIfNull(state);
        Log("player mode: native save owns player state; persistent apply skipped", StardewModdingAPI.LogLevel.Info);
    }

    /// <summary>
    /// Read-only diagnostic snapshot built from live data (native save remains
    /// authoritative; PersistedTaskId is always null).
    /// </summary>
    public CompanionActorState CapturePersistentState()
    {
        return new CompanionActorState
        {
            Version = CompanionActorState.CurrentSchemaVersion,
            CompanionId = CompanionId,
            Stamina = Stamina,
            MaxStamina = MaxStamina,
            Water = WaterLeft,
            MaxWater = MaxWater,
            Pose = new AuthoritativePose(LocationName, Tile, Facing),
            Inventory = GetInventorySnapshot().ToList(),
            LastSavedUtc = DateTime.UtcNow.ToString("o"),
            PersistedTaskId = null
        };
    }
}
