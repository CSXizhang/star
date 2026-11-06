using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Adapters;

public sealed partial class NormalNativeActionAdapter
{
    private (string Key, object? Target, int Density, string? Error)? _playerGroundBefore;

    private string? PreparePlayerGround(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        _playerClumpBefore = null;
        var location = ResolveLocation(request.LocationId);
        var tile = new Vector2(target.Tile.X, target.Tile.Y);
        if (request.Kind == NativeActionKind.ClearDebris && NativeResourceClumps.Find(location, target.Tile) != null)
            return PreparePlayerClump(actor, request, target);
        object? obstacle = null;
        int density = 0;
        string? tool = null;
        if (request.Kind == NativeActionKind.CutGrass && location != null
            && location.terrainFeatures.TryGetValue(tile, out var feature))
        {
            obstacle = feature;
            density = (feature as Grass)?.numberOfWeeds.Value ?? 0;
            if (feature is Grass || feature is HoeDirt dirt && IsDeadCrop(dirt))
                tool = actor.GetToolNames().Contains("Scythe") ? "Scythe" : null;
        }
        else if (request.Kind == NativeActionKind.ClearDebris && location?.getObjectAtTile(target.Tile.X, target.Tile.Y) is { } obj)
        {
            obstacle = obj;
            if (obj.IsWeeds() || obj.IsTwig()) tool = actor.FindTool<Axe>() != null ? "Axe" : null;
            if (obj.IsBreakableStone()) tool = actor.FindTool<Pickaxe>() != null ? "Pickaxe" : null;
        }
        _playerGroundBefore = (ChopKey(request, target), obstacle, density,
            tool == null ? "No supported ground obstacle and available native tool; crops and placed objects are protected." : null);
        return tool;
    }

    private NativeActionStepResult VerifyPlayerGround(PlayerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var before = _playerGroundBefore;
        _playerGroundBefore = null;
        if (before == null || before.Value.Key != ChopKey(request, target))
            return NativeActionStepResult.Failed("Missing native ground-clearing preflight.");
        if (before.Value.Error != null) return NativeActionStepResult.Precondition(before.Value.Error, "protected-or-unavailable-ground-target");
        var location = actor.GameFarmer?.currentLocation;
        if (location == null || location.NameOrUniqueName != request.LocationId)
            return NativeActionStepResult.Failed("Native player location changed during clearing.");
        float cost = Math.Max(0, actor.ToolStartStamina - actor.Stamina);
        var tile = new Vector2(target.Tile.X, target.Tile.Y);
        object? remaining = request.Kind == NativeActionKind.ClearDebris
            ? location.getObjectAtTile(target.Tile.X, target.Tile.Y)
            : location.terrainFeatures.TryGetValue(tile, out var feature) ? feature : null;
        if (remaining == null || remaining is HoeDirt dirt && dirt.crop == null && before.Value.Target is HoeDirt)
            return NativeActionStepResult.Succeeded("cleared", staminaCost: cost);
        if (ReferenceEquals(remaining, before.Value.Target) && remaining is Grass grass
            && grass.numberOfWeeds.Value < before.Value.Density)
            return NativeActionStepResult.Continue("cutting-grass", staminaCost: cost);
        return NativeActionStepResult.Failed("Native tool completed but the observed target remains without verified progress.", staminaCost: cost);
    }
}
