using System.Reflection;
using StardewValley;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>Scopes the ambient location used by a synchronous native action to its target map.</summary>
internal static class NativeToolLocationContext
{
    private static readonly FieldInfo? GameField = typeof(Game1).GetField("game1", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
    private static readonly FieldInfo? LocationField = typeof(Game1).GetField("instanceGameLocation", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);

    public static void Run(GameLocation location, Action action)
    {
        // WateringCan.DoFunction tests Game1.currentLocation for refill water,
        // even when the explicit location argument is the companion's farm.
        // Building placement likewise reads Buildable/Diggable from this ambient map.
        // Avoid the public setter: it raises player location-change events.
        var game = GameField?.GetValue(null);
        if (game is null || LocationField is null)
            throw new InvalidOperationException("Cannot access native action location context.");
        var previousLocation = LocationField.GetValue(game);
        try
        {
            LocationField.SetValue(game, location);
            action();
        }
        finally
        {
            LocationField.SetValue(game, previousLocation);
        }
    }
}
