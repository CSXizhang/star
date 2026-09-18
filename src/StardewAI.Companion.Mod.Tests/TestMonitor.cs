using StardewModdingAPI;
using StardewModdingAPI.Framework.Logging;

namespace StardewAI.Companion.Mod.Tests;

public sealed class TestMonitor : IMonitor
{
    public bool IsVerbose => false;
    public List<string> LoggedMessages { get; } = new();

    public void Log(string message, LogLevel level = LogLevel.Debug)
    {
        LoggedMessages.Add($"[{level}] {message}");
    }

    public void LogOnce(string message, LogLevel level = LogLevel.Debug)
    {
        Log(message, level);
    }

    public void VerboseLog(string message)
    {
    }

    public void VerboseLog(ref VerboseLogStringHandler message)
    {
    }
}
