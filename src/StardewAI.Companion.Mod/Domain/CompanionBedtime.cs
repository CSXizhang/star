namespace StardewAI.Companion.Mod.Domain;

public static class CompanionBedtime
{
    public const int Default = 2400;
    public const int RoutePreviewWindowMinutes = 240;
    public static int Normalize(int time)
    {
        if (time is >= 0 and <= 100) time += 2400;
        return time is >= 1800 and <= 2500 && time % 100 < 60 ? time : Default;
    }
    public static int Minutes(int time) => time / 100 * 60 + time % 100;
    public static bool ShouldWindDown(int now, int bedtime, int travelMinutes) =>
        Minutes(now) >= Minutes(Normalize(bedtime)) - Math.Max(30, travelMinutes + 20);
    public static bool ShouldPreviewRoute(int now, int bedtime) =>
        Minutes(now) >= Minutes(Normalize(bedtime)) - RoutePreviewWindowMinutes;
    public static bool NeedsDaytimeRest(float stamina, int maxStamina) =>
        maxStamina > 0 && stamina <= Math.Max(20, maxStamina * 0.1f);
    public static bool HasRecovered(float stamina, int maxStamina) =>
        maxStamina > 0 && stamina >= maxStamina;
}

/// <summary>A short-lived estimate, not a reusable movement route. Dispatch always plans against the live world.</summary>
internal sealed class CompanionBedtimeRouteEstimate
{
    private (int Day, int Bedtime, string Origin, TileCoordinate Tile, string House, TileCoordinate Bed, int Clock, int Minutes)? _value;

    public bool TryGet(int day, int bedtime, string origin, TileCoordinate tile, string house, TileCoordinate bed, int now, out int minutes)
    {
        minutes = 60;
        if (_value is not { } cached) return false;
        int age = CompanionBedtime.Minutes(now) - CompanionBedtime.Minutes(cached.Clock);
        if (cached.Day != day || cached.Bedtime != CompanionBedtime.Normalize(bedtime) ||
            cached.Origin != origin || cached.House != house || cached.Bed != bed ||
            Math.Abs(cached.Tile.X - tile.X) + Math.Abs(cached.Tile.Y - tile.Y) > 12 || age < 0 || age >= 30)
            return false;
        minutes = cached.Minutes;
        return true;
    }

    public void Store(int day, int bedtime, string origin, TileCoordinate tile, string house, TileCoordinate bed, int now, int minutes) =>
        _value = (day, CompanionBedtime.Normalize(bedtime), origin, tile, house, bed, now, Math.Max(0, minutes));
    public void Clear() => _value = null;
}
