namespace StardewAI.Companion.Mod.Transport;

/// <summary>Polled on the game thread; only an actual chat handshake confirms startup.</summary>
public sealed class ReleaseBridgeStartupState
{
    private Task<ReleaseBridgeLaunchResult>? _launch;
    private DateTime _startedAt;
    private bool _waiting;
    private string? _reportedProblem;
    public string? Problem { get; private set; }
    public bool IsStarting => _waiting && Problem == null;

    public bool CanStart(DateTime now) => (_launch == null || _launch.IsCompleted) &&
        (!_waiting || now - _startedAt >= TimeSpan.FromSeconds(60)) &&
        (_startedAt == default || now - _startedAt >= TimeSpan.FromSeconds(30));

    public void Begin(Task<ReleaseBridgeLaunchResult> launch, DateTime now)
    {
        _launch = launch;
        _startedAt = now;
        _waiting = true;
    }

    public string? Update(bool connected, DateTime now)
    {
        if (connected)
        {
            _waiting = false;
            Problem = _reportedProblem = null;
            return null;
        }
        if (!_waiting) return null;
        if (_launch?.IsCompleted == true)
        {
            var result = _launch.Status == TaskStatus.RanToCompletion ? _launch.Result :
                new ReleaseBridgeLaunchResult(-1, "伙伴启动器无法完成运行，请检查伙伴安装。");
            if (result.ExitCode != 0)
            {
                _waiting = false;
                return Report(result.FailureReason ?? $"伙伴启动进程退出（代码 {result.ExitCode}）。");
            }
        }
        if (now - _startedAt < TimeSpan.FromSeconds(60)) return null;
        return Report("启动后 60 秒仍未收到伙伴连接；服务尚未连接，请检查模型配置和伙伴后台。");
    }

    private string? Report(string problem)
    {
        Problem = problem;
        if (_reportedProblem == problem) return null;
        _reportedProblem = problem;
        return problem;
    }

    public void Reset()
    {
        _launch = null;
        _startedAt = default;
        _waiting = false;
        Problem = _reportedProblem = null;
    }
}
