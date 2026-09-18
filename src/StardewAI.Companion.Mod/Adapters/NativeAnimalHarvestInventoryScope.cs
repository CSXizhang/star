using System.Reflection;
using System.Reflection.Emit;
using HarmonyLib;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Tools;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Native MilkPail/Shears create and insert their own produce, but Farmer's boolean
/// insertion entry rejects detached farmers before touching their inventory.
/// Permit only the exact companion during equipped tool harvest or pickup of
/// the exact collectible egg still present in its current animal house.
/// No player swapping, identity changes, item creation or inventory replacement.
/// </summary>
internal sealed class NativeAnimalHarvestInventoryScope : IDisposable
{
    private static readonly object PatchLock = new();
    private static bool _patched;
    [ThreadStatic] private static Farmer? _receiver;
    [ThreadStatic] private static Tool? _tool;
    [ThreadStatic] private static AnimalHouse? _house;
    [ThreadStatic] private static StardewValley.Object? _egg;
    [ThreadStatic] private static Vector2 _tile;
    [ThreadStatic] private static GameLocation? _debrisLocation;
    [ThreadStatic] private static Debris? _debris;
    [ThreadStatic] private static Chunk? _chunk;
    private bool _disposed;

    public NativeAnimalHarvestInventoryScope(Farmer receiver, Tool tool)
    {
        if (!IsSupportedTool(tool) || _receiver is not null || ReferenceEquals(receiver, Game1.player)
            || !ReferenceEquals(receiver.CurrentTool, tool) || !receiver.Items.Contains(tool))
            throw new InvalidOperationException("Invalid or nested companion animal harvest inventory context.");
        EnsurePatched();
        _receiver = receiver;
        _tool = tool;
    }

    public NativeAnimalHarvestInventoryScope(Farmer receiver, AnimalHouse house, Vector2 tile, StardewValley.Object egg)
    {
        if (_receiver is not null || ReferenceEquals(receiver, Game1.player)
            || !ReferenceEquals(receiver.currentLocation, house)
            || !IsGroundEgg(egg) || !ReferenceEquals(house.getObjectAtTile((int)tile.X, (int)tile.Y), egg))
            throw new InvalidOperationException("Invalid or nested companion ground egg inventory context.");
        EnsurePatched();
        _receiver = receiver;
        _house = house;
        _tile = tile;
        _egg = egg;
    }

    public NativeAnimalHarvestInventoryScope(Farmer receiver, GameLocation location, Debris debris, Chunk chunk)
    {
        if (_receiver is not null || ReferenceEquals(receiver, Game1.player)
            || !ReferenceEquals(receiver.currentLocation, location)
            || !IsOrdinaryDebris(debris) || !location.debris.Contains(debris) || !debris.Chunks.Contains(chunk))
            throw new InvalidOperationException("Invalid companion native debris inventory context.");
        EnsurePatched();
        _receiver = receiver;
        _debrisLocation = location;
        _debris = debris;
        _chunk = chunk;
    }

    internal static bool IsOrdinaryDebris(Debris debris) =>
        debris.debrisType.Value is Debris.DebrisType.OBJECT or Debris.DebrisType.RESOURCE
        && (debris.item is not null || !string.IsNullOrWhiteSpace(debris.itemId.Value));

    internal static bool IsGroundEgg(StardewValley.Object obj) => obj.CanBeGrabbed && obj.Category == StardewValley.Object.EggCategory;

    private static void EnsurePatched()
    {
        lock (PatchLock)
        {
            if (!_patched)
            {
                var method = AccessTools.Method(typeof(Farmer), nameof(Farmer.addItemToInventoryBool),
                    new[] { typeof(Item), typeof(bool) });
                new Harmony("StardewAI.Companion.NativeMilkInventory").Patch(method,
                    transpiler: new HarmonyMethod(typeof(NativeAnimalHarvestInventoryScope), nameof(Transpile)));
                _patched = true;
            }
        }
    }

    private static bool IsSupportedTool(Tool tool) => tool is MilkPail or Shears;

    private static bool IsNativeOrScopedReceiver(Farmer farmer) => farmer.IsLocalPlayer
        || (_receiver is not null && ReferenceEquals(farmer, _receiver)
            && !ReferenceEquals(farmer, Game1.player)
            && ((_tool is not null && IsSupportedTool(_tool)
                 && ReferenceEquals(farmer.CurrentTool, _tool) && farmer.Items.Contains(_tool))
                || (_house is not null && _egg is not null && IsGroundEgg(_egg)
                    && ReferenceEquals(farmer.currentLocation, _house)
                    && ReferenceEquals(_house.getObjectAtTile((int)_tile.X, (int)_tile.Y), _egg))
                || (_debrisLocation is not null && _debris is not null && _chunk is not null
                    && ReferenceEquals(farmer.currentLocation, _debrisLocation)
                    && _debrisLocation.debris.Contains(_debris) && _debris.Chunks.Contains(_chunk))));

    // Replace precisely the one ownership gate. The entire native insertion body,
    // capacity rules, returned bool and all animal state changes remain native.
    private static IEnumerable<CodeInstruction> Transpile(IEnumerable<CodeInstruction> instructions)
    {
        var code = instructions.ToList();
        MethodInfo gate = AccessTools.PropertyGetter(typeof(Farmer), nameof(Farmer.IsLocalPlayer));
        var matches = code.Where(i => i.Calls(gate)).ToList();
        if (matches.Count != 1)
            throw new InvalidOperationException($"Unsupported native inventory ownership gate count: {matches.Count}.");
        matches[0].opcode = OpCodes.Call;
        matches[0].operand = AccessTools.Method(typeof(NativeAnimalHarvestInventoryScope), nameof(IsNativeOrScopedReceiver));
        return code;
    }

    public void Dispose()
    {
        if (_disposed) return;
        _receiver = null;
        _tool = null;
        _house = null;
        _egg = null;
        _tile = default;
        _debrisLocation = null;
        _debris = null;
        _chunk = null;
        _disposed = true;
    }
}
