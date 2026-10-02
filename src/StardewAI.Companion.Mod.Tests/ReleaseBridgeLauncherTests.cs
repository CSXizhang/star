using System.Diagnostics;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class ReleaseBridgeLauncherTests
{
    [Fact]
    public void CurrentBackendSelectionMarkerHasReadableFailure()
    {
        Assert.Contains("配置无效", ReleaseBridgeLauncher.SafeFailureReason("Choose Kimi, agy, Codex, or dsh in setup first."));
    }
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
            Assert.Equal(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
                "WindowsPowerShell", "v1.0", "powershell.exe"), start.FileName);
            Assert.True(Path.IsPathFullyQualified(start.FileName));
            Assert.Equal(script, start.ArgumentList.Last());
            Assert.Contains("-File", start.ArgumentList);
            Assert.DoesNotContain("--run-dir", start.ArgumentList);
            Assert.False(start.UseShellExecute);
            Assert.True(start.CreateNoWindow);
            Assert.True(start.RedirectStandardOutput);
            Assert.True(start.RedirectStandardError);
            Assert.Equal(ProcessWindowStyle.Hidden, start.WindowStyle);
        }
        finally { Directory.Delete(root, recursive: true); }
    }

    [Theory]
    [InlineData("Write-Error 'AI CLI codex is missing. sessionToken=never-show-this'; exit 9", "codex", 9)]
    [InlineData("Write-Error 'AI CLI dsh is missing. sessionToken=never-show-this'; exit 1", "dsh", 1)]
    [InlineData("Write-Error 'AI CLI dsh Node.js is missing. sessionToken=never-show-this'; exit 1", "Node.js", 1)]
    [InlineData("Write-Error 'AI CLI dsh entry point is missing. sessionToken=never-show-this'; exit 1", "dsh 启动文件缺失", 1)]
    [InlineData("Write-Error 'AI CLI mcode is missing. sessionToken=never-show-this'; exit 1", "mcode", 1)]
    [InlineData("Write-Error 'AI CLI mcode Node.js is missing. sessionToken=never-show-this'; exit 1", "Node.js", 1)]
    [InlineData("Write-Error 'AI CLI mcode entry point is missing. sessionToken=never-show-this'; exit 1", "mcode 启动文件缺失", 1)]
    [InlineData("Write-Error 'AI CLI mcode could not start. sessionToken=never-show-this'; exit 1", "mcode 客户端无法启动", 1)]
    [InlineData("Write-Error 'AI CLI mcode did not respond to its startup check. sessionToken=never-show-this'; exit 1", "mcode 客户端启动检查超时", 1)]
    [InlineData("Write-Error 'AI CLI mcode failed its startup check. sessionToken=never-show-this'; exit 1", "mcode 客户端启动检查失败", 1)]
    [InlineData("Add-Content -LiteralPath (Join-Path $PSScriptRoot '../data/release-start.log') -Value 'AI CLI kimi is missing'; exit 3", "kimi", 3)]
    [InlineData("Write-Error 'unknown failure sessionToken=never-show-this'; exit 2", "代码 2", 2)]
    [InlineData("Write-Error 'AI CLI codex did not respond to its startup check. sessionToken=never-show-this'; exit 4", "codex 客户端启动检查超时", 4)]
    public async Task FailedHiddenLauncherReportsOnlyThisAttemptAndSafeReason(string scriptText, string expected, int exitCode)
    {
        string root = Path.Combine(Path.GetTempPath(), "companion-start-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path.Combine(root, "tools"));
        Directory.CreateDirectory(Path.Combine(root, "data"));
        try
        {
            File.WriteAllText(Path.Combine(root, "release-manifest.json"), "{}");
            File.WriteAllText(Path.Combine(root, "tools", "start-companion.ps1"), scriptText);
            File.WriteAllText(Path.Combine(root, "data", "release-start.log"), "AI CLI agy is missing. sessionToken=old-secret\n");
            var result = await ReleaseBridgeLauncher.RunAsync(ReleaseBridgeLauncher.CreateStartInfo(root)!);
            Assert.Equal(exitCode, result.ExitCode);
            Assert.Contains(expected, result.FailureReason);
            Assert.DoesNotContain("agy", result.FailureReason);
            Assert.DoesNotContain("sessionToken", result.FailureReason);
            Assert.DoesNotContain("never-show-this", result.FailureReason);
        }
        finally { Directory.Delete(root, recursive: true); }
    }
}
