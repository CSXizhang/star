using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Tools;

namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// Persistent Independent Farmer Mechanics Actor.
/// Encapsulates physical tick movement, actual WateringCan tool lifecycle,
/// independent stamina/water pools, and authoritative spatial pose.
/// </summary>
public sealed class FarmerMechanicsActor : IFarmerActor
{
    private readonly object _lock = new();
    private readonly Farmer? _gameFarmer;
    private readonly Action<string, StardewModdingAPI.LogLevel>? _log;
    private int _lastAnimIndex = -1;

    private void Log(string message, StardewModdingAPI.LogLevel level = StardewModdingAPI.LogLevel.Info) => _log?.Invoke(message, level);
    private WateringCan? _wateringCan;
    private Hoe? _hoe;

    private float _stamina = CompanionActorState.DefaultMaxStamina;
    private int _maxStamina = (int)CompanionActorState.DefaultMaxStamina;
    private int _waterLeft = CompanionActorState.DefaultMaxWater;
    private int _maxWater = CompanionActorState.DefaultMaxWater;
    private string _locationName = "Farm";
    private Vector2 _pixelPosition = new(64 * 64, 15 * 64);
    private FacingDirection _facing = FacingDirection.Down;
    private string? _activeTaskId;
    private List<InventoryItem> _inventory = new();
    private bool _isUsingTool;
    private int _currentFrame;
    private ToolAnimationPhase _animPhase = ToolAnimationPhase.None;

    public string CompanionId { get; }
    public Farmer? GameFarmer => _gameFarmer;
    public WateringCan? WateringCan => _gameFarmer != null ? _gameFarmer.Items.OfType<WateringCan>().FirstOrDefault() : _wateringCan;
    public Hoe? Hoe => _gameFarmer != null ? _gameFarmer.Items.OfType<Hoe>().FirstOrDefault() : _hoe;

    /// <summary>
    /// Real inventory tool lookup (never a granted/phantom tool). With a live
    /// GameFarmer the inventory is authoritative; the offline test path matches the
    /// persisted inventory snapshot, so unit tests configure tools exactly the way
    /// the player would provide them.
    /// </summary>
    public T? FindTool<T>() where T : Tool
    {
        string wanted = typeof(T).Name;
        lock (_lock)
        {
            if (_gameFarmer != null)
                return _gameFarmer.Items.OfType<T>().FirstOrDefault();

            string? itemId = _inventory.FirstOrDefault(i =>
                !string.IsNullOrEmpty(i.ItemId) && i.IsTool &&
                i.ItemId.Contains(wanted, StringComparison.OrdinalIgnoreCase))?.ItemId;
            if (itemId is null)
                return null;
            try
            {
                var created = ItemRegistry.Create(itemId, 1, 0);
                if (created is T tool)
                    return tool;
                if (created is Tool genericTool)
                {
                    // The registry knows the item id but not the concrete tool class:
                    // only accept the tool shapes the actor can safely drive natively.
                    if (typeof(T) == typeof(Axe) && genericTool is Axe axe) return (T?)(Tool?)axe;
                    if (typeof(T) == typeof(Pickaxe) && genericTool is Pickaxe pick) return (T?)(Tool?)pick;
                    if (typeof(T) == typeof(MilkPail) && genericTool is MilkPail pail) return (T?)(Tool?)pail;
                    if (typeof(T) == typeof(Shears) && genericTool is Shears shears) return (T?)(Tool?)shears;
                }
            }
            catch { /* falls through to the named-field fallbacks below */ }

            if (typeof(T) == typeof(WateringCan) && _wateringCan is T can) return can;
            if (typeof(T) == typeof(Hoe) && _hoe is T hoe) return hoe;
            return null;
        }
    }

    public IReadOnlyList<string> GetToolNames()
    {
        lock (_lock)
        {
            if (_gameFarmer != null)
                return _gameFarmer.Items.OfType<Tool>().Select(t => t.GetType().Name).Distinct().ToList();

            var names = _inventory
                .Where(i => i.IsTool && !string.IsNullOrEmpty(i.ItemId))
                .Select(i => i.ItemId)
                .Distinct()
                .Select(id => id.StartsWith("(T)", StringComparison.OrdinalIgnoreCase) ? id[3..] : id)
                .Where(name => !string.IsNullOrWhiteSpace(name))
                .ToList();
            if (_wateringCan != null && !names.Contains(_wateringCan.GetType().Name))
                names.Add(_wateringCan.GetType().Name);
            if (_hoe != null && !names.Contains(_hoe.GetType().Name))
                names.Add(_hoe.GetType().Name);
            return names;
        }
    }

    public string? ActiveTaskId => _activeTaskId;

    public bool IsUsingTool => _isUsingTool;
    public int CurrentFrame => _currentFrame;
    public ToolAnimationPhase AnimationPhase => _animPhase;


    public float Stamina
    {
        get
        {
            lock (_lock)
            {
                return _gameFarmer != null ? _gameFarmer.Stamina : _stamina;
            }
        }
        set
        {
            lock (_lock)
            {
                _stamina = Math.Clamp(value, 0f, MaxStamina);
                if (_gameFarmer != null)
                {
                    _gameFarmer.Stamina = _stamina;
                }
            }
        }
    }

    public int MaxStamina
    {
        get
        {
            lock (_lock)
            {
                return _gameFarmer != null ? _gameFarmer.MaxStamina : _maxStamina;
            }
        }
    }

    public int WaterLeft
    {
        get
        {
            lock (_lock)
            {
                if (_wateringCan != null)
                    return _wateringCan.WaterLeft;
                return _waterLeft;
            }
        }
        set
        {
            lock (_lock)
            {
                _waterLeft = Math.Clamp(value, 0, MaxWater);
                if (_wateringCan != null)
                {
                    _wateringCan.WaterLeft = _waterLeft;
                }
            }
        }
    }

    public int MaxWater
    {
        get
        {
            lock (_lock)
            {
                if (_wateringCan != null)
                    return _wateringCan.waterCanMax > 0 ? _wateringCan.waterCanMax : CompanionActorState.DefaultMaxWater;
                return _maxWater;
            }
        }
    }

    public string LocationName
    {
        get
        {
            lock (_lock)
            {
                if (_gameFarmer?.currentLocation != null)
                {
                    var loc = _gameFarmer.currentLocation;
                    return !string.IsNullOrWhiteSpace(loc.NameOrUniqueName)
                        ? loc.NameOrUniqueName
                        : loc.Name;
                }
                return _locationName;
            }
        }
    }

    public Vector2 PixelPosition
    {
        get
        {
            lock (_lock)
            {
                return _gameFarmer != null ? _gameFarmer.Position : _pixelPosition;
            }
        }
        set
        {
            lock (_lock)
            {
                _pixelPosition = value;
                if (_gameFarmer != null)
                {
                    _gameFarmer.Position = value;
                }
            }
        }
    }

    public TileCoordinate Tile
    {
        get
        {
            var pos = PixelPosition;
            return new TileCoordinate((int)(pos.X / 64f), (int)(pos.Y / 64f));
        }
    }

    public FacingDirection Facing
    {
        get
        {
            lock (_lock)
            {
                if (_gameFarmer != null)
                    return (FacingDirection)_gameFarmer.FacingDirection;
                return _facing;
            }
        }
        set
        {
            lock (_lock)
            {
                _facing = value;
                if (_gameFarmer != null)
                {
                    _gameFarmer.faceDirection((int)value);
                }
            }
        }
    }

    public bool IsExhausted => Stamina <= 0f;
    public bool IsWateringCanEmpty => WaterLeft <= 0;

    public FarmerMechanicsActor(
        string companionId = "default-companion",
        Farmer? gameFarmer = null,
        WateringCan? wateringCan = null,
        string initialLocation = "Farm",
        TileCoordinate? initialTile = null,
        Action<string, StardewModdingAPI.LogLevel>? log = null,
        Hoe? hoe = null)
    {
        CompanionId = companionId;
        _gameFarmer = gameFarmer;
        _locationName = initialLocation;
        _log = log;

        var tile = initialTile ?? new TileCoordinate(64, 15);
        _pixelPosition = new Vector2(tile.X * 64, tile.Y * 64);

        if (wateringCan != null)
        {
            _wateringCan = wateringCan;
        }
        else
        {
            _wateringCan = new WateringCan { WaterLeft = CompanionActorState.DefaultMaxWater };
        }

        if (hoe != null)
        {
            _hoe = hoe;
        }

        if (_gameFarmer != null)
        {
            _gameFarmer.Name = "Companion";
            _gameFarmer.Position = _pixelPosition;
            _gameFarmer.faceDirection((int)_facing);
            if (!_gameFarmer.Items.Contains(_wateringCan))
            {
                _gameFarmer.Items.Add(_wateringCan);
                _gameFarmer.CurrentToolIndex = _gameFarmer.Items.IndexOf(_wateringCan);
            }
            if (_hoe != null && !_gameFarmer.Items.Contains(_hoe))
            {
                _gameFarmer.Items.Add(_hoe);
            }
        }
    }

    public void SetActiveTask(string? taskId)
    {
        lock (_lock)
        {
            _activeTaskId = taskId;
        }
    }

    public void MovePixels(float dx, float dy)
    {
        lock (_lock)
        {
            var nextPos = PixelPosition + new Vector2(dx, dy);
            PixelPosition = nextPos;

            // Determine facing direction from movement delta
            if (Math.Abs(dx) >= Math.Abs(dy))
            {
                if (dx > 0) Facing = FacingDirection.Right;
                else if (dx < 0) Facing = FacingDirection.Left;
            }
            else
            {
                if (dy > 0) Facing = FacingDirection.Down;
                else if (dy < 0) Facing = FacingDirection.Up;
            }

            if (_gameFarmer != null)
            {
                _gameFarmer.SetMovingDown(dy > 0);
                _gameFarmer.SetMovingUp(dy < 0);
                _gameFarmer.SetMovingRight(dx > 0);
                _gameFarmer.SetMovingLeft(dx < 0);
            }
        }
    }

    public void Face(FacingDirection dir)
    {
        Facing = dir;
    }

    public void Halt()
    {
        lock (_lock)
        {
            if (_isUsingTool)
            {
                _isUsingTool = false;
                _animPhase = ToolAnimationPhase.None;
            }

            if (_gameFarmer != null)
            {
                _gameFarmer.FarmerSprite?.StopAnimation();
                _gameFarmer.UsingTool = false;
                _gameFarmer.canReleaseTool = true;
                _gameFarmer.Halt();
            }
        }
    }

    public void BeginUsingTool()
    {
        lock (_lock)
        {
            if (_gameFarmer == null)
            {
                throw new InvalidOperationException("Cannot begin tool usage: GameFarmer is not initialized.");
            }
            if (_wateringCan == null)
            {
                throw new InvalidOperationException("Cannot begin tool usage: WateringCan is not initialized.");
            }
            if (_gameFarmer.currentLocation == null)
            {
                throw new InvalidOperationException("Cannot begin tool usage: GameFarmer current location is null.");
            }
            if (_wateringCan.WaterLeft <= 0)
            {
                throw new InvalidOperationException("Cannot begin tool usage: WateringCan has no water remaining.");
            }
            if (_gameFarmer.FarmerSprite == null)
            {
                throw new InvalidOperationException("Cannot begin tool usage: GameFarmer FarmerSprite is null.");
            }

            _isUsingTool = true;
            _animPhase = ToolAnimationPhase.Windup;
            _lastAnimIndex = -1;

            int baseFrame = GetNativeBaseFrame(Facing);
            _currentFrame = baseFrame;

            _gameFarmer.CanMove = false;
            _gameFarmer.UsingTool = true;
            _gameFarmer.canReleaseTool = false;

            // Ensure current tool index resolves to the equipped WateringCan
            int canSlot = _gameFarmer.Items.IndexOf(_wateringCan);
            if (canSlot >= 0)
            {
                _gameFarmer.CurrentToolIndex = canSlot;
            }

            // Execute native tool begin/end call chain to initialize FarmerSprite.animateOnce
            // Note: WateringCan.endUsing calls FarmerSprite.animateOnce with null callback (no Game1.toolAnimationDone)
            _wateringCan.beginUsing(_gameFarmer.currentLocation, (int)_pixelPosition.X, (int)_pixelPosition.Y, _gameFarmer);
            _wateringCan.endUsing(_gameFarmer.currentLocation, _gameFarmer);

            // Detached-farmer adaptation: the native watering frames carry static behaviors
            // (Farmer.showToolSwipeEffect / Farmer.useTool / Farmer.canMoveNow) which
            // AnimatedSprite.animateOnce(GameTime) invokes with a NULL farmer, causing an NRE
            // for our detached companion Farmer. Farmer.useTool would also double-apply the
            // physical effect owned by NormalWateringCanAdapter. Strip the behaviors only:
            // frame timing/indices stay native; the physical effect stays with the adapter.
            var toolFrames = _gameFarmer.FarmerSprite.CurrentAnimation;
            if (toolFrames != null)
            {
                for (int i = 0; i < toolFrames.Count; i++)
                {
                    var frame = toolFrames[i];
                    frame.frameStartBehavior = null;
                    frame.frameEndBehavior = null;
                    toolFrames[i] = frame;
                }
            }
            Log($"Tool animation started: {toolFrames?.Count ?? 0} native frames, frame behaviors detached (physical effect handled by adapter).");

            // The native loop interval is 0 for this animation (advance every tick, ~66ms/loop).
            // Restore human-like pacing so the pour is visibly distinguishable on real frames.
            if (_gameFarmer.FarmerSprite.interval <= 0f)
            {
                _gameFarmer.FarmerSprite.interval = 125f;
            }

            _currentFrame = _gameFarmer.FarmerSprite.currentFrame;
        }
    }

    public ToolAnimationPhase UpdateToolAnimation(GameTime? time, long tickCount)
    {
        lock (_lock)
        {
            if (!_isUsingTool)
                return ToolAnimationPhase.None;

            if (_gameFarmer == null || _gameFarmer.FarmerSprite == null)
            {
                throw new InvalidOperationException("Cannot update tool animation: GameFarmer or FarmerSprite is null.");
            }

            // Native Stardew Valley lifecycle: advance the FarmerSprite animation.
            // Note: the native watering animation LOOPS (loopThisAnimation=true), it never
            // clears CurrentAnimation by itself — the human path stops on button release.
            // Completion for our one-shot pour is detected below via frame-index wrap.
            var gameTime = time ?? new GameTime(TimeSpan.Zero, TimeSpan.FromMilliseconds(16.6667));
            _gameFarmer.FarmerSprite.animateOnce(gameTime);

            var sprite = _gameFarmer.FarmerSprite;
            _currentFrame = sprite.currentFrame;
            int animIndex = sprite.currentAnimationIndex;
            bool isOnToolAnim = sprite.isOnToolAnimation();

            int frameCount = sprite.CurrentAnimation?.Count ?? 0;
            bool completedOnePass = frameCount > 0 && _lastAnimIndex >= frameCount - 1 && animIndex < _lastAnimIndex;
            _lastAnimIndex = animIndex;

            if (!isOnToolAnim || sprite.CurrentAnimation == null || completedOnePass)
            {
                _animPhase = ToolAnimationPhase.Completed;
            }
            else if (animIndex == 0)
            {
                _animPhase = ToolAnimationPhase.Windup;
            }
            else if (animIndex == 1)
            {
                // Physical tool effect point: water pours from spout
                _animPhase = ToolAnimationPhase.EffectPoint;
            }
            else
            {
                _animPhase = ToolAnimationPhase.FollowThrough;
            }

            return _animPhase;
        }
    }

    public void EndUsingTool()
    {
        lock (_lock)
        {
            _isUsingTool = false;
            _animPhase = ToolAnimationPhase.None;

            if (_gameFarmer != null)
            {
                _gameFarmer.FarmerSprite?.StopAnimation();
                _gameFarmer.UsingTool = false;
                _gameFarmer.canReleaseTool = true;
                _gameFarmer.Halt();
            }
        }
    }

    private static int GetNativeBaseFrame(FacingDirection facing)
    {
        return facing switch
        {
            FacingDirection.Up => 180,
            FacingDirection.Right => 172,
            FacingDirection.Down => 164,
            FacingDirection.Left => 188,
            _ => 164
        };
    }


    public void SetLocation(string locationName, TileCoordinate tile)
    {
        lock (_lock)
        {
            _locationName = locationName;
            PixelPosition = new Vector2(tile.X * 64, tile.Y * 64);
            if (_gameFarmer != null)
            {
                try
                {
                    var loc = Game1.getLocationFromName(locationName);
                    if (loc != null)
                    {
                        _gameFarmer.currentLocation = loc;
                    }
                }
                catch
                {
                    // Headless test or pre-load safe guard
                }
            }
        }
    }

    public void SetLocation(GameLocation location, TileCoordinate tile)
    {
        ArgumentNullException.ThrowIfNull(location);
        lock (_lock)
        {
            _locationName = !string.IsNullOrWhiteSpace(location.NameOrUniqueName)
                ? location.NameOrUniqueName
                : location.Name;
            PixelPosition = new Vector2(tile.X * 64, tile.Y * 64);
            if (_gameFarmer != null)
            {
                _gameFarmer.currentLocation = location;
            }
        }
    }

    private const int DefaultInventoryCapacity = 36;
    private const int MaxStackPerSlot = 999;

    public int InventoryCapacity
    {
        get
        {
            lock (_lock)
            {
                return _gameFarmer?.MaxItems ?? DefaultInventoryCapacity;
            }
        }
    }

    public int FreeInventorySlots
    {
        get
        {
            lock (_lock)
            {
                int capacity = InventoryCapacity;
                if (_gameFarmer != null)
                {
                    int occupied = 0;
                    int bound = Math.Min(capacity, _gameFarmer.Items.Count);
                    for (int slot = 0; slot < bound; slot++)
                    {
                        if (_gameFarmer.Items[slot] != null) occupied++;
                    }
                    return Math.Max(0, capacity - occupied);
                }
                return Math.Max(0, capacity - _inventory.Count(i => !string.IsNullOrEmpty(i.ItemId) && i.SlotIndex >= 0 && i.SlotIndex < capacity));
            }
        }
    }

    public IReadOnlyList<InventoryItem> GetInventorySnapshot()
    {
        lock (_lock)
        {
            var snapshot = new List<InventoryItem>();
            if (_gameFarmer != null)
            {
                int bound = Math.Min(InventoryCapacity, _gameFarmer.Items.Count);
                for (int slot = 0; slot < bound; slot++)
                {
                    var item = _gameFarmer.Items[slot];
                    if (item is null) continue;
                    snapshot.Add(new InventoryItem(
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
                    ));
                }
            }
            else
            {
                int capacity = InventoryCapacity;
                snapshot.AddRange(_inventory
                    .Where(i => !string.IsNullOrEmpty(i.ItemId) && i.SlotIndex >= 0 && i.SlotIndex < capacity)
                    .OrderBy(i => i.SlotIndex)
                    .Select(i => i with { }));
            }
            return snapshot;
        }
    }

    public bool TryAddItemToInventory(Item gameItem)
    {
        ArgumentNullException.ThrowIfNull(gameItem);
        if (gameItem.Stack <= 0)
            return false;

        lock (_lock)
        {
            if (_gameFarmer != null)
            {
                // Merge into existing compatible stacks first (genuine stacking rules).
                foreach (var existing in _gameFarmer.Items)
                {
                    if (existing is null || !existing.canStackWith(gameItem)) continue;
                    int leftover = existing.addToStack(gameItem);
                    if (leftover <= 0)
                        return true;
                    gameItem.Stack = leftover;
                }

                // Occupy the lowest free slot within capacity.
                int capacity = InventoryCapacity;
                for (int slot = 0; slot < capacity; slot++)
                {
                    if (slot < _gameFarmer.Items.Count)
                    {
                        if (_gameFarmer.Items[slot] is null)
                        {
                            _gameFarmer.Items[slot] = gameItem;
                            return true;
                        }
                    }
                    else
                    {
                        _gameFarmer.Items.Add(gameItem);
                        return true;
                    }
                }
                return false;
            }

            // Offline path: convert to InventoryItem and add
            var invItem = new InventoryItem(
                gameItem.QualifiedItemId,
                !string.IsNullOrEmpty(gameItem.DisplayName) ? gameItem.DisplayName : gameItem.Name,
                gameItem.Stack,
                gameItem.Quality
            );
            return TryAddItemToInventory(invItem);
        }
    }

    public bool TryAddItemToInventory(InventoryItem item)
    {
        ArgumentNullException.ThrowIfNull(item);
        if (string.IsNullOrEmpty(item.ItemId) || item.Stack <= 0)
            return false;

        lock (_lock)
        {
            if (_gameFarmer != null)
            {
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
                    return false;

                return TryAddItemToInventory(gameItem);
            }

            // Offline path: merge into matching stacks, then lowest free slot.
            int capacityOffline = InventoryCapacity;
            int remaining = item.Stack;
            for (int i = 0; i < _inventory.Count && remaining > 0; i++)
            {
                var existing = _inventory[i];
                if (string.IsNullOrEmpty(existing.ItemId)) continue;
                if (existing.ItemId != item.ItemId || existing.Quality != item.Quality) continue;
                int moved = Math.Min(MaxStackPerSlot - existing.Stack, remaining);
                if (moved > 0)
                {
                    _inventory[i] = existing with { Stack = existing.Stack + moved };
                    remaining -= moved;
                }
            }

            if (remaining > 0)
            {
                if (remaining > MaxStackPerSlot)
                    return false;
                var occupied = new HashSet<int>(_inventory
                    .Where(i => !string.IsNullOrEmpty(i.ItemId) && i.SlotIndex >= 0 && i.SlotIndex < capacityOffline)
                    .Select(i => i.SlotIndex));
                int target = -1;
                for (int slot = 0; slot < capacityOffline; slot++)
                {
                    if (!occupied.Contains(slot)) { target = slot; break; }
                }
                if (target < 0)
                    return false;
                _inventory.Add(item with { Stack = remaining, SlotIndex = target });
            }

            return true;
        }
    }

    public bool TryRemoveItemAtSlot(int slotIndex, int stackToRemove, out InventoryItem? removed)
    {
        removed = null;
        if (stackToRemove <= 0)
            return false;

        lock (_lock)
        {
            if (_gameFarmer != null)
            {
                if (slotIndex < 0 || slotIndex >= _gameFarmer.Items.Count)
                    return false;
                var item = _gameFarmer.Items[slotIndex];
                if (item is null)
                    return false;

                int removedStack = Math.Min(stackToRemove, item.Stack);
                removed = new InventoryItem(
                    itemId: item.QualifiedItemId ?? item.ItemId ?? item.Name,
                    name: item.Name,
                    stack: removedStack,
                    quality: item.Quality,
                    category: item.getCategoryName() ?? "",
                    upgradeLevel: item is Tool tool ? tool.UpgradeLevel : 0,
                    waterLeft: item is WateringCan can ? can.WaterLeft : 0,
                    slotIndex: slotIndex,
                    isTool: item is Tool
                );

                if (stackToRemove >= item.Stack)
                {
                    _gameFarmer.Items[slotIndex] = null;
                }
                else
                {
                    item.Stack -= stackToRemove;
                }
                return true;
            }

            int index = _inventory.FindIndex(i => !string.IsNullOrEmpty(i.ItemId) && i.SlotIndex == slotIndex);
            if (index < 0)
                return false;

            var existingOffline = _inventory[index];
            int removedStackOffline = Math.Min(stackToRemove, existingOffline.Stack);
            removed = existingOffline with { Stack = removedStackOffline };
            if (stackToRemove >= existingOffline.Stack)
            {
                _inventory.RemoveAt(index);
            }
            else
            {
                _inventory[index] = existingOffline with { Stack = existingOffline.Stack - stackToRemove };
            }
            return true;
        }
    }

    public static bool ItemMatchesId(Item item, string targetItemId)
    {
        if (item == null || string.IsNullOrWhiteSpace(targetItemId)) return false;
        string id = item.QualifiedItemId ?? item.ItemId ?? item.Name;
        if (string.Equals(id, targetItemId, StringComparison.OrdinalIgnoreCase)) return true;
        if (string.Equals(item.ItemId, targetItemId, StringComparison.OrdinalIgnoreCase)) return true;
        if (string.Equals(item.Name, targetItemId, StringComparison.OrdinalIgnoreCase)) return true;

        string targetUnqualified = targetItemId.StartsWith("(") && targetItemId.Contains(')')
            ? targetItemId.Substring(targetItemId.IndexOf(')') + 1)
            : targetItemId;
        if (string.Equals(item.ItemId, targetUnqualified, StringComparison.OrdinalIgnoreCase)) return true;

        if (!targetItemId.StartsWith("(") && string.Equals(item.QualifiedItemId, $"(O){targetItemId}", StringComparison.OrdinalIgnoreCase)) return true;

        return false;
    }

    public static bool ItemIdMatches(string inventoryItemId, string targetItemId)
    {
        if (string.Equals(inventoryItemId, targetItemId, StringComparison.OrdinalIgnoreCase))
            return true;
        string normInv = inventoryItemId.StartsWith("(") && inventoryItemId.Contains(')')
            ? inventoryItemId.Substring(inventoryItemId.IndexOf(')') + 1)
            : inventoryItemId;
        string normTarget = targetItemId.StartsWith("(") && targetItemId.Contains(')')
            ? targetItemId.Substring(targetItemId.IndexOf(')') + 1)
            : targetItemId;
        return string.Equals(normInv, normTarget, StringComparison.OrdinalIgnoreCase);
    }

    public bool TryConsumeItem(string itemId, int count = 1)
    {
        lock (_lock)
        {
            if (count <= 0) return true;
            int total = GetItemCount(itemId);
            if (total < count) return false;

            int needed = count;
            if (_gameFarmer != null)
            {
                for (int i = 0; i < _gameFarmer.Items.Count && needed > 0; i++)
                {
                    var item = _gameFarmer.Items[i];
                    if (item == null) continue;
                    if (ItemMatchesId(item, itemId))
                    {
                        if (item.Stack > needed)
                        {
                            item.Stack -= needed;
                            needed = 0;
                        }
                        else
                        {
                            needed -= item.Stack;
                            _gameFarmer.Items[i] = null;
                        }
                    }
                }
                return true;
            }
            else
            {
                for (int i = 0; i < _inventory.Count && needed > 0; i++)
                {
                    var item = _inventory[i];
                    if (ItemIdMatches(item.ItemId, itemId))
                    {
                        if (item.Stack > needed)
                        {
                            _inventory[i] = item with { Stack = item.Stack - needed };
                            needed = 0;
                        }
                        else
                        {
                            needed -= item.Stack;
                            _inventory.RemoveAt(i);
                            i--;
                        }
                    }
                }
                return true;
            }
        }
    }

    public bool TryExtractItem(string itemId, int count, out List<Item> extractedItems)
    {
        lock (_lock)
        {
            extractedItems = new List<Item>();
            if (count <= 0) return true;
            int total = GetItemCount(itemId);
            if (total < count) return false;

            int needed = count;
            if (_gameFarmer != null)
            {
                for (int i = 0; i < _gameFarmer.Items.Count && needed > 0; i++)
                {
                    var item = _gameFarmer.Items[i];
                    if (item == null) continue;
                    if (ItemMatchesId(item, itemId))
                    {
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
                            _gameFarmer.Items[i] = null;
                        }
                    }
                }
                return true;
            }
            else
            {
                for (int i = 0; i < _inventory.Count && needed > 0; i++)
                {
                    var item = _inventory[i];
                    if (ItemIdMatches(item.ItemId, itemId))
                    {
                        if (item.Stack > needed)
                        {
                            _inventory[i] = item with { Stack = item.Stack - needed };
                            needed = 0;
                        }
                        else
                        {
                            needed -= item.Stack;
                            _inventory.RemoveAt(i);
                            i--;
                        }
                    }
                }
                return true;
            }
        }
    }

    public int GetItemCount(string itemId)
    {
        lock (_lock)
        {
            if (_gameFarmer != null)
            {
                int total = 0;
                foreach (var item in _gameFarmer.Items)
                {
                    if (item == null) continue;
                    if (ItemMatchesId(item, itemId))
                    {
                        total += item.Stack;
                    }
                }
                return total;
            }
            else
            {
                return _inventory
                    .Where(i => ItemIdMatches(i.ItemId, itemId))
                    .Sum(i => i.Stack);
            }
        }
    }

    public void ApplyPersistentState(CompanionActorState state)
    {
        ArgumentNullException.ThrowIfNull(state);
        lock (_lock)
        {
            _maxStamina = (int)state.MaxStamina;
            Stamina = state.Stamina;
            _maxWater = state.MaxWater;
            _locationName = state.Pose.LocationName;
            PixelPosition = new Vector2(state.Pose.Tile.X * 64, state.Pose.Tile.Y * 64);
            Facing = state.Pose.Facing;
            _inventory = state.Inventory.Select(i => i with { }).ToList();

            // Populate actual Farmer.Items from persisted state atomically
            if (_gameFarmer != null)
            {
                var targetSlots = new Item?[36];
                bool wateringCanRestored = false;

                for (int i = 0; i < state.Inventory.Count; i++)
                {
                    var inv = state.Inventory[i];
                    int targetSlot = (inv.SlotIndex >= 0 && inv.SlotIndex < 36) ? inv.SlotIndex : i;
                    if (targetSlot >= 36) continue;

                    if (string.IsNullOrEmpty(inv.ItemId))
                    {
                        targetSlots[targetSlot] = null;
                        continue;
                    }

                    Item? restoredItem = null;
                    if (inv.ItemId.Contains("WateringCan") || inv.Name.Contains("Watering Can"))
                    {
                        // Preserve zero water exactly: do not convert 0 to state.Water!
                        var can = new WateringCan { WaterLeft = inv.WaterLeft };
                        can.UpgradeLevel = inv.UpgradeLevel;
                        restoredItem = can;
                        if (!wateringCanRestored)
                        {
                            _wateringCan = can;
                            wateringCanRestored = true;
                        }
                    }
                    else
                    {
                        try
                        {
                            restoredItem = ItemRegistry.Create(inv.ItemId, inv.Stack, inv.Quality);
                            if (restoredItem is Tool t && inv.UpgradeLevel > 0)
                            {
                                t.UpgradeLevel = inv.UpgradeLevel;
                            }
                        }
                        catch (Exception ex)
                        {
                            // Atomic fail-closed: reject corrupted/invalid state
                            throw new InvalidOperationException(
                                $"Atomic state restoration failed: Unable to create item '{inv.ItemId}' at slot {targetSlot}: {ex.Message}", ex);
                        }

                        if (restoredItem == null)
                        {
                            throw new InvalidOperationException(
                                $"Atomic state restoration failed: ItemRegistry returned null for item '{inv.ItemId}' at slot {targetSlot}.");
                        }
                    }

                    targetSlots[targetSlot] = restoredItem;
                }

                // If watering can was not in inventory, restore our active watering can
                if (!wateringCanRestored)
                {
                    if (_wateringCan == null)
                    {
                        _wateringCan = new WateringCan { WaterLeft = state.Water };
                    }
                    targetSlots[0] = _wateringCan;
                }

                // Atomic commit to _gameFarmer.Items
                _gameFarmer.Items.Clear();
                for (int s = 0; s < 36; s++)
                {
                    _gameFarmer.Items.Add(targetSlots[s]);
                }

                // Ensure current tool index points to the watering can
                for (int slot = 0; slot < _gameFarmer.Items.Count; slot++)
                {
                    if (_gameFarmer.Items[slot] is WateringCan)
                    {
                        _gameFarmer.CurrentToolIndex = slot;
                        _wateringCan = (WateringCan)_gameFarmer.Items[slot]!;
                        break;
                    }
                }

                // Exact water consistency
                WaterLeft = _wateringCan != null ? _wateringCan.WaterLeft : state.Water;
                _hoe = _gameFarmer.Items.OfType<Hoe>().FirstOrDefault();
            }
            else
            {
                // In offline/test mode without GameFarmer:
                var canInv = state.Inventory.FirstOrDefault(i =>
                    !string.IsNullOrEmpty(i.ItemId) &&
                    (i.ItemId.Contains("WateringCan") || i.Name.Contains("Watering Can")));

                int resolvedWater = canInv != null ? canInv.WaterLeft : state.Water;
                if (_wateringCan != null)
                {
                    _wateringCan.WaterLeft = resolvedWater;
                }
                WaterLeft = resolvedWater;

                var hoeInv = state.Inventory.FirstOrDefault(i =>
                    !string.IsNullOrEmpty(i.ItemId) &&
                    (i.ItemId.Contains("Hoe") || i.Name.Contains("Hoe")));
                if (hoeInv != null)
                {
                    if (_hoe == null) _hoe = new Hoe { UpgradeLevel = hoeInv.UpgradeLevel };
                    else _hoe.UpgradeLevel = hoeInv.UpgradeLevel;
                }
                else
                {
                    _hoe = null;
                }
            }

            // CRITICAL: reload preserves stamina/water/items and NEVER auto-resumes stale task
            _activeTaskId = null;
        }
    }

    public CompanionActorState CapturePersistentState()
    {
        lock (_lock)
        {
            var inventoryToPersist = new List<InventoryItem>();
            if (_gameFarmer != null)
            {
                for (int slot = 0; slot < 36; slot++)
                {
                    Item? item = slot < _gameFarmer.Items.Count ? _gameFarmer.Items[slot] : null;
                    if (item == null)
                    {
                        // Explicitly capture empty slot to preserve 0..35 slot identity
                        inventoryToPersist.Add(new InventoryItem
                        {
                            SlotIndex = slot,
                            ItemId = "",
                            Name = "",
                            Stack = 0
                        });
                        continue;
                    }

                    int upgradeLevel = 0;
                    int waterLeft = 0;
                    if (item is Tool tool)
                    {
                        upgradeLevel = tool.UpgradeLevel;
                        if (tool is WateringCan can)
                        {
                            waterLeft = can.WaterLeft;
                        }
                    }

                    inventoryToPersist.Add(new InventoryItem(
                        itemId: item.QualifiedItemId ?? item.ItemId ?? item.Name,
                        name: item.Name,
                        stack: item.Stack,
                        quality: item.Quality,
                        category: item.getCategoryName() ?? "",
                        upgradeLevel: upgradeLevel,
                        waterLeft: waterLeft,
                        slotIndex: slot,
                        isTool: item is Tool,
                        displayName: item.DisplayName ?? item.Name
                    ));
                }
            }
            else
            {
                for (int i = 0; i < _inventory.Count; i++)
                {
                    var item = _inventory[i];
                    int slot = item.SlotIndex >= 0 ? item.SlotIndex : i;
                    inventoryToPersist.Add(item with { SlotIndex = slot });
                }
            }

            return new CompanionActorState
            {
                Version = CompanionActorState.CurrentSchemaVersion,
                CompanionId = CompanionId,
                Stamina = Stamina,
                MaxStamina = MaxStamina,
                Water = WaterLeft,
                MaxWater = MaxWater,
                Pose = new AuthoritativePose(LocationName, Tile, Facing),
                Inventory = inventoryToPersist,
                LastSavedUtc = DateTime.UtcNow.ToString("o"),
                PersistedTaskId = _activeTaskId
            };
        }
    }
}
