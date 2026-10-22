using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.GameData.Crops;
using StardewValley.TerrainFeatures;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// One genuine player scythe swing. The native tool lifecycle performs every crop
/// callback and creates its own drops. This class only records before/after facts.
/// </summary>
internal sealed class PlayerScytheHarvest
{
    private const int MaxAnimationTicks = 180;
    private readonly PlayerActor _actor;
    private readonly GameLocation _location;
    private readonly float _staminaBefore;
    private readonly Dictionary<TileCoordinate, (HoeDirt Dirt, Crop Crop, string? CropId)> _before = new();
    private int _ticks;

    public bool Completed { get; private set; }
    public string? Error { get; private set; }
    public float StaminaUsed { get; private set; }
    public List<HarvestedTileInfo> HarvestedTiles { get; } = new();

    public PlayerScytheHarvest(PlayerActor actor, IWorldObserver observer, string locationId, TileCoordinate target)
    {
        if (!observer.IsMainThread || actor.GameFarmer?.currentLocation is not { } location ||
            !string.Equals(actor.LocationName, locationId, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Scythe harvesting requires the bound player on the current map and game thread.");
        if (!actor.Tile.IsAdjacentTo(target))
            throw new InvalidOperationException("Scythe harvesting requires an adjacent standing tile.");
        if (!location.terrainFeatures.TryGetValue(new Vector2(target.X, target.Y), out var feature) ||
            feature is not HoeDirt dirt || dirt.crop is null || dirt.crop.dead.Value || !dirt.readyForHarvest() ||
            dirt.crop.GetHarvestMethod() != HarvestMethod.Scythe)
            throw new InvalidOperationException("The requested tile no longer has a mature scythe crop.");

        _actor = actor;
        _location = location;
        _staminaBefore = actor.Stamina;
        // Native scythes have an area of effect. Record nearby mature crops before
        // the swing so collateral native harvests are reported once, without
        // attributing a made-up number of drops to individual tiles.
        for (int x = actor.Tile.X - 8; x <= actor.Tile.X + 8; x++)
        for (int y = actor.Tile.Y - 8; y <= actor.Tile.Y + 8; y++)
        {
            if (location.terrainFeatures.TryGetValue(new Vector2(x, y), out var nearby) &&
                nearby is HoeDirt cropDirt && cropDirt.crop is { } crop && !crop.dead.Value && cropDirt.readyForHarvest())
                _before[new TileCoordinate(x, y)] = (cropDirt, crop, crop.indexOfHarvest?.Value);
        }
        _actor.BeginUsingTool("Scythe");
    }

    public void Update(GameTime? time, long tickCount)
    {
        if (Completed) return;
        if (_actor.GameFarmer?.currentLocation != _location)
        {
            Error = "Native player binding or map changed during scythe harvest.";
            Completed = true;
            return;
        }
        _ticks++;
        var phase = _actor.UpdateToolAnimation(time, tickCount);
        if (phase != ToolAnimationPhase.Completed && _ticks < MaxAnimationTicks) return;

        foreach (var (tile, before) in _before)
        {
            bool advanced = before.Dirt.crop is null ||
                (ReferenceEquals(before.Dirt.crop, before.Crop) && !before.Crop.dead.Value && !before.Dirt.readyForHarvest());
            if (advanced)
                HarvestedTiles.Add(new HarvestedTileInfo(tile, before.CropId, null, null, 0, 0) { HarvestMethod = "scythe" });
        }
        StaminaUsed = Math.Max(0, _staminaBefore - _actor.Stamina);
        if (phase != ToolAnimationPhase.Completed)
            Error = "Native scythe animation did not complete within its limit; observed crop changes are retained.";
        // Only resets actor-side observation counters; never changes UsingTool,
        // CanMove, crop fields, inventory, position, or native animation state.
        _actor.EndUsingTool();
        Completed = true;
    }
}
