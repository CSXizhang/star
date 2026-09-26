using System.Diagnostics;

namespace StardewAI.Companion.Mod.Transport;

/// <summary>Launch only the runtime shipped beside this installed Mod.</summary>
public static class ReleaseBridgeLauncher
{
    public static ProcessStartInfo? CreateStartInfo(string modDirectory)
    {
        string root = Path.GetFullPath(modDirectory);
        string script = Path.Combine(root, "tools", "start-companion.ps1");
        if (!File.Exists(Path.Combine(root, "release-manifest.json")) || !File.Exists(script)) return null;
        var start = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
            WorkingDirectory = root,
        };
        // ArgumentList keeps installation paths literal, including spaces and metacharacters.
        foreach (string argument in new[] { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script })
            start.ArgumentList.Add(argument);
        return start;
    }
}
