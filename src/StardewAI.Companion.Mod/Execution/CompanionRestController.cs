using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Objects;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Menus;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>Local bedtime routine; never calls the player's sleep/day-end action.</summary>
public sealed class CompanionRestController
{
    private readonly IFarmerActor _actor;
    private readonly NavigationStateMachine _navigation;
    private int _lastAttemptTime = -1;
    private bool _cancelRequested;
    public bool Paused { get; set; }
    public int Bedtime { get; set; } = CompanionBedtime.Default;
    public string State { get; private set; } = "awake";
    public string? Reason { get; private set; }
    public int? SleepStartedAt { get; private set; }
    public bool IsResting => State != "awake";

    public CompanionRestController(IFarmerActor actor, NavigationStateMachine navigation)
    { _actor = actor; _navigation = navigation; }

    public void RestoreSleep(int startedAt)
    {
        SleepStartedAt = startedAt;
        State = "sleeping";
        if (_actor.GameFarmer is { } farmer)
        {
            farmer.isInBed.Value = true;
            farmer.timeWentToBed.Value = startedAt;
            farmer.CanMove = false;
            farmer.FarmerSprite.setCurrentFrame(farmer.FacingDirection * 8);
        }
    }

    public void Wake()
    {
        _navigation.RequestCancel("New day");
        _navigation.Update(null, 0);
        State = "awake";
        Reason = null;
        SleepStartedAt = null;
        _cancelRequested = false;
        _lastAttemptTime = -1;
        if (_actor.GameFarmer is { } farmer)
        {
            if (farmer.isInBed.Value) BedFurniture.ShiftPositionForBed(farmer);
            farmer.isInBed.Value = false;
            farmer.CanMove = true;
            farmer.FarmerSprite.StopAnimation();
        }
    }

    private void FinishWorkAtSafePoint(ISkillExecutionMachine? work)
    {
        if (work?.IsExecuting != true || _cancelRequested) return;
        // Pause boundaries finish and verify the current effect before the partial
        // result is emitted, preserving accurate progress for tomorrow.
        if (!work.IsPaused) { work.RequestPause(); return; }
        work.RequestCancel("BEDTIME: wrapping up for sleep; preserve unfinished work for tomorrow.");
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
        if (State == "sleeping") return;
        int now = Game1.timeOfDay;
        if (!IsResting && now < 1200) return;
        var house = Game1.getLocationFromName("FarmHouse") as FarmHouse;
        var bed = house?.GetPlayerBed();
        if (house == null || bed == null)
        {
            if (CompanionBedtime.ShouldWindDown(now, Bedtime, 30))
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
            if (_lastAttemptTime == now) return;
            _lastAttemptTime = now;
            var route = _navigation.PreviewRoute(house.NameOrUniqueName, target);
            int travel = route.TryGetValue("estimatedGameMinutes", out var estimate) && estimate is int minutes ? minutes : 60;
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
            return;
        }
        if (bed.IsBeingSleptIn())
        { State = "waiting-for-bed"; Reason = "床正有人使用，等空出来再睡。"; return; }
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
            farmer.FarmerSprite.setCurrentFrame(farmer.FacingDirection * 8);
            farmer.lastSleepLocation.Value = house.NameOrUniqueName;
            farmer.lastSleepPoint.Value = farmer.TilePoint;
            farmer.mostRecentBed = farmer.Position;
            farmer.doEmote(24);
            SleepStartedAt = now;
            State = "sleeping";
            Reason = null;
            return;
        }
        if (State is "returning-home" or "waiting-for-bed" && _lastAttemptTime == now) return;
        _lastAttemptTime = now;
        var request = new NavigationRequest("bedtime", $"bedtime-{Game1.Date.TotalDays}-{now}", house.NameOrUniqueName, target, 180);
        if (_navigation.Start(request, out var failure))
        { State = "returning-home"; Reason = null; }
        else
        { State = "waiting-for-bed"; Reason = failure?.ErrorMessage ?? "暂时走不到床边。"; }
    }
}
