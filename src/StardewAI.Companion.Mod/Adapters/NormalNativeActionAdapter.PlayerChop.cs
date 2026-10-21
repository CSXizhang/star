using Microsoft.Xna.Framework;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Adapters;

public sealed partial class NormalNativeActionAdapter
{
    private readonly Dictionary<string, (float Health, bool Stump, bool Animate, string? Rejection)> _playerChopBefore = new();
    private static string ChopKey(NativeActionRequest r, NativeActionTarget t) => $"{r.CommandId}:{t.Tile.X},{t.Tile.Y}";

    private string? PreparePlayerChop(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        _playerChopBefore.Clear(); // Coordinator allows one active action only.
        var location = ResolveLocation(request.LocationId);
        var tile = new Vector2(target.Tile.X, target.Tile.Y);
        var axe = actor.FindTool<Axe>();
        string key = ChopKey(request, target);
        if (location == null || axe == null)
        {
            _playerChopBefore[key] = (0, false, false, "Native location or Axe is unavailable.");
            return null;
        }
        location.terrainFeatures.TryGetValue(tile, out var feature);
        if (feature is FruitTree)
        {
            _playerChopBefore[key] = (0, false, false, "Fruit trees are protected; no swing was issued.");
            return null;
        }
        if (feature is Tree tree)
        {
            bool animate = !tree.falling.Value;
            _playerChopBefore[key] = (tree.health.Value, tree.stump.Value, animate, null);
            return animate ? "Axe" : null;
        }
        // Large clumps need their own native upgrade/footprint contract. This
        // primitive intentionally exposes that limitation before any real swing.
        _playerChopBefore[key] = (0, false, false, "No ordinary wild tree at this tile; large resource clumps are not supported by player chop_tree.");
        return null;
    }

    private NativeActionStepResult VerifyPlayerChop(PlayerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        string key = ChopKey(request, target);
        if (!_playerChopBefore.Remove(key, out var before))
            return NativeActionStepResult.Failed("Missing native chop preflight; no tree result inferred.");
        if (before.Rejection != null)
            return NativeActionStepResult.Precondition(before.Rejection, "protected-or-unsupported-tree");
        var location = actor.GameFarmer?.currentLocation;
        if (location == null || location.NameOrUniqueName != request.LocationId)
            return NativeActionStepResult.Failed("Native player location changed during the swing.");
        float cost = before.Animate ? Math.Max(0, actor.ToolStartStamina - actor.Stamina) : 0;
        var tile = new Vector2(target.Tile.X, target.Tile.Y);
        if (!location.terrainFeatures.TryGetValue(tile, out var feature) || feature is not Tree tree)
            return NativeActionStepResult.Succeeded("chopped-tree", staminaCost: cost);
        if (tree.falling.Value || !before.Animate)
            return NativeActionStepResult.Continue("native-tree-fall", staminaCost: cost);
        if (tree.health.Value < before.Health || tree.stump.Value != before.Stump)
            return NativeActionStepResult.Continue("chopping", staminaCost: cost);
        return NativeActionStepResult.Failed("The completed native Axe swing did not damage this tree; inspect its condition and required tool.", staminaCost: cost);
    }
}
