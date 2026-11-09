using StardewValley;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Adapters;

internal static class NativeResourceClumps
{
    // Stardew 1.6 native ResourceClump.performToolAction upgrade gates.
    public static (string? Tool, int Level) Requirement(int id) => id switch
    {
        600 => ("Axe", 1), 602 => ("Axe", 2),
        148 or 622 => ("Pickaxe", 3), 672 => ("Pickaxe", 2),
        752 or 754 or 756 or 758 => ("Pickaxe", 0), _ => (null, 0)
    };

    public static ResourceClump? Find(GameLocation? location, TileCoordinate tile) =>
        location?.resourceClumps.FirstOrDefault(clump =>
            tile.X >= clump.Tile.X && tile.X < clump.Tile.X + clump.width.Value
            && tile.Y >= clump.Tile.Y && tile.Y < clump.Tile.Y + clump.height.Value);
}

public sealed partial class NormalNativeActionAdapter
{
    private (string Key, GameLocation? Location, ResourceClump? Clump, float Health, string? Error)? _playerClumpBefore;

    public IReadOnlyList<NativeActionTarget> GetInteractionTargets(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var clump = request.Kind is NativeActionKind.ChopTree or NativeActionKind.ClearDebris
            ? NativeResourceClumps.Find(ResolveLocation(request.LocationId), target.Tile) : null;
        if (clump == null) return new[] { target };
        var targets = new List<NativeActionTarget>();
        for (int x = 0; x < clump.width.Value; x++)
            for (int y = 0; y < clump.height.Value; y++)
                if (x == 0 || y == 0 || x == clump.width.Value - 1 || y == clump.height.Value - 1)
                    targets.Add(target with { Tile = new TileCoordinate((int)clump.Tile.X + x, (int)clump.Tile.Y + y) });
        return targets;
    }

    private string? PreparePlayerClump(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var location = ResolveLocation(request.LocationId);
        var clump = NativeResourceClumps.Find(location, target.Tile);
        var required = NativeResourceClumps.Requirement(clump?.parentSheetIndex.Value ?? -1);
        Tool? tool = required.Tool == "Axe" ? actor.FindTool<Axe>() : actor.FindTool<Pickaxe>();
        string expected = request.Kind == NativeActionKind.ChopTree ? "Axe" : "Pickaxe";
        string? error = clump == null || required.Tool == null ? "No supported native resource clump at this tile."
            : required.Tool != expected ? $"This clump requires {required.Tool}; use {(required.Tool == "Axe" ? "chop_tree" : "clear_debris")}."
            : tool == null || tool.UpgradeLevel < required.Level ? $"Requires {required.Tool} upgrade level {required.Level}; no swing issued." : null;
        _playerClumpBefore = (ChopKey(request, target), location, clump, clump?.health.Value ?? 0, error);
        return error == null ? required.Tool : null;
    }

    private NativeActionStepResult VerifyPlayerClump(PlayerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var before = _playerClumpBefore;
        _playerClumpBefore = null;
        if (before == null || before.Value.Key != ChopKey(request, target))
            return NativeActionStepResult.Failed("Missing native resource-clump preflight.");
        var snapshot = before.Value;
        if (snapshot.Error != null) return NativeActionStepResult.Precondition(snapshot.Error, "clump-tool-requirement");
        if (!ReferenceEquals(actor.GameFarmer?.currentLocation, snapshot.Location))
            return NativeActionStepResult.Failed("Native location changed during the clump swing.");
        float cost = Math.Max(0, actor.ToolStartStamina - actor.Stamina);
        if (!snapshot.Location!.resourceClumps.Contains(snapshot.Clump!))
            return NativeActionStepResult.Succeeded("clump-cleared", staminaCost: cost);
        return snapshot.Clump!.health.Value < snapshot.Health
            ? NativeActionStepResult.Continue("breaking-clump", staminaCost: cost)
            : NativeActionStepResult.Failed("Native swing did not damage the resource clump; inspect tool and reachability.", staminaCost: cost);
    }
}
