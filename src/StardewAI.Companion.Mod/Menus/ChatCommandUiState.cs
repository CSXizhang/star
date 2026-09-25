namespace StardewAI.Companion.Mod.Menus;

/// <summary>
/// Main-thread presentation state for one player instruction and one pending control.
/// A command can contain several model turns and native jobs; only the command's
/// terminal reply (or a confirmed cancel) releases it for the next instruction.
/// </summary>
public sealed class ChatCommandUiState
{
    private readonly HashSet<string> _retiredTurnIds = new(StringComparer.Ordinal);
    public string? CommandId { get; private set; }
    public string? SaveId { get; private set; }
    public string? TurnId { get; private set; }
    public string? PendingControlId { get; private set; }
    public string? PendingControlAction { get; private set; }
    public bool IsPaused { get; private set; }
    public bool LocalPauseRequested { get; private set; }
    public string StatusText { get; private set; } = "就绪";

    public bool HasActiveCommand => CommandId is not null;
    public bool HasPendingControl => PendingControlId is not null;
    public bool CanSubmit => !HasActiveCommand && !HasPendingControl && !IsPaused && !LocalPauseRequested;

    public bool BeginCommand(string commandId, string saveId, string statusText = "正在规划")
    {
        if (!CanSubmit || string.IsNullOrWhiteSpace(commandId) || string.IsNullOrWhiteSpace(saveId))
            return false;
        CommandId = commandId;
        SaveId = saveId;
        TurnId = commandId;
        StatusText = statusText;
        return true;
    }

    public bool MatchesReply(string requestId, string? commandId, string? saveId)
    {
        if (CommandId is null || string.IsNullOrWhiteSpace(requestId)) return false;
        if (!string.IsNullOrEmpty(saveId) && !string.Equals(SaveId, saveId, StringComparison.Ordinal)) return false;
        if (_retiredTurnIds.Contains(requestId)) return false;
        // New wire replies carry the stable root command ID. Legacy replies are
        // accepted only for the current turn, so an old request cannot take over.
        return !string.IsNullOrEmpty(commandId)
            ? string.Equals(CommandId, commandId, StringComparison.Ordinal)
            : string.Equals(TurnId, requestId, StringComparison.Ordinal);
    }

    public bool ApplyReply(string requestId, string? commandId, string? saveId, string status, bool? commandComplete)
    {
        if (!MatchesReply(requestId, commandId, saveId)) return false;
        if (TurnId != requestId && TurnId != null) _retiredTurnIds.Add(TurnId);
        TurnId = requestId;
        // Explicit commandComplete owns the new multi-turn contract. Null keeps
        // the historical single-request terminal behavior for older bridges.
        bool terminal = commandComplete ?? status is "job-completed" or "job-failed" or
            "decision-completed" or "completed" or "cancelled" or "failed";
        StatusText = status switch
        {
            "processing" => "模型思考 / 观察与选择",
            "selected" => "已选择，等待执行",
            "job-running" => "原生执行中",
            "job-waiting" => "作业等待",
            "job-completed" => terminal ? "作业已完成" : "作业已完成，等待后续",
            "job-failed" => terminal ? "作业未完成" : "作业未完成，等待后续",
            "decision-completed" or "completed" => "模型回复完成",
            "cancelled" => "已取消",
            _ => "失败"
        };
        if (terminal) ClearCommand();
        if (HasPendingControl)
            StatusText = PendingControlAction switch
            {
                "pause" => "正在请求暂停",
                "resume" => "正在请求继续",
                "cancel" => "正在取消任务",
                _ => "等待控制确认"
            };
        return true;
    }

    public bool BeginControl(string requestId, string action)
    {
        if (HasPendingControl || string.IsNullOrWhiteSpace(requestId)) return false;
        PendingControlId = requestId;
        PendingControlAction = action;
        StatusText = action switch
        {
            "pause" => "正在请求暂停",
            "resume" => "正在请求继续",
            "cancel" => "正在取消任务",
            "set_mode" => "等待模式确认",
            _ => "等待设置确认"
        };
        return true;
    }

    public bool ApplyControlAck(string requestId, bool confirmed, bool paused)
    {
        if (!string.Equals(PendingControlId, requestId, StringComparison.Ordinal)) return false;
        string? action = PendingControlAction;
        PendingControlId = null;
        PendingControlAction = null;
        if (!confirmed)
        {
            StatusText = LocalPauseRequested ? "服务未确认；游戏动作暂停请求已发出" : "控制未确认";
            return true;
        }
        IsPaused = paused;
        if (action == "cancel")
        {
            ClearCommand();
            LocalPauseRequested = false;
            StatusText = "已取消";
        }
        else if (action == "pause") StatusText = paused ? "已暂停" : "暂停未生效";
        else if (action == "resume") StatusText = paused ? "仍处于暂停" : "已恢复";
        else StatusText = paused ? "已暂停" : "就绪";
        return true;
    }

    public bool FailControl(string requestId)
    {
        if (!string.Equals(PendingControlId, requestId, StringComparison.Ordinal)) return false;
        PendingControlId = null;
        PendingControlAction = null;
        StatusText = LocalPauseRequested ? "服务未确认；游戏动作暂停请求已发出" : "控制未确认";
        return true;
    }

    public bool FailSend(string commandId)
    {
        if (!string.Equals(CommandId, commandId, StringComparison.Ordinal)) return false;
        ClearCommand();
        StatusText = "发送失败";
        return true;
    }

    public void ShowConnectionProblem(string text)
    {
        StatusText = text;
    }

    public void NoteLocalPauseRequested() => LocalPauseRequested = true;

    public void NoteLocalResumed() => LocalPauseRequested = false;

    public void Reset()
    {
        ClearCommand();
        PendingControlId = null;
        PendingControlAction = null;
        IsPaused = false;
        LocalPauseRequested = false;
        StatusText = "就绪";
    }

    private void ClearCommand()
    {
        CommandId = null;
        SaveId = null;
        TurnId = null;
        _retiredTurnIds.Clear();
    }
}
