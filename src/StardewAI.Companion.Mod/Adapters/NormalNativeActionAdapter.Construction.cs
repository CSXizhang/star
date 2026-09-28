using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Objects;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Adapters;

public sealed partial class NormalNativeActionAdapter
{
    private NativeActionStepResult CraftItems(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var farmer = actor.GameFarmer;
        string name = request.ItemId ?? "";
        if (farmer is null) return NativeActionStepResult.Failed("Companion farmer is unavailable.");
        // The farm's learned crafting recipes are readable shared knowledge; materials and output
        // belong exclusively to the companion. No recipe is unlocked by this operation.
        if (!Game1.player.craftingRecipes.ContainsKey(name) || !CraftingRecipe.craftingRecipes.ContainsKey(name))
            return NativeActionStepResult.Precondition("The farm has not learned this crafting recipe.", "recipe-not-learned");
        var recipe = new CraftingRecipe(name, false);
        var remaining = farmer.Items.Select(i => i?.Stack ?? 0).ToArray();
        var consume = new int[remaining.Length];
        foreach (var ingredient in recipe.recipeList)
        {
            int needed = ingredient.Value;
            for (int slot = 0; slot < remaining.Length && needed > 0; slot++)
            {
                if (remaining[slot] <= 0 || !CraftingRecipe.ItemMatchesForCrafting(farmer.Items[slot], ingredient.Key)) continue;
                int used = Math.Min(remaining[slot], needed);
                remaining[slot] -= used; consume[slot] += used; needed -= used;
            }
            if (needed > 0)
                return NativeActionStepResult.Precondition($"Companion lacks {needed} of recipe ingredient '{ingredient.Key}'.", "missing-materials");
        }
        if (actor.FreeInventorySlots < 1)
            return NativeActionStepResult.Precondition("Crafting requires one free companion inventory slot.", "inventory-full");
        return InvokeIsolated(actor, "craft-items", () =>
        {
            // Use the game's genuine recipe factory, and its ingredient matcher above. The native
            // consumeIngredients method is deliberately not called: it always consumes Game1.player.
            var output = recipe.createItem();
            if (output is null || output.Stack <= 0) return NativeActionStepResult.Failed("Native recipe returned no item.");
            string outputId = output.QualifiedItemId;
            int outputCount = output.Stack;
            var inventoryBefore = SnapshotInventoryTotals(farmer);
            var originals = farmer.Items.ToArray();
            var stacks = originals.Select(i => i?.Stack ?? 0).ToArray();
            for (int slot = 0; slot < consume.Length; slot++)
            {
                if (consume[slot] == 0) continue;
                farmer.Items[slot].Stack -= consume[slot];
                if (farmer.Items[slot].Stack <= 0) farmer.Items[slot] = null;
            }
            if (!actor.TryAddItemToInventory(output))
            {
                // All work is one game-thread transaction. Restore only the actual consumed objects.
                for (int slot = 0; slot < originals.Length; slot++)
                {
                    if (consume[slot] == 0) continue;
                    farmer.Items[slot] = originals[slot];
                    originals[slot].Stack = stacks[slot];
                }
                return NativeActionStepResult.Failed("Native crafted item could not be inserted; materials restored.");
            }
            int gained = MeasureGainedStack(inventoryBefore, SnapshotInventoryTotals(farmer), outputId);
            if (gained != outputCount)
                return NativeActionStepResult.Failed($"Crafting output delta {gained} differs from native output {outputCount}; re-observe before retrying.");
            return NativeActionStepResult.Succeeded("crafted", itemId: outputId, itemCount: outputCount);
        });
    }

    private NativeActionStepResult RemoveFurniture(IFarmerActor actor, GameLocation loc, Furniture furniture, string? expectedId)
    {
        var farmer = actor.GameFarmer!;
        if (!SameItem(furniture.QualifiedItemId, expectedId))
            return NativeActionStepResult.Precondition("Furniture identity changed.", "target-changed");
        // Multiplayer uses a queue owned by the local player. Do not bypass that ownership/mutex.
        if (Game1.IsMultiplayer || furniture.GetType() != typeof(Furniture) || furniture.heldObject.Value is not null)
            return NativeActionStepResult.Precondition("Only ordinary empty furniture in single-player supports companion pickup.", "unsupported-furniture-pickup");
        if (!furniture.canBeRemoved(farmer))
            return NativeActionStepResult.Precondition("Native furniture removal rules refuse pickup.", "native-removal-refused");
        int freeSlot = -1;
        for (int i = 0; i < farmer.Items.Count; i++) if (farmer.Items[i] is null) { freeSlot = i; break; }
        if (freeSlot < 0) return NativeActionStepResult.Precondition("Companion backpack is full.", "inventory-full");
        return InvokeIsolated(actor, "remove-items", () =>
        {
            // Same native lifecycle as removeQueuedFurniture; transfer the existing instance to the
            // detached companion rather than that method's hard-coded Game1.player receiver.
            furniture.performRemoveAction();
            loc.furniture.Remove(furniture);
            farmer.Items[freeSlot] = furniture;
            if (loc.furniture.Contains(furniture) || !ReferenceEquals(farmer.Items[freeSlot], furniture))
                return NativeActionStepResult.Failed("Furniture transfer could not be verified.");
            return NativeActionStepResult.Succeeded("removed-and-recovered", itemId: expectedId, itemCount: 1);
        });
    }

    private NativeActionStepResult MoveBuilding(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var loc = ResolveLocation(request.LocationId);
        var farmer = actor.GameFarmer;
        if (loc is null || farmer is null || !ReferenceEquals(farmer.currentLocation, loc))
            return NativeActionStepResult.Precondition("Companion must be on the requested map.", "wrong-map");
        if (Game1.IsMultiplayer || !loc.IsBuildableLocation())
            return NativeActionStepResult.Precondition("Building relocation currently requires a single-player buildable location.", "unsupported-building-location");
        if (!Guid.TryParse(target.TargetId, out var id))
            return NativeActionStepResult.Precondition("buildingName must be the observed building id.", "building-not-found");
        var building = loc.getBuildingById(id);
        if (building is null || building.daysOfConstructionLeft.Value > 0 || building.isMoving)
            return NativeActionStepResult.Precondition("Building is missing, under construction, or already moving.", "building-unavailable");
        // CarpenterMenu.hasPermissionsToMove is an instance UI method. Its single-player
        // branch rejects an unrepaired greenhouse, then requires the master game; mirror
        // those read-only checks without constructing a menu or enabling farmhand permissions.
        // buildStructure by itself omits this selection gate.
        if (!Game1.IsMasterGame || building is StardewValley.Buildings.GreenhouseBuilding && !Game1.getFarm().greenhouseUnlocked.Value)
            return NativeActionStepResult.Precondition("Native carpenter permissions do not allow moving this building (including an unrepaired greenhouse).", "building-move-not-permitted");
        if (!actor.Tile.IsAdjacentTo(target.Tile))
            return NativeActionStepResult.Precondition("Companion must stand beside the destination origin.", "not-adjacent");
        var footprint = new Rectangle(target.Tile.X, target.Tile.Y, building.tilesWide.Value, building.tilesHigh.Value);
        if (footprint.Contains(actor.Tile.X, actor.Tile.Y))
            return NativeActionStepResult.Precondition("Navigate to the left or above the destination before moving the building.", "companion-in-footprint");
        // Native move clears underlying terrain. Reject occupied destinations first so existing
        // crops, paths and objects are never erased as an incidental side effect of construction.
        var placementTiles = new HashSet<Point>();
        for (int y = footprint.Top; y < footprint.Bottom; y++)
        for (int x = footprint.Left; x < footprint.Right; x++) placementTiles.Add(new Point(x, y));
        foreach (var extra in building.GetAdditionalPlacementTiles())
        for (int y = extra.TileArea.Top; y < extra.TileArea.Bottom; y++)
        for (int x = extra.TileArea.Left; x < extra.TileArea.Right; x++) placementTiles.Add(new Point(target.Tile.X + x, target.Tile.Y + y));
        foreach (var point in placementTiles)
        {
            int x = point.X, y = point.Y;
            var tile = new Vector2(x, y);
            if ((!building.occupiesTile(tile) && !_observer.IsTilePassable(request.LocationId, new TileCoordinate(x, y)))
                || _observer.IsWarpOrDoorTile(request.LocationId, new TileCoordinate(x, y))
                || loc.terrainFeatures.ContainsKey(tile) || loc.objects.ContainsKey(tile) || loc.GetFurnitureAt(tile) is not null)
                return NativeActionStepResult.Precondition("The destination footprint contains existing terrain, occupants or entrances.", "occupied-footprint");
        }
        return InvokeIsolated(actor, "move-building", () =>
        {
            building.OnStartMove();
            bool moved = loc.buildStructure(building, new Vector2(target.Tile.X, target.Tile.Y), farmer, false);
            if (!moved) return NativeActionStepResult.Precondition("Native building placement checks refused the destination.", "native-building-refused");
            building.OnEndMove();
            if (building.tileX.Value != target.Tile.X || building.tileY.Value != target.Tile.Y || !ReferenceEquals(loc.getBuildingById(id), building))
                return NativeActionStepResult.Failed("Native building relocation did not match the requested destination.");
            return NativeActionStepResult.Succeeded("building-moved", itemId: building.buildingType.Value, itemCount: 1);
        });
    }
}
