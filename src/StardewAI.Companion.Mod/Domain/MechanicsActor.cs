using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Tools;

namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// Independent Mechanics Actor responsible for physical execution, independent
/// stamina, water capacity, inventory, and authoritative spatial pose.
/// Can be used as a pure domain model and as a test double implementing IFarmerActor.
/// </summary>
public class MechanicsActor : IFarmerActor
{
    private readonly object _lock = new();
    private float _stamina;
    private float _maxStamina;
    private Vector2 _pixelPosition;

    public string CompanionId { get; }
    public float Stamina
    {
        get => _stamina;
        set
        {
            lock (_lock)
            {
                _stamina = Math.Clamp(value, 0f, _maxStamina);
            }
        }
    }
    public int MaxStamina => (int)_maxStamina;
    public int WaterLeft
    {
        get => Water;
        set
        {
            lock (_lock)
            {
                Water = Math.Clamp(value, 0, MaxWater);
            }
        }
    }
    public int Water { get; private set; }
    public int MaxWater { get; private set; }
    public AuthoritativePose Pose { get; private set; }
    public List<InventoryItem> Inventory { get; private set; }

    public string LocationName => Pose.LocationName;
    public Vector2 PixelPosition
    {
        get => _pixelPosition;
        set
        {
            lock (_lock)
            {
                _pixelPosition = value;
                Pose = new AuthoritativePose(Pose.LocationName, new TileCoordinate((int)(value.X / 64f), (int)(value.Y / 64f)), Pose.Facing);
            }
        }
    }
    public TileCoordinate Tile => Pose.Tile;
    public FacingDirection Facing
    {
        get => Pose.Facing;
        set
        {
            lock (_lock)
            {
                Pose = new AuthoritativePose(Pose.LocationName, Pose.Tile, value);
            }
        }
    }

    public WateringCan? WateringCan => null;
    public Hoe? Hoe { get; set; }
    public Farmer? GameFarmer => null;

    /// <summary>
    /// Offline/test tool resolution over the persisted inventory snapshot. A tool is
    /// only reported when the inventory really holds it; construction prefers the
    /// game registry (authoritative item data) and falls back to the tool's own
    /// parameterless shape, so the offline path stays usable without game content.
    /// </summary>
    public T? FindTool<T>() where T : Tool
    {
        string wanted = typeof(T).Name;
        lock (_lock)
        {
            if (typeof(T) == typeof(Hoe) && Hoe is T hoe)
                return hoe;

            string? itemId = Inventory.FirstOrDefault(i =>
                !string.IsNullOrEmpty(i.ItemId) && i.IsTool &&
                i.ItemId.Contains(wanted, StringComparison.OrdinalIgnoreCase))?.ItemId;
            if (itemId is null)
                return null;

            try
            {
                if (ItemRegistry.Create(itemId, 1, 0) is T created)
                    return created;
            }
            catch
            {
                // Registry-backed construction is unavailable in stripped test hosts.
            }

            try
            {
                if (Activator.CreateInstance(typeof(T)) is T fallback)
                    return fallback;
            }
            catch
            {
                return null;
            }

            return null;
        }
    }

    public IReadOnlyList<string> GetToolNames()
    {
        lock (_lock)
        {
            var names = Inventory
                .Where(i => i.IsTool && !string.IsNullOrEmpty(i.ItemId))
                .Select(i => i.ItemId.StartsWith("(T)", StringComparison.OrdinalIgnoreCase) ? i.ItemId[3..] : i.ItemId)
                .Distinct()
                .ToList();
            if (Hoe is not null && !names.Contains(nameof(Hoe)))
                names.Add(nameof(Hoe));
            return names;
        }
    }

    private const int MaxStackPerSlot = 999;

    /// <summary>
    /// Total inventory slot capacity (mirrors farmer.MaxItems).
    /// </summary>
    public int MaxItems { get; private set; } = 36;

    public int InventoryCapacity => MaxItems;

    public int FreeInventorySlots
    {
        get
        {
            lock (_lock)
            {
                return MaxItems - OccupiedSlots().Count;
            }
        }
    }

    private HashSet<int> OccupiedSlots()
    {
        var slots = new HashSet<int>();
        foreach (var item in Inventory)
        {
            if (!string.IsNullOrEmpty(item.ItemId) && item.SlotIndex >= 0 && item.SlotIndex < MaxItems)
            {
                slots.Add(item.SlotIndex);
            }
        }
        return slots;
    }

    public IReadOnlyList<InventoryItem> GetInventorySnapshot()
    {
        lock (_lock)
        {
            return Inventory
                .Where(i => !string.IsNullOrEmpty(i.ItemId) && i.SlotIndex >= 0 && i.SlotIndex < MaxItems)
                .OrderBy(i => i.SlotIndex)
                .Select(i => i with { })
                .ToList();
        }
    }

    public bool TryAddItemToInventory(Item gameItem)
    {
        ArgumentNullException.ThrowIfNull(gameItem);
        if (gameItem.Stack <= 0)
            return false;

        return TryAddItemToInventory(new InventoryItem(
            gameItem.QualifiedItemId,
            !string.IsNullOrEmpty(gameItem.DisplayName) ? gameItem.DisplayName : gameItem.Name,
            gameItem.Stack,
            gameItem.Quality
        ));
    }

    public bool TryAddItemToInventory(InventoryItem item)
    {
        ArgumentNullException.ThrowIfNull(item);
        if (string.IsNullOrEmpty(item.ItemId) || item.Stack <= 0)
            return false;

        lock (_lock)
        {
            // Phase 1: verify the full stack can be placed (merge capacity, then one free slot).
            int remaining = item.Stack;
            foreach (var existing in Inventory)
            {
                if (string.IsNullOrEmpty(existing.ItemId)) continue;
                if (existing.ItemId != item.ItemId || existing.Quality != item.Quality) continue;
                remaining -= Math.Min(MaxStackPerSlot - existing.Stack, remaining);
                if (remaining <= 0) break;
            }

            if (remaining > 0)
            {
                if (remaining > MaxStackPerSlot)
                    return false;
                var occupied = OccupiedSlots();
                bool hasFree = false;
                for (int slot = 0; slot < MaxItems && !hasFree; slot++)
                {
                    if (!occupied.Contains(slot)) hasFree = true;
                }
                if (!hasFree)
                    return false;
            }

            // Phase 2: apply merges, then place the remainder in the lowest free slot.
            remaining = item.Stack;
            for (int i = 0; i < Inventory.Count && remaining > 0; i++)
            {
                var existing = Inventory[i];
                if (string.IsNullOrEmpty(existing.ItemId)) continue;
                if (existing.ItemId != item.ItemId || existing.Quality != item.Quality) continue;
                int moved = Math.Min(MaxStackPerSlot - existing.Stack, remaining);
                if (moved > 0)
                {
                    Inventory[i] = existing with { Stack = existing.Stack + moved };
                    remaining -= moved;
                }
            }

            if (remaining > 0)
            {
                var occupied = OccupiedSlots();
                int target = -1;
                for (int slot = 0; slot < MaxItems; slot++)
                {
                    if (!occupied.Contains(slot)) { target = slot; break; }
                }
                Inventory.Add(item with { Stack = remaining, SlotIndex = target });
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
            int index = Inventory.FindIndex(i =>
                !string.IsNullOrEmpty(i.ItemId) && i.SlotIndex == slotIndex);
            if (index < 0)
                return false;

            var existing = Inventory[index];
            int removedStack = Math.Min(stackToRemove, existing.Stack);
            removed = existing with { Stack = removedStack };

            if (stackToRemove >= existing.Stack)
            {
                Inventory.RemoveAt(index);
            }
            else
            {
                Inventory[index] = existing with { Stack = existing.Stack - stackToRemove };
            }

            return true;
        }
    }

    public bool TryConsumeItem(string itemId, int count = 1)
    {
        lock (_lock)
        {
            if (count <= 0) return true;
            int total = GetItemCount(itemId);
            if (total < count) return false;

            int needed = count;
            for (int i = 0; i < Inventory.Count && needed > 0; i++)
            {
                var item = Inventory[i];
                if (string.Equals(item.ItemId, itemId, StringComparison.OrdinalIgnoreCase))
                {
                    if (item.Stack > needed)
                    {
                        Inventory[i] = item with { Stack = item.Stack - needed };
                        needed = 0;
                    }
                    else
                    {
                        needed -= item.Stack;
                        Inventory.RemoveAt(i);
                        i--;
                    }
                }
            }
            return true;
        }
    }

    private static bool ItemIdMatches(string inventoryItemId, string targetItemId)
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

    public bool TryExtractItem(string itemId, int count, out List<Item> extractedItems)
    {
        lock (_lock)
        {
            extractedItems = new List<Item>();
            if (count <= 0) return true;
            int total = GetItemCount(itemId);
            if (total < count) return false;

            int needed = count;
            for (int i = 0; i < Inventory.Count && needed > 0; i++)
            {
                var item = Inventory[i];
                if (ItemIdMatches(item.ItemId, itemId))
                {
                    if (item.Stack > needed)
                    {
                        Inventory[i] = item with { Stack = item.Stack - needed };
                        needed = 0;
                    }
                    else
                    {
                        needed -= item.Stack;
                        Inventory.RemoveAt(i);
                        i--;
                    }
                }
            }
            return true;
        }
    }

    public int GetItemCount(string itemId)
    {
        lock (_lock)
        {
            return Inventory
                .Where(i => ItemIdMatches(i.ItemId, itemId))
                .Sum(i => i.Stack);
        }
    }

    private bool _isUsingTool;
    private int _currentFrame;
    private ToolAnimationPhase _animPhase = ToolAnimationPhase.None;
    private int _toolTicks;

    public bool IsUsingTool => _isUsingTool;
    public int CurrentFrame => _currentFrame;
    public ToolAnimationPhase AnimationPhase => _animPhase;

    /// <summary>
    /// Currently running in-memory task ID (null if idle).
    /// Stale tasks are never auto-resumed on reload.
    /// </summary>
    public string? ActiveTaskId { get; private set; }

    public bool IsExhausted => Stamina <= 0f;
    public bool IsWateringCanEmpty => Water <= 0;


    public MechanicsActor(
        string companionId = "default-companion",
        float stamina = CompanionActorState.DefaultMaxStamina,
        float maxStamina = CompanionActorState.DefaultMaxStamina,
        int water = CompanionActorState.DefaultMaxWater,
        int maxWater = CompanionActorState.DefaultMaxWater,
        AuthoritativePose? initialPose = null,
        IEnumerable<InventoryItem>? initialInventory = null,
        int maxItems = 36)
    {
        CompanionId = companionId;
        _maxStamina = maxStamina;
        _stamina = Math.Clamp(stamina, 0f, maxStamina);
        MaxWater = maxWater;
        Water = Math.Clamp(water, 0, maxWater);
        MaxItems = maxItems > 0 ? maxItems : 36;
        Pose = initialPose ?? new AuthoritativePose("Farm", 64, 15, FacingDirection.Down);
        _pixelPosition = new Vector2(Pose.Tile.X * 64, Pose.Tile.Y * 64);
        Inventory = initialInventory?.ToList() ?? new List<InventoryItem>();
        ActiveTaskId = null;
    }

    /// <summary>
    /// Atomically attempt to consume stamina.
    /// </summary>
    public bool TryConsumeStamina(float amount, out string? failureReason)
    {
        lock (_lock)
        {
            if (amount < 0)
            {
                failureReason = "Cannot consume negative stamina.";
                return false;
            }
            if (Stamina < amount)
            {
                failureReason = $"Insufficient stamina: available {Stamina:F1}, requested {amount:F1}.";
                return false;
            }
            Stamina -= amount;
            failureReason = null;
            return true;
        }
    }

    /// <summary>
    /// Atomically attempt to consume water from the watering can.
    /// </summary>
    public bool TryConsumeWater(int amount, out string? failureReason)
    {
        lock (_lock)
        {
            if (amount < 0)
            {
                failureReason = "Cannot consume negative water.";
                return false;
            }
            if (Water < amount)
            {
                failureReason = $"Watering can is empty or has insufficient water: available {Water}, requested {amount}.";
                return false;
            }
            Water -= amount;
            failureReason = null;
            return true;
        }
    }

    /// <summary>
    /// Updates the actor's authoritative spatial pose.
    /// </summary>
    public void UpdatePose(string locationName, TileCoordinate tile, FacingDirection facing)
    {
        lock (_lock)
        {
            Pose = new AuthoritativePose(locationName, tile, facing);
            _pixelPosition = new Vector2(tile.X * 64, tile.Y * 64);
        }
    }

    /// <summary>
    /// Updates the actor's authoritative spatial pose.
    /// </summary>
    public void UpdatePose(AuthoritativePose newPose)
    {
        ArgumentNullException.ThrowIfNull(newPose);
        lock (_lock)
        {
            Pose = newPose;
            _pixelPosition = new Vector2(newPose.Tile.X * 64, newPose.Tile.Y * 64);
        }
    }

    public void MovePixels(float dx, float dy)
    {
        lock (_lock)
        {
            var nextPos = _pixelPosition + new Vector2(dx, dy);
            _pixelPosition = nextPos;
            FacingDirection newFacing = Pose.Facing;
            if (Math.Abs(dx) >= Math.Abs(dy))
            {
                if (dx > 0) newFacing = FacingDirection.Right;
                else if (dx < 0) newFacing = FacingDirection.Left;
            }
            else
            {
                if (dy > 0) newFacing = FacingDirection.Down;
                else if (dy < 0) newFacing = FacingDirection.Up;
            }
            Pose = new AuthoritativePose(Pose.LocationName, new TileCoordinate((int)(nextPos.X / 64f), (int)(nextPos.Y / 64f)), newFacing);
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
                _toolTicks = 0;
            }
        }
    }

    public virtual void BeginUsingTool()
    {
        lock (_lock)
        {
            _isUsingTool = true;
            _toolTicks = 0;
            _animPhase = ToolAnimationPhase.Windup;

            int baseFrame = Facing switch
            {
                FacingDirection.Up => 176,
                FacingDirection.Right => 168,
                FacingDirection.Down => 160,
                FacingDirection.Left => 184,
                _ => 160
            };
            _currentFrame = baseFrame;
        }
    }

    public virtual ToolAnimationPhase UpdateToolAnimation(GameTime? time, long tickCount)
    {
        lock (_lock)
        {
            if (!_isUsingTool)
                return ToolAnimationPhase.None;

            _toolTicks++;

            int baseFrame = Facing switch
            {
                FacingDirection.Up => 176,
                FacingDirection.Right => 168,
                FacingDirection.Down => 160,
                FacingDirection.Left => 184,
                _ => 160
            };

            if (_toolTicks <= 3)
            {
                _animPhase = ToolAnimationPhase.Windup;
                _currentFrame = baseFrame;
            }
            else if (_toolTicks == 4)
            {
                _animPhase = ToolAnimationPhase.EffectPoint;
                _currentFrame = baseFrame + 1;
            }
            else if (_toolTicks <= 8)
            {
                _animPhase = ToolAnimationPhase.FollowThrough;
                _currentFrame = baseFrame + 2;
            }
            else
            {
                _animPhase = ToolAnimationPhase.Completed;
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
            _toolTicks = 0;
        }
    }


    public void SetLocation(string locationName, TileCoordinate tile)
    {
        UpdatePose(locationName, tile, Facing);
    }

    /// <summary>
    /// Sets the currently active in-memory task.
    /// </summary>
    public void SetActiveTask(string? taskId)
    {
        lock (_lock)
        {
            ActiveTaskId = taskId;
        }
    }

    /// <summary>
    /// Exports the current live state into a persistent state snapshot.
    /// </summary>
    public CompanionActorState ToState()
    {
        lock (_lock)
        {
            return new CompanionActorState
            {
                Version = CompanionActorState.CurrentSchemaVersion,
                CompanionId = CompanionId,
                Stamina = Stamina,
                MaxStamina = MaxStamina,
                Water = Water,
                MaxWater = MaxWater,
                Pose = Pose,
                Inventory = Inventory.Select(i => i with { }).ToList(),
                LastSavedUtc = DateTime.UtcNow.ToString("o"),
                PersistedTaskId = ActiveTaskId
            };
        }
    }

    /// <summary>
    /// Restores the actor from persisted state.
    /// CRITICAL RULE: Reload preserves stamina, water, inventory, and pose,
    /// but NEVER auto-resumes any stale task from disk!
    /// </summary>
    public void ApplyState(CompanionActorState state)
    {
        ArgumentNullException.ThrowIfNull(state);
        lock (_lock)
        {
            _maxStamina = state.MaxStamina;
            Stamina = state.Stamina;
            MaxWater = state.MaxWater;
            Water = state.Water;
            Pose = state.Pose;
            _pixelPosition = new Vector2(state.Pose.Tile.X * 64, state.Pose.Tile.Y * 64);
            Inventory = state.Inventory.Select(i => i with { }).ToList();
            // NEVER auto-resume stale task!
            ActiveTaskId = null;
        }
    }

    public void ApplyPersistentState(CompanionActorState state) => ApplyState(state);
    public CompanionActorState CapturePersistentState() => ToState();
}
