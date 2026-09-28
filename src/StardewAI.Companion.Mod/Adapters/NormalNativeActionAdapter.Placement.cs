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
    private static bool SameItem(string? first, string? second) =>
        !string.IsNullOrWhiteSpace(first) && !string.IsNullOrWhiteSpace(second) &&
        string.Equals(ItemRegistry.QualifyItemId(first), ItemRegistry.QualifyItemId(second), StringComparison.Ordinal);

    private NativeActionStepResult PlaceItems(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var loc = ResolveLocation(request.LocationId);
        var farmer = actor.GameFarmer;
        if (loc is null || farmer is null || !ReferenceEquals(farmer.currentLocation, loc))
            return NativeActionStepResult.Precondition("Companion is not on the requested map.", "wrong-map");
        if (!actor.Tile.IsAdjacentTo(target.Tile) && actor.Tile != target.Tile)
            return NativeActionStepResult.Precondition("Placement requires an adjacent tile.", "not-adjacent");
        var source = farmer.Items.OfType<StardewValley.Object>().FirstOrDefault(i => SameItem(i.QualifiedItemId, request.ItemId) && i.Stack > 0);
        if (source is null)
            return NativeActionStepResult.Precondition("The companion does not carry the requested item.", "missing-item", playerActionRequired: true);
        if (source is StorageFurniture || source.heldObject.Value is not null || source is Chest chest && chest.Items.Any(i => i is not null))
            return NativeActionStepResult.Precondition("Empty stored contents before placing this item.", "protected-contents");
        // Native predicates distinguish placeable construction from consumables (e.g. bombs and totems).
        if (!(source is Furniture || source.bigCraftable.Value || source.IsFloorPathItem() || source.IsFenceItem()
            || source.IsWildTreeSapling() || source.IsFruitTreeSapling() || source.IsTeaSapling() || source.IsSprinkler()))
            return NativeActionStepResult.Precondition("This item is not a supported construction item.", "unsupported-placement-item");
        var tile = new Vector2(target.Tile.X, target.Tile.Y);
        if (_observer.IsWarpOrDoorTile(request.LocationId, target.Tile))
            return NativeActionStepResult.Precondition("Entrance/warp tiles must stay clear.", "protected-entrance");
        if (loc.objects.ContainsKey(tile) || loc.terrainFeatures.ContainsKey(tile) || loc.GetFurnitureAt(tile) is not null)
            return NativeActionStepResult.Precondition("Target already contains an object or terrain feature; inspect before changing it.", "occupied");
        var placed = (StardewValley.Object)source.getOne();
        if (!placed.canBePlacedHere(loc, tile))
            return NativeActionStepResult.Precondition("Native placement rules refuse this item at the requested tile.", "native-placement-refused");
        return InvokeIsolated(actor, "place-items", () =>
        {
            bool accepted = placed.placementAction(loc, target.Tile.X * 64, target.Tile.Y * 64, farmer);
            bool changed = loc.objects.TryGetValue(tile, out var obj) && SameItem(obj.QualifiedItemId, source.QualifiedItemId)
                || loc.furniture.Any(f => f.TileLocation == tile && SameItem(f.QualifiedItemId, source.QualifiedItemId))
                || loc.terrainFeatures.TryGetValue(tile, out var feature) && (feature is Flooring floor
                    ? SameItem(floor.GetData()?.ItemId, source.QualifiedItemId)
                    : feature is Tree or FruitTree or Bush);
            // placementAction creates the placed entity; the native caller consumes the inventory unit.
            // Consume on observed mutation even when a mod's return value disagrees, preventing duplication.
            if (accepted || changed)
            {
                source.Stack--;
                if (source.Stack <= 0) farmer.Items[farmer.Items.IndexOf(source)] = null;
            }
            if (!accepted || !changed)
                return NativeActionStepResult.Failed($"Native placement result={accepted}; expected placed entity verified={changed}. Re-observe before retrying.");
            return NativeActionStepResult.Succeeded("placed", itemId: request.ItemId, itemCount: 1);
        });
    }

    private NativeActionStepResult RemoveItems(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var loc = ResolveLocation(request.LocationId);
        var farmer = actor.GameFarmer;
        if (loc is null || farmer is null || !ReferenceEquals(farmer.currentLocation, loc))
            return NativeActionStepResult.Precondition("Companion is not on the requested map.", "wrong-map");
        if (!actor.Tile.IsAdjacentTo(target.Tile) && actor.Tile != target.Tile)
            return NativeActionStepResult.Precondition("Removal requires an adjacent tile.", "not-adjacent");
        var tile = new Vector2(target.Tile.X, target.Tile.Y);
        var furniture = loc.GetFurnitureAt(tile);
        if (furniture is not null) return RemoveFurniture(actor, loc, furniture, request.ItemId);
        var obj = loc.getObjectAtTile(target.Tile.X, target.Tile.Y);
        loc.terrainFeatures.TryGetValue(tile, out var terrain);
        var floor = terrain as Flooring;
        if (obj is null && floor is null)
            return NativeActionStepResult.Precondition("No supported removable object or floor at this tile. Furniture needs native pickup support.", "unsupported-removal-target");
        string? actualId = obj?.QualifiedItemId ?? floor?.GetData()?.ItemId;
        if (!SameItem(actualId, request.ItemId))
            return NativeActionStepResult.Precondition($"Target identity changed: found '{actualId}', expected '{request.ItemId}'.", "target-changed");
        if (obj is Chest || obj?.heldObject.Value is not null || obj?.IsTapper() == true || obj?.CanBeGrabbed == true)
            return NativeActionStepResult.Precondition("Storage, filled machines, tappers and forage are protected from construction removal.", "protected-object");
        if (obj is not null && !(obj is Fence || obj.bigCraftable.Value || obj.IsSprinkler()))
            return NativeActionStepResult.Precondition("This object is not a supported construction removal target.", "unsupported-removal-target");
        Tool? tool = actor.FindTool<Pickaxe>() ?? (Tool?)actor.FindTool<Axe>();
        if (tool is null)
            return NativeActionStepResult.Precondition("Removal needs a pickaxe or axe in the companion inventory.", "missing-tool", playerActionRequired: true);
        if (actor.FreeInventorySlots < 1)
            return NativeActionStepResult.Precondition("Keep one inventory slot free for native recovered drops.", "inventory-full", playerActionRequired: true);
        return InvokeIsolated(actor, "remove-items", () =>
        {
            var beforeDebris = loc.debris.ToHashSet();
            var beforeInventory = SnapshotInventoryTotals(farmer);
            float stamina = actor.Stamina;
            tool.DoFunction(loc, target.Tile.X * 64 + 32, target.Tile.Y * 64 + 32, 1, farmer);
            bool gone = obj is not null ? !ReferenceEquals(loc.getObjectAtTile(target.Tile.X, target.Tile.Y), obj)
                : !loc.terrainFeatures.TryGetValue(tile, out var current) || !ReferenceEquals(current, floor);
            // Claim only drops generated by this exact native swing, on the same game tick.
            // Never sweep existing player debris or manufacture replacement items.
            foreach (var debris in loc.debris.Where(d => !beforeDebris.Contains(d) && NativeAnimalHarvestInventoryScope.IsOrdinaryDebris(d)).ToList())
            {
                for (int i = debris.Chunks.Count - 1; i >= 0; i--)
                {
                    var chunk = debris.Chunks[i];
                    bool collected;
                    using (new NativeAnimalHarvestInventoryScope(farmer, loc, debris, chunk))
                        collected = debris.collect(farmer, chunk);
                    if (!collected) return NativeActionStepResult.Failed("Native removal ran, but a generated drop could not enter the companion backpack. Re-observe before retrying.");
                    debris.Chunks.RemoveAt(i);
                }
                if (debris.Chunks.Count == 0) loc.debris.Remove(debris);
            }
            int gained = MeasureGainedStack(beforeInventory, SnapshotInventoryTotals(farmer), ItemRegistry.QualifyItemId(actualId!));
            if (!gone || gained < 1)
                return NativeActionStepResult.Failed($"Native removal verified gone={gone}, recovered expected items={gained}; no recovery was fabricated.");
            return NativeActionStepResult.Succeeded("removed-and-recovered", staminaCost: Math.Max(0, stamina - actor.Stamina), itemId: request.ItemId, itemCount: gained);
        });
    }
}
