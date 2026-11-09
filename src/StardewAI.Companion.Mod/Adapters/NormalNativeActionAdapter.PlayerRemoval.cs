using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Objects;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Adapters;

public sealed partial class NormalNativeActionAdapter
{
    private (string Key, GameLocation? Location, object? Target, float Health, string? Error)? _playerRemovalBefore;

    private string? PreparePlayerRemoval(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var location = ResolveLocation(request.LocationId);
        var tile = new Vector2(target.Tile.X, target.Tile.Y);
        var obj = location?.getObjectAtTile(target.Tile.X, target.Tile.Y);
        Flooring? floor = location != null && location.terrainFeatures.TryGetValue(tile, out var terrain) ? terrain as Flooring : null;
        string? actual = obj?.QualifiedItemId ?? floor?.GetData()?.ItemId;
        string? error = obj == null && floor == null ? "No supported placed object or flooring."
            : !SameItem(actual, request.ItemId) ? "Target identity changed; observe before removal."
            : obj is Chest || obj?.heldObject.Value != null || obj?.IsTapper() == true || obj?.isSpawnedObject.Value == true ? "Storage, filled machines, tappers and forage are protected."
            : obj != null && !(obj is Fence || obj.bigCraftable.Value || obj.IsSprinkler()) ? "Unsupported removal target."
            : actor.FreeInventorySlots < 1 ? "Keep a free backpack slot for native recovered drops." : null;
        string? tool = ConstructionRemovalTool(actor, obj) switch { Pickaxe => "Pickaxe", Axe => "Axe", _ => null };
        error ??= tool == null ? "A native axe or pickaxe is required." : null;
        _playerRemovalBefore = (ChopKey(request, target), location, (object?)obj ?? floor, (obj as Fence)?.health.Value ?? 0, error);
        return error == null ? tool : null;
    }

    private NativeActionStepResult VerifyPlayerRemoval(PlayerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var before = _playerRemovalBefore;
        _playerRemovalBefore = null;
        if (before == null || before.Value.Key != ChopKey(request, target))
            return NativeActionStepResult.Failed("Missing native removal preflight.");
        var snapshot = before.Value;
        if (snapshot.Error != null) return NativeActionStepResult.Precondition(snapshot.Error, "protected-or-unavailable-removal");
        if (!ReferenceEquals(actor.GameFarmer?.currentLocation, snapshot.Location))
            return NativeActionStepResult.Failed("Location changed during native removal.");
        float cost = Math.Max(0, actor.ToolStartStamina - actor.Stamina);
        var tile = new Vector2(target.Tile.X, target.Tile.Y);
        object? remaining = snapshot.Target is Flooring
            ? snapshot.Location!.terrainFeatures.TryGetValue(tile, out var terrain) ? terrain : null
            : snapshot.Location!.getObjectAtTile(target.Tile.X, target.Tile.Y);
        if (remaining == null)
            return NativeActionStepResult.Succeeded("removed", staminaCost: cost, itemId: request.ItemId, itemCount: 1);
        if (ReferenceEquals(remaining, snapshot.Target) && remaining is Fence fence && fence.health.Value < snapshot.Health)
            return NativeActionStepResult.Continue("removing-fence", staminaCost: cost);
        return NativeActionStepResult.Failed("Native removal did not remove the observed item; inspect actual drops and remaining target.", staminaCost: cost);
    }
}
