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
            Assert.Equal(script, start.ArgumentList[start.ArgumentList.IndexOf("-File") + 1]);
            Assert.Equal("-OwnerProcessId", start.ArgumentList[^2]);
            Assert.Equal(Environment.ProcessId.ToString(), start.ArgumentList[^1]);
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
            File.WriteAllText(Path.Combine(root, "tools", "start-companion.ps1"), "param([int]$OwnerProcessId)\n" + scriptText);
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

    [Fact]
    public async Task DisposingOwnedLauncherStopsItsChildTreeAndLeavesAnUnrelatedLauncherAlive()
    {
        string root = CreateTestPackage(@"
param([int]$OwnerProcessId)
$child = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList @('-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep -Seconds 60') -PassThru -WindowStyle Hidden
[IO.File]::WriteAllText((Join-Path $PSScriptRoot '../data/pids'), ""$PID,$($child.Id)"")
Start-Sleep -Seconds 60
");
        var unrelatedStart = new ProcessStartInfo
        {
            FileName = ReleaseBridgeLauncher.CreateStartInfo(root)!.FileName,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        foreach (string arg in new[] { "-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 60" })
            unrelatedStart.ArgumentList.Add(arg);
        using var unrelated = Process.Start(unrelatedStart)!;
        using var launch = ReleaseBridgeLauncher.Start(ReleaseBridgeLauncher.CreateStartInfo(root)!);
        Process? parent = null;
        Process? child = null;
        try
        {
            int[] pids = await ReadProcessIds(root);
            parent = Process.GetProcessById(pids[0]);
            child = Process.GetProcessById(pids[1]);
            launch.Dispose();
            launch.Dispose(); // Repeated title/exit cleanup is harmless.
            var result = await launch.Completion.WaitAsync(TimeSpan.FromSeconds(10));
            Assert.Equal(0, result.ExitCode);
            Assert.Null(result.FailureReason);
            Assert.True(parent.WaitForExit(2000));
            Assert.True(child.WaitForExit(2000));
            Assert.False(unrelated.HasExited);
        }
        finally
        {
            StopTestProcess(child);
            StopTestProcess(parent);
            StopTestProcess(unrelated);
            Directory.Delete(root, recursive: true);
        }
    }

    [Fact]
    public async Task StopDuringPreflightCancelsStartupAndAnotherSaveCanStartAFreshLauncher()
    {
        string root = CreateTestPackage(@"
param([int]$OwnerProcessId)
[IO.File]::WriteAllText((Join-Path $PSScriptRoot '../data/pids'), [string]$PID)
Start-Sleep -Seconds 30
[IO.File]::WriteAllText((Join-Path $PSScriptRoot '../data/bridge-started'), 'started')
Start-Sleep -Seconds 60
");
        try
        {
            int oldPid;
            using (var first = ReleaseBridgeLauncher.Start(ReleaseBridgeLauncher.CreateStartInfo(root)!))
            {
                oldPid = (await ReadProcessIds(root))[0];
                using var process = Process.GetProcessById(oldPid);
                first.Dispose();
                Assert.Equal(0, (await first.Completion.WaitAsync(TimeSpan.FromSeconds(10))).ExitCode);
                Assert.True(process.WaitForExit(2000));
            }
            File.Delete(Path.Combine(root, "data", "pids"));
            using var second = ReleaseBridgeLauncher.Start(ReleaseBridgeLauncher.CreateStartInfo(root)!);
            int newPid = (await ReadProcessIds(root))[0];
            using var nextProcess = Process.GetProcessById(newPid);
            Assert.NotEqual(oldPid, newPid);
            Assert.False(nextProcess.HasExited);
            Assert.False(File.Exists(Path.Combine(root, "data", "bridge-started")));
            second.Dispose();
            Assert.Equal(0, (await second.Completion.WaitAsync(TimeSpan.FromSeconds(10))).ExitCode);
            Assert.True(nextProcess.WaitForExit(2000));
        }
        finally { Directory.Delete(root, recursive: true); }
    }

    [Fact]
    public async Task StopImmediatelyAfterStartDoesNotLeaveAnUnpublishedProcess()
    {
        string root = CreateTestPackage("param([int]$OwnerProcessId)\nStart-Sleep -Seconds 60");
        try
        {
            for (int i = 0; i < 5; i++)
            {
                using var launch = ReleaseBridgeLauncher.Start(ReleaseBridgeLauncher.CreateStartInfo(root)!);
                launch.Dispose();
                Assert.Equal(0, (await launch.Completion.WaitAsync(TimeSpan.FromSeconds(10))).ExitCode);
            }
        }
        finally { Directory.Delete(root, recursive: true); }
    }

    private static string CreateTestPackage(string script)
    {
        string root = Path.Combine(Path.GetTempPath(), "companion-lifetime-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path.Combine(root, "tools"));
        Directory.CreateDirectory(Path.Combine(root, "data"));
        File.WriteAllText(Path.Combine(root, "release-manifest.json"), "{}");
        File.WriteAllText(Path.Combine(root, "tools", "start-companion.ps1"), script);
        return root;
    }

    private static async Task<int[]> ReadProcessIds(string root)
    {
        string path = Path.Combine(root, "data", "pids");
        for (int attempt = 0; attempt < 200; attempt++)
        {
            if (File.Exists(path))
            {
                string content = File.ReadAllText(path);
                if (content.Length > 0) return content.Split(',').Select(int.Parse).ToArray();
            }
            await Task.Delay(50);
        }
        throw new TimeoutException("Test launcher did not finish its startup marker.");
    }

    private static void StopTestProcess(Process? process)
    {
        if (process == null) return;
        try
        {
            if (!process.HasExited) process.Kill(entireProcessTree: true);
            process.WaitForExit(2000);
        }
        catch (InvalidOperationException) { }
        finally { process.Dispose(); }
    }
}
