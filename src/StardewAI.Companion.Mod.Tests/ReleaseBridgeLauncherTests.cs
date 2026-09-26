using System.Diagnostics;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class ReleaseBridgeLauncherTests
{
    [Fact]
    public void SourceDirectoryWithoutReleasePayloadDoesNotGuessAParentLauncher()
    {
        string path = Path.Combine(Path.GetTempPath(), "missing-companion-" + Guid.NewGuid().ToString("N"));
        Assert.Null(ReleaseBridgeLauncher.CreateStartInfo(path));
    }

    [Fact]
    public void InstalledLauncherUsesOnlyItsOwnPackageAndLiteralPathArguments()
    {
        string root = Path.Combine(Path.GetTempPath(), "伙伴 包 & " + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path.Combine(root, "tools"));
        try
        {
            string script = Path.Combine(root, "tools", "start-companion.ps1");
            File.WriteAllText(script, "# test only; never executed");
            Assert.Null(ReleaseBridgeLauncher.CreateStartInfo(root));
            File.WriteAllText(Path.Combine(root, "release-manifest.json"), "{}");
            var start = ReleaseBridgeLauncher.CreateStartInfo(root)!;
            Assert.Equal(Path.GetFullPath(root), start.WorkingDirectory);
            Assert.Equal("powershell.exe", start.FileName);
            Assert.Equal(script, start.ArgumentList.Last());
            Assert.Contains("-File", start.ArgumentList);
            Assert.DoesNotContain("--run-dir", start.ArgumentList);
            Assert.False(start.UseShellExecute);
            Assert.True(start.CreateNoWindow);
            Assert.Equal(ProcessWindowStyle.Hidden, start.WindowStyle);
        }
        finally { Directory.Delete(root, recursive: true); }
    }
}
