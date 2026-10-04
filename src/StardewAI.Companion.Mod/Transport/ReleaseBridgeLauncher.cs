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
            FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
                "WindowsPowerShell", "v1.0", "powershell.exe"),
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
            WorkingDirectory = root,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        // ArgumentList keeps installation paths literal, including spaces and metacharacters.
        foreach (string argument in new[] { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script,
            "-OwnerProcessId", Environment.ProcessId.ToString(System.Globalization.CultureInfo.InvariantCulture) })
            start.ArgumentList.Add(argument);
        return start;
    }

    public static ReleaseBridgeLaunch Start(ProcessStartInfo start) => new(start);

    public static async Task<ReleaseBridgeLaunchResult> RunAsync(ProcessStartInfo start)
    {
        using var launch = Start(start);
        return await launch.Completion.ConfigureAwait(false);
    }

    internal static async Task<ReleaseBridgeLaunchResult> RunOwnedAsync(ProcessStartInfo start, ReleaseBridgeLaunch launch)
    {
        string logPath = Path.Combine(start.WorkingDirectory, "data", "release-start.log");
        long logOffset = LogLength(logPath);
        string? reason = null;
        void Capture(object sender, DataReceivedEventArgs args)
        {
            // Retain only recognized launcher failures, never CLI output or credentials.
            string? safe = SafeFailureReason(args.Data);
            if (safe != null) Interlocked.CompareExchange(ref reason, safe, null);
        }
        try
        {
            using var process = new Process { StartInfo = start };
            process.OutputDataReceived += Capture;
            process.ErrorDataReceived += Capture;
            try
            {
                if (!launch.StartProcess(process)) return new(-1, "无法创建伙伴启动进程，请检查安装和系统权限。");
                await process.WaitForExitAsync().ConfigureAwait(false);
                // WaitForExit drains the final redirected output events too.
                process.WaitForExit();
                if (launch.IsStopping || process.ExitCode == 0) return new(0, null);
                reason ??= ReadNewFailure(logPath, logOffset);
                return new(process.ExitCode, reason ?? $"伙伴启动进程退出（代码 {process.ExitCode}），请检查伙伴目录 data/release-start.log。");
            }
            finally { launch.ReleaseProcess(process); }
        }
        catch (System.ComponentModel.Win32Exception)
        {
            return new(-1, "无法运行 PowerShell 启动器，请检查安装和系统权限。");
        }
        catch (Exception)
        {
            return new(-1, "伙伴启动器无法完成运行，请检查伙伴目录 data/release-start.log。");
        }
    }

    public static string? SafeFailureReason(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return null;
        foreach (string cli in new[] { "codex", "kimi", "agy", "dsh", "mcode" })
        {
            if (text.Contains($"AI CLI {cli} Node.js is missing", StringComparison.OrdinalIgnoreCase))
                return $"{cli} 所需的 Node.js 未找到，请修复 Node.js 安装后重新打开游戏。";
            if (text.Contains($"AI CLI {cli} entry point is missing", StringComparison.OrdinalIgnoreCase))
                return $"{cli} 启动文件缺失，请修复 {cli} 安装后重新打开游戏。";
            if (text.Contains($"AI CLI {cli} is missing", StringComparison.OrdinalIgnoreCase))
                return $"伙伴启动环境中未找到 {cli} 客户端。请查看伙伴目录 data/release-start.log 中的查找路径。";
            if (text.Contains($"AI CLI {cli} could not start", StringComparison.OrdinalIgnoreCase))
                return $"{cli} 客户端无法启动，请修复该客户端安装后重新打开游戏。";
            if (text.Contains($"AI CLI {cli} did not respond to its startup check", StringComparison.OrdinalIgnoreCase))
                return $"{cli} 客户端启动检查超时，请修复该客户端安装后重新打开游戏。";
            if (text.Contains($"AI CLI {cli} failed its startup check", StringComparison.OrdinalIgnoreCase))
                return $"{cli} 客户端启动检查失败，请修复该客户端安装后重新打开游戏。";
        }
        foreach (var failure in new (string Marker, string Message)[]
        {
            ("Run the setup entry first", "尚未配置 AI 客户端和模型，请运行伙伴目录里的“设置星露谷伙伴.cmd”。"),
            ("Choose Kimi, agy, or Codex", "AI 客户端配置无效，请重新运行“设置星露谷伙伴.cmd”。"),
            ("Choose Kimi, agy, Codex, or dsh", "AI 客户端配置无效，请重新运行“设置星露谷伙伴.cmd”。"),
            ("Choose Kimi, agy, Codex, dsh, or mcode", "AI 客户端配置无效，请重新运行“设置星露谷伙伴.cmd”。"),
            ("Packaged Python could not import", "伙伴随包运行环境缺少依赖，请重新安装完整伙伴包。"),
            ("Release file missing", "伙伴安装文件缺失，请重新安装完整伙伴包。"),
            ("Release file changed", "伙伴安装文件与发行包不一致，请重新安装匹配的伙伴包。"),
            ("Mod/runtime release pairing differs", "伙伴 Mod 与运行环境版本不匹配，请重新安装匹配的伙伴包。"),
            ("Unsupported release package", "伙伴发行包格式或平台不受支持，请重新安装 Windows 版本。"),
            ("Companion service exited with code", "伙伴后台服务提前退出，请检查伙伴目录 data/release-start.log。"),
        })
            if (text.Contains(failure.Marker, StringComparison.OrdinalIgnoreCase)) return failure.Message;
        return null;
    }

    private static long LogLength(string path)
    {
        try { return File.Exists(path) ? new FileInfo(path).Length : 0; }
        catch (IOException) { return -1; }
        catch (UnauthorizedAccessException) { return -1; }
    }

    private static string? ReadNewFailure(string path, long offset)
    {
        if (offset < 0) return null;
        try
        {
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            if (stream.Length <= offset) return null;
            stream.Position = Math.Max(offset, stream.Length - 16384);
            using var reader = new StreamReader(stream);
            return SafeFailureReason(reader.ReadToEnd());
        }
        catch (IOException) { return null; }
        catch (UnauthorizedAccessException) { return null; }
    }
}

/// <summary>Owns only the launcher started by this Mod and its descendants.</summary>
public sealed class ReleaseBridgeLaunch : IDisposable
{
    private readonly object _gate = new();
    private Process? _process;
    private bool _stopping;
    public Task<ReleaseBridgeLaunchResult> Completion { get; }
    internal bool IsStopping { get { lock (_gate) return _stopping; } }

    internal ReleaseBridgeLaunch(ProcessStartInfo start)
    {
        // Start is synchronous through process publication; the owner can stop it
        // as soon as this handle is returned, including during launcher preflight.
        Completion = ReleaseBridgeLauncher.RunOwnedAsync(start, this);
    }

    internal bool StartProcess(Process process)
    {
        lock (_gate)
        {
            if (_stopping) return false;
            if (!process.Start()) return false;
            _process = process;
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            return true;
        }
    }

    internal void ReleaseProcess(Process process)
    {
        lock (_gate)
        {
            // An output/setup failure must not abandon a process which already started.
            StopProcess(process);
            if (ReferenceEquals(_process, process)) _process = null;
        }
    }

    public void Dispose()
    {
        lock (_gate)
        {
            _stopping = true;
            if (_process != null) StopProcess(_process);
        }
    }

    private static void StopProcess(Process process)
    {
        try
        {
            if (process.HasExited) return;
            process.Kill(entireProcessTree: true);
            // ProcessExit cannot await the background task. Bound the synchronous
            // wait, while allowing the launcher/mutex to finish before a new save.
            process.WaitForExit(2000);
        }
        catch (InvalidOperationException) { } // It exited, or never started.
        catch (System.ComponentModel.Win32Exception) { } // OS teardown can race exit.
        catch (AggregateException) { } // A descendant exited during tree traversal.
    }
}

public sealed record ReleaseBridgeLaunchResult(int ExitCode, string? FailureReason);
