using Microsoft.Xna.Framework;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Adapters;

public sealed partial class NormalNativeActionAdapter
{
    public string? GetAnimationTool(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var location = ResolveLocation(request.LocationId);
        if (location is null) return null;
        switch (request.Kind)
        {
            case NativeActionKind.RefillWateringCan:
                // An empty can cannot start a pouring animation. Refill is a
                // separate native transaction and does not pretend to pour.
                return null;
            case NativeActionKind.CutGrass:
                return actor.GetToolNames().Contains("Scythe") ? "Scythe" : null;
            case NativeActionKind.ChopTree:
                if (location.terrainFeatures.TryGetValue(new Vector2(target.Tile.X, target.Tile.Y), out var feature)
                    && feature is Tree tree && tree.falling.Value) return null;
                return actor.FindTool<Axe>() is not null ? "Axe" : null;
            case NativeActionKind.ClearDebris:
                var debris = location.getObjectAtTile(target.Tile.X, target.Tile.Y);
                if (debris is null) return null;
                if (debris.IsWeeds()) return actor.Hoe is not null ? "Hoe" : actor.FindTool<Axe>() is not null ? "Axe" : null;
                if (debris.IsBreakableStone()) return actor.FindTool<Pickaxe>() is not null ? "Pickaxe" : null;
                if (debris.IsTwig()) return actor.FindTool<Axe>() is not null ? "Axe" : actor.FindTool<Pickaxe>() is not null ? "Pickaxe" : null;
                return null;
            default:
                return null;
        }
    }
}
