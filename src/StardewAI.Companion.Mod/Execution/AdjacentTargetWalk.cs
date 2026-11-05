using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Execution;

internal enum AdjacentWalkState { Walking, Facing, Preparing, Unreachable, PlayerObstruction }

/// <summary>Waypoint traversal shared by hoeing and planting; owns no action or task.</summary>
internal sealed class AdjacentTargetWalk
{
    private const float WalkPixelsPerTick = 4f;
    private const int MaxReplansPerTarget = 3;
    private readonly IFarmerActor _actor;
    private readonly IWorldObserver _observer;
    private readonly SameMapNavigator _navigator;
    private IReadOnlyList<PathStep> _path = Array.Empty<PathStep>();
    private int _index;
    private int _replans;

    public AdjacentTargetWalk(IFarmerActor actor, IWorldObserver observer, SameMapNavigator navigator)
    {
        _actor = actor;
        _observer = observer;
        _navigator = navigator;
    }

    public void Reset() { _path = Array.Empty<PathStep>(); _index = 0; _replans = 0; }

    public bool Plan(string location, TileCoordinate target)
    {
        _replans = 0;
        return Replan(location, target);
    }

    private bool Replan(string location, TileCoordinate target)
    {
        var result = _navigator.FindPathToInteract(_actor, location, target);
        if (!result.Success || result.Steps.Count == 0) return false;
        _path = result.Steps.ToList();
        _index = 0;
        return true;
    }

    public AdjacentWalkState Tick(string location, TileCoordinate target)
    {
        if (_index >= _path.Count)
        {
            _actor.Halt();
            return _actor.Tile.IsAdjacentTo(target) ? AdjacentWalkState.Facing : AdjacentWalkState.Preparing;
        }

        var step = _path[_index];
        if (ActorGuards.HasReachedWalkingTile(_actor, step.Tile))
        {
            _actor.Halt();
            _index++;
            return AdjacentWalkState.Walking;
        }
        var targetPixel = ActorGuards.WalkingTargetPixel(_actor, step.Tile);
        if (_observer.IsPlayerOnTile(location, step.Tile))
        {
            _actor.Halt();
            if (++_replans > MaxReplansPerTarget) return AdjacentWalkState.PlayerObstruction;
            return Replan(location, target) ? AdjacentWalkState.Walking : AdjacentWalkState.Unreachable;
        }

        var diff = targetPixel - _actor.PixelPosition;
        if (ActorGuards.IsPlayerBound(_actor))
            _actor.MovePixels(diff.X, diff.Y);
        else if (diff.Length() <= WalkPixelsPerTick)
        {
            if (ActorGuards.TryFinishWalkingStep(_actor, targetPixel)) _index++;
        }
        else
        {
            diff.Normalize();
            _actor.Facing = step.Facing;
            _actor.MovePixels(diff.X * WalkPixelsPerTick, diff.Y * WalkPixelsPerTick);
        }
        return AdjacentWalkState.Walking;
    }
}
