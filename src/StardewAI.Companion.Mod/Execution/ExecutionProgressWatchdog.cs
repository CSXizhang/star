using Microsoft.Xna.Framework;
using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>Detects stalled execution without imposing a duration on productive work.</summary>
internal sealed class ExecutionProgressWatchdog
{
    private bool _observed;
    private long _lastProgressTick;
    private string? _location;
    private Vector2 _position;
    private int _targetIndex;
    private float _stamina;
    private int _water;

    public void Reset() => _observed = false;

    public bool HasTimedOut(long activeTick, long maxIdleTicks, IFarmerActor actor, int targetIndex)
    {
        if (!_observed || _location != actor.LocationName ||
            Vector2.DistanceSquared(_position, actor.PixelPosition) >= 1f ||
            _targetIndex != targetIndex || _stamina != actor.Stamina || _water != actor.WaterLeft)
        {
            _observed = true;
            _lastProgressTick = activeTick;
            _location = actor.LocationName;
            _position = actor.PixelPosition;
            _targetIndex = targetIndex;
            _stamina = actor.Stamina;
            _water = actor.WaterLeft;
        }

        return activeTick - _lastProgressTick > maxIdleTicks;
    }
}
