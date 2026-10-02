namespace StardewAI.Companion.Mod.Domain;

public static class CompanionBedtime
{
    public const int Default = 2400;
    public static int Normalize(int time)
    {
        if (time is >= 0 and <= 100) time += 2400;
        return time is >= 1800 and <= 2500 && time % 100 < 60 ? time : Default;
    }
    public static int Minutes(int time) => time / 100 * 60 + time % 100;
    public static bool ShouldWindDown(int now, int bedtime, int travelMinutes) =>
        Minutes(now) >= Minutes(Normalize(bedtime)) - Math.Max(30, travelMinutes + 20);
}
