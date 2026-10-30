using HarmonyLib;
using Microsoft.Xna.Framework.Graphics;
using StardewValley;

namespace StardewAI.Companion.Mod.Avatar;

internal static class CompanionWorldDraw
{
    private static Func<CompanionAvatar?>? _current;

    internal static void Install(Func<CompanionAvatar?> current)
    {
        if (_current is null)
            new Harmony("StardewAI.Companion.WorldDraw").Patch(
                AccessTools.Method(typeof(GameLocation), "drawFarmers", new[] { typeof(SpriteBatch) }),
                postfix: new HarmonyMethod(typeof(CompanionWorldDraw), nameof(Draw)));
        _current = current;
    }

    private static void Draw(GameLocation __instance, SpriteBatch __0)
    {
        // Join the native world batch so objects, trees and front layers occlude
        // the companion using Farmer.draw's own depth, like other farmers.
        if (ReferenceEquals(__instance, Game1.currentLocation) && !Game1.eventUp)
            _current?.Invoke()?.Draw(__0);
    }
}
