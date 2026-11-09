using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Navigation;

namespace StardewAI.Companion.Mod.Observation;

/// <summary>Current native interaction routes, not placement vetoes or future guarantees.</summary>
public static class OperationAccessObserver
{
    public static Dictionary<string, object> Observe(IWorldObserver observer, SameMapNavigator navigator,
        IFarmerActor actor, string location, IReadOnlyList<TileCoordinate>? changedTiles = null)
    {
        var machines = observer.ScanMachines(location, 257);
        var candidates = machines.Select(m => (m.Tile, Kind: "machine", ItemId: (string?)m.ItemId))
            .Concat(observer.ScanChests(location).Select(c => (c.Tile, Kind: "chest", ItemId: (string?)null)))
            .DistinctBy(item => item.Tile);
        if (changedTiles != null)
            candidates = candidates.Where(item => changedTiles.Any(tile => tile == item.Tile || tile.IsAdjacentTo(item.Tile)));
        var selected = candidates.Take(257).ToArray();
        bool sameMap = string.Equals(actor.LocationName, location, StringComparison.OrdinalIgnoreCase);
        var items = selected.Take(256).Select(item => {
            var route = sameMap ? navigator.FindPathToInteract(actor, location, item.Tile) : null;
            return new { tile = item.Tile, kind = item.Kind, itemId = item.ItemId,
                reachable = (bool?)route?.Success,
                standingTile = route?.Success == true ? (TileCoordinate?)route.Steps.Last().Tile : null,
                error = route?.ErrorMessage, observationStatus = sameMap ? "observed" : "actor-on-other-map" };
        }).ToArray();
        return new() { ["locationId"] = location, ["capturedRevision"] = observer.WorldRevision,
            ["scope"] = changedTiles == null ? "observed-machines-and-chests" : "changed-tiles-and-adjacent-machines-or-chests",
            ["items"] = items, ["truncated"] = machines.Count > 256 || selected.Length > 256,
            ["note"] = "Current native interaction access only; temporary blockers can change it. Placement is not proof of access or production. Inspect machines for actual input, processing and output." };
    }
}
