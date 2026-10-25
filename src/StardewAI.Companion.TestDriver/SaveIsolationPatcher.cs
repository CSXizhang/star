using System;
using System.IO;
using System.Reflection;
using HarmonyLib;
using StardewModdingAPI;

namespace StardewAI.Companion.TestDriver;

public static class SaveIsolationPatcher
{
    public static string? TargetIsolatedSavesPath { get; private set; }
    private static bool _patched;

    public static bool Apply(string isolatedSavesPath, IMonitor monitor)
    {
        if (_patched)
        {
            monitor.Log($"[TestDriver] SaveIsolationPatcher already active: {TargetIsolatedSavesPath}", LogLevel.Debug);
            return VerifyIsolation(monitor);
        }

        try
        {
            if (string.IsNullOrWhiteSpace(isolatedSavesPath))
            {
                monitor.Log("[TestDriver] FATAL: Save isolation path is null or empty. Gating fails closed.", LogLevel.Error);
                return false;
            }

            var fullPath = Path.GetFullPath(isolatedSavesPath);
            Directory.CreateDirectory(fullPath);
            TargetIsolatedSavesPath = fullPath;
            PatcherState.TargetIsolatedPath = fullPath;

            var harmony = new Harmony("StardewAI.Companion.TestDriver.SaveIsolation");

            // 1. Patch StardewValley.Program.GetSavesFolder()
            var gameMethod = typeof(StardewValley.Program).GetMethod(
                "GetSavesFolder",
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);

            if (gameMethod == null)
            {
                monitor.Log("[TestDriver] FATAL: StardewValley.Program.GetSavesFolder method not found.", LogLevel.Error);
                return false;
            }

            var gamePrefix = typeof(PatcherState).GetMethod(
                nameof(PatcherState.Prefix_GetSavesFolder),
                BindingFlags.Public | BindingFlags.Static);

            harmony.Patch(gameMethod, prefix: new HarmonyMethod(gamePrefix));

            // 2. Patch StardewModdingAPI.Constants.get_SavesPath()
            var smapiGetter = typeof(Constants).GetProperty(
                "SavesPath",
                BindingFlags.Public | BindingFlags.Static)?.GetGetMethod();

            if (smapiGetter == null)
            {
                monitor.Log("[TestDriver] FATAL: StardewModdingAPI.Constants.get_SavesPath getter not found.", LogLevel.Error);
                return false;
            }

            var smapiPrefix = typeof(PatcherState).GetMethod(
                nameof(PatcherState.Prefix_SmapiSavesPath),
                BindingFlags.Public | BindingFlags.Static);

            harmony.Patch(smapiGetter, prefix: new HarmonyMethod(smapiPrefix));
            _patched = true;

            // 3. Strict fail-closed verification of BOTH game and SMAPI paths
            return VerifyIsolation(monitor);
        }
        catch (Exception ex)
        {
            monitor.Log($"[TestDriver] FATAL: Failed to apply save isolation patches: {ex}", LogLevel.Error);
            return false;
        }
    }

    private static bool VerifyIsolation(IMonitor monitor)
    {
        var verifiedGame = StardewValley.Program.GetSavesFolder();
        var verifiedSmapi = Constants.SavesPath;

        bool gameMatch = string.Equals(verifiedGame, TargetIsolatedSavesPath, StringComparison.OrdinalIgnoreCase);
        bool smapiMatch = string.Equals(verifiedSmapi, TargetIsolatedSavesPath, StringComparison.OrdinalIgnoreCase);

        if (!gameMatch)
        {
            monitor.Log($"[TestDriver] FATAL: Program.GetSavesFolder() returned '{verifiedGame}' instead of expected '{TargetIsolatedSavesPath}'!", LogLevel.Error);
            return false;
        }

        if (!smapiMatch)
        {
            monitor.Log($"[TestDriver] FATAL: Constants.SavesPath returned '{verifiedSmapi}' instead of expected '{TargetIsolatedSavesPath}'!", LogLevel.Error);
            return false;
        }

        monitor.Log($"[TestDriver] Save path isolation ACTIVE and VERIFIED on both Game and SMAPI roots: {TargetIsolatedSavesPath}", LogLevel.Info);
        return true;
    }
}

public static class PatcherState
{
    public static string? TargetIsolatedPath;

    public static bool Prefix_GetSavesFolder(ref string __result)
    {
        if (!string.IsNullOrEmpty(TargetIsolatedPath))
        {
            __result = TargetIsolatedPath;
            return false; // Skip original game method
        }
        return true;
    }

    public static bool Prefix_SmapiSavesPath(ref string __result)
    {
        if (!string.IsNullOrEmpty(TargetIsolatedPath))
        {
            __result = TargetIsolatedPath;
            return false; // Skip original SMAPI getter
        }
        return true;
    }
}
