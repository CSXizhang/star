using Microsoft.Xna.Framework;
using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>Only approaching the current waypoint counts as walking progress.</summary>
internal sealed class WalkingProgress
{
    private TileCoordinate? _target;
    private float _best = float.PositiveInfinity;
    private int _idle;

    public void Reset() { _target = null; _best = float.PositiveInfinity; _idle = 0; }

    public bool IsBlocked(IFarmerActor actor, TileCoordinate target)
    {
        float distance = Vector2.Distance(actor.PixelPosition, ActorGuards.WalkingTargetPixel(actor, target));
        if (_target != target || distance < _best - 0.5f)
        {
            _target = target;
            _best = distance;
            _idle = 0;
        }
        else _idle++;
        return _idle >= 180;
    }
}
