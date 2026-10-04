using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Objects;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Menus;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>Local daytime rest and bedtime; never calls the player's sleep/day-end action.</summary>
public sealed class CompanionRestController
{
    private readonly IFarmerActor _actor;
    private readonly NavigationStateMachine _navigation;
    private int _lastAttemptTime = -1;
    private int _lastPreviewTime = -1;
    private bool _cancelRequested;
    private readonly CompanionBedtimeRouteEstimate _routeEstimate = new();
    private string? _recoveryFailure;
    public bool Paused { get; set; }
    public int Bedtime { get; set; } = CompanionBedtime.Default;
    public string State { get; private set; } = "awake";
    public string? Reason { get; private set; }
    public int? SleepStartedAt { get; private set; }
    public bool IsDaytimeRest { get; private set; }
    public bool IsResting => State != "awake";

    public CompanionRestController(IFarmerActor actor, NavigationStateMachine navigation)
    { _actor = actor; _navigation = navigation; }

    public void RestoreSleep(int startedAt, bool daytime = false)
    {
        SleepStartedAt = startedAt;
        IsDaytimeRest = daytime;
        State = daytime ? "resting" : "sleeping";
        if (_actor.GameFarmer is { } farmer)
        {
            farmer.isInBed.Value = true;
            farmer.timeWentToBed.Value = startedAt;
            farmer.CanMove = false;
            farmer.regenTimer = 500;
            NativeCompanionAnimation.SleepPose(farmer);
        }
    }

    public void Wake()
    {
        // An idle rest navigator shares the actor with work. Do not clear or halt
        // a newer work task merely because its old navigation request still exists.
        if (_navigation.IsExecuting)
        {
            _navigation.RequestCancel("Rest finished");
            _navigation.Update(null, 0);
        }
        State = "awake";
        Reason = null;
        SleepStartedAt = null;
        IsDaytimeRest = false;
        _cancelRequested = false;
        _lastAttemptTime = -1;
        _lastPreviewTime = -1;
        _routeEstimate.Clear();
        _recoveryFailure = null;
        if (_actor.GameFarmer is { } farmer)
        {
            if (farmer.isInBed.Value) BedFurniture.ShiftPositionForBed(farmer);
            farmer.isInBed.Value = false;
            farmer.timeWentToBed.Value = 0;
            farmer.currentEyes = 0;
            farmer.blinkTimer = 0;
            farmer.CanMove = true;
            farmer.Halt();
            farmer.FarmerSprite.StopAnimation();
        }
    }

    private void FinishWorkAtSafePoint(ISkillExecutionMachine? work)
    {
        if (work?.IsExecuting != true || _cancelRequested) return;
        // Pause boundaries finish and verify the current effect before the partial
        // result is emitted, preserving accurate progress for tomorrow.
        if (!work.IsPaused) { work.RequestPause(); return; }
        work.RequestCancel(IsDaytimeRest
            ? "LOW_STAMINA_REST: wrapping up to recover in bed; preserve unfinished work until awake."
            : "BEDTIME: wrapping up for sleep; preserve unfinished work for tomorrow.");
        _cancelRequested = true;
    }

    private static bool CanEnterBed(GameLocation house, BedFurniture bed, TileCoordinate origin, TileCoordinate target)
    {
        int dx = target.X - origin.X, dy = target.Y - origin.Y;
        if (Math.Abs(dx) + Math.Abs(dy) > 3 || (dx != 0 && dy != 0)) return false;
        int x = origin.X, y = origin.Y;
        while (x != target.X || y != target.Y)
        {
            x += Math.Sign(dx); y += Math.Sign(dy);
            if (!ReferenceEquals(BedFurniture.GetBedAtTile(house, x, y), bed)) return false;
            var tile = new Vector2(x, y);
            if (house.objects.ContainsKey(tile)) return false;
            var bounds = new Rectangle(x * 64, y * 64, 64, 64);
            if (house.furniture.Any(f => !ReferenceEquals(f, bed) && !f.isPassable() && f.GetBoundingBox().Intersects(bounds))) return false;
        }
        return ReferenceEquals(BedFurniture.GetBedAtTile(house, target.X, target.Y), bed);
    }

    public void Update(GameTime? time, long tick, ISkillExecutionMachine? work)
    {
        if (Paused || Game1.paused || Game1.eventUp || CompanionMenuClock.HasBlockingMenu) return;
        // The same native time boundary applies to movement and resource recovery.
        if (time is not null && !Game1.shouldTimePass()) return;
        int now = Game1.timeOfDay;
        if (State is "sleeping" or "resting")
        {
            if (IsDaytimeRest && CompanionBedtime.ShouldWindDown(now, Bedtime, 0))
            {
                IsDaytimeRest = false;
                SleepStartedAt = now;
                State = "sleeping";
                if (_actor.GameFarmer is { } sleepingFarmer) sleepingFarmer.timeWentToBed.Value = now;
                return;
            }
            if (IsDaytimeRest && CompanionBedtime.HasRecovered(_actor.Stamina, _actor.MaxStamina))
            {
                Wake();
                return;
            }
            if (time is not null && _actor.GameFarmer is { } restingFarmer && _recoveryFailure is null)
            {
                if (!restingFarmer.isInBed.Value || restingFarmer.currentLocation is null ||
                    BedFurniture.GetBedAtTile(restingFarmer.currentLocation, _actor.Tile.X, _actor.Tile.Y) is null)
                {
                    restingFarmer.isInBed.Value = false;
                    restingFarmer.CanMove = true;
                    SleepStartedAt = null;
                    State = "waiting-for-bed";
                    Reason = "休息的床已不可用，需要重新找床。";
                    return;
                }
                try { NativeBedRecovery.Apply(restingFarmer, time); }
                catch (Exception ex)
                {
                    _recoveryFailure = $"床上恢复未能执行：{ex.Message}";
                    Reason = _recoveryFailure;
                }
            }
            return;
        }
        // Rest requires a physical native farmer and bed. Offline mechanics tests
        // have neither and must not resolve the global game world when stamina falls.
        if (_actor.GameFarmer is null) return;
        if (!IsResting)
        {
            if (CompanionBedtime.NeedsDaytimeRest(_actor.Stamina, _actor.MaxStamina))
            {
                IsDaytimeRest = !CompanionBedtime.ShouldWindDown(now, Bedtime, 0);
                State = "winding-down";
                Reason = IsDaytimeRest ? "体力偏低，收尾后回床恢复。" : null;
            }
            else if (!CompanionBedtime.ShouldPreviewRoute(now, Bedtime)) return;
        }
        var house = Game1.getLocationFromName("FarmHouse") as FarmHouse;
        var bed = house?.GetPlayerBed();
        if (house == null || bed == null)
        {
            if (IsResting || CompanionBedtime.ShouldWindDown(now, Bedtime, 30))
            {
                State = "waiting-for-bed"; Reason = "家里没有可用的床。";
                FinishWorkAtSafePoint(work);
            }
            return;
        }
        var spot = bed.GetBedSpot();
        var target = new TileCoordinate(spot.X, spot.Y);
        if (!IsResting)
        {
            int day = Game1.Date.TotalDays;
            if (!_routeEstimate.TryGet(day, Bedtime, _actor.LocationName, _actor.Tile, house.NameOrUniqueName, target, now, out int travel))
            {
                if (_lastPreviewTime == now) return;
                _lastPreviewTime = now;
                var route = _navigation.PreviewRoute(house.NameOrUniqueName, target);
                travel = route.TryGetValue("estimatedGameMinutes", out var estimate) && estimate is int minutes ? minutes : 60;
                _routeEstimate.Store(day, Bedtime, _actor.LocationName, _actor.Tile, house.NameOrUniqueName, target, now, travel);
            }
            if (!CompanionBedtime.ShouldWindDown(now, Bedtime, travel)) return;
            State = "winding-down";
        }
        if (work?.IsExecuting == true)
        {
            FinishWorkAtSafePoint(work);
            return;
        }
        if (_navigation.IsExecuting)
        {
            _navigation.Update(time, tick);
            if (!_navigation.IsExecuting && _navigation.FinalResult is { FinalState: ExecutionState.Failed } failed)
            {
                State = "waiting-for-bed";
                Reason = failed.ErrorMessage ?? "暂时走不到床边。";
            }
            return;
        }
        if (bed.IsBeingSleptIn())
        { _actor.Halt(); State = "waiting-for-bed"; Reason = "床正有人使用，等空出来再睡。"; return; }
        if (_actor.LocationName == house.NameOrUniqueName &&
            CanEnterBed(house, bed, _actor.Tile, target) &&
            _actor.GameFarmer is { } farmer)
        {
            // The normal navigator stops outside furniture. Walk the short final
            // entrance continuously, only through this bed's verified footprint.
            var destination = new Vector2(spot.X * 64, spot.Y * 64);
            var delta = destination - _actor.PixelPosition;
            if (delta.LengthSquared() > 0.01f)
            {
                float distance = delta.Length();
                var step = delta / distance * Math.Min(4f, distance);
                _actor.MovePixels(step.X, step.Y);
                return;
            }
            _actor.Halt();
            farmer.isInBed.Value = true;
            farmer.timeWentToBed.Value = now;
            farmer.CanMove = false;
            farmer.regenTimer = 500;
            NativeCompanionAnimation.SleepPose(farmer);
            farmer.lastSleepLocation.Value = house.NameOrUniqueName;
            farmer.lastSleepPoint.Value = farmer.TilePoint;
            farmer.mostRecentBed = farmer.Position;
            farmer.doEmote(24);
            SleepStartedAt = now;
            State = IsDaytimeRest ? "resting" : "sleeping";
            Reason = null;
            return;
        }
        if (State is "returning-home" or "waiting-for-bed" && _lastAttemptTime == now) return;
        _lastAttemptTime = now;
        var request = new NavigationRequest("bedtime", $"rest-{Game1.Date.TotalDays}-{now}", house.NameOrUniqueName, target, 180);
        if (_navigation.Start(request, out var failure))
        { State = "returning-home"; Reason = null; }
        else
        { State = "waiting-for-bed"; Reason = failure?.ErrorMessage ?? "暂时走不到床边。"; }
    }
}
