using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>One player-facing projection shared by NPC planning and the task panel.</summary>
public sealed class CompanionTaskPanelState
{
    public string Direction { get; set; } = "earn";
    public string Stage { get; private set; } = "还没有新安排";
    public string Current { get; private set; } = "可以先商量，也可以直接交代一件事。";
    public string? Progress { get; private set; }
    public string? WaitReason { get; private set; }
    public string? LastResult { get; private set; }
    public string NextStep { get; private set; } = "找伙伴聊聊，或在下面输入工作。";
    public string? Goal { get; private set; }
    public bool Running { get; private set; }
    public string? UsageTodayText { get; private set; }
    public string? UsageSaveTodayText { get; private set; }
    public string? UsageSaveTotalText { get; private set; }
    public string? ConnectionProblem { get; private set; }
    private string? _connectionStage;
    private string? _stageBeforeConnectionProblem;
    private string? _nextBeforeConnectionProblem;
    private bool _hasWorkingActivity;
    private bool _paused;
    private bool _cancelled;
    private string? _pauseReason;
    public string? UpdatedAt { get; private set; }
    public IReadOnlyList<string> OtherGoals { get; private set; } = Array.Empty<string>();
    public IReadOnlyList<RecentExecutionDto> RecentExecutions { get; private set; } = Array.Empty<RecentExecutionDto>();

    public void ApplyUsageToday(string? text)
    {
        // Older runtimes omit the summary; an explicit empty string clears it.
        if (text is not null) UsageTodayText = string.IsNullOrWhiteSpace(text) ? null : text;
    }

    public void ApplySaveUsage(string? today, string? total)
    {
        if (today != null) UsageSaveTodayText = today;
        if (total != null) UsageSaveTotalText = total;
    }

    public string UsageSummary => string.Join("\n\n", new[] { UsageTodayText, UsageSaveTodayText, UsageSaveTotalText }
        .Where(t => !string.IsNullOrWhiteSpace(t)));

    public void ApplyChatReply(ChatReplyPayload reply)
    {
        // Conversation completes its own request, but is not a work receipt.
        if (reply.ReadOnly == true || reply.ResumeWorkRequested == true) return;
        if (reply.Status is "job-completed" or "job-failed" or "completed" or "failed" or "cancelled")
            Complete(reply.ReplyText, reply.Status is "failed" or "job-failed");
        else if (reply.Status == "processing")
            SetPlanning("正在安排", reply.ReplyText, "伙伴会边聊边做，有进展会更新这里。");
        else if (reply.Status.StartsWith("job-", StringComparison.Ordinal))
            SetPlanning("正在干活", reply.ReplyText, "伙伴正在工作，需要时可暂停或取消。");
        else if (reply.Status is "selected" or "decision-completed")
            SetPlanning(reply.CommandComplete == true ? "本次结果" : "正在安排", reply.ReplyText);
    }

    public void ApplyExecutionHistory(IEnumerable<RecentExecutionDto>? records)
    {
        // Old runtimes omit this projection; don't erase a displayed log on unrelated refreshes.
        if (records is null) return;
        RecentExecutions = records.Where(r => !string.IsNullOrWhiteSpace(r.CommandId))
            .GroupBy(r => r.CommandId, StringComparer.Ordinal).Select(g => g.Last()).TakeLast(8).ToList();
        if (RecentExecutions.LastOrDefault() is { } last)
            LastResult = $"{last.TaskTitle}：{last.Summary}";
    }

    public static string ExecutionOutcomeName(string outcome) => outcome switch
    {
        "succeeded" or "completed" => "完成", "partially-succeeded" or "partial" => "部分完成", "failed" => "失败",
        "rejected" => "未执行", "cancelled" => "已取消", _ => "结果待核对",
    };

    public static string ExecutionDateName(string? date)
    {
        var parts = date?.Split(':');
        if (parts is not { Length: 3 } || !int.TryParse(parts[0], out int year) || !int.TryParse(parts[2], out int day))
            return "日期未记录";
        string season = parts[1].ToLowerInvariant() switch { "spring" => "春", "summer" => "夏", "fall" => "秋", "winter" => "冬", _ => "" };
        return season.Length == 0 ? "日期未记录" : $"第{year}年 {season}{day}日";
    }

    public static ActiveGoalDto? SelectCurrentGoal(CompanionWorkStateDto work) =>
        work.ActiveGoals?.FirstOrDefault(g => g.Id == work.CurrentGoalId && IsActiveGoal(g));

    public static bool IsActiveGoal(ActiveGoalDto goal) => goal.Status is "active" or "pending" or "in-progress" or "waiting";
    public static string GoalText(ActiveGoalDto goal) => string.IsNullOrWhiteSpace(goal.Text) ? goal.Summary ?? "" : goal.Text;
    public static string? CurrentGoalText(CompanionWorkStateDto work) => SelectCurrentGoal(work) is { } goal ? GoalText(goal) : work.Goal;

    public static string LocalTimestamp(string? timestamp) => DateTimeOffset.TryParse(timestamp, out var value)
        ? value.ToLocalTime().ToString("MM-dd HH:mm:ss") : "记录时间未知";

    public static string ExecutionRecordText(RecentExecutionDto record)
    {
        string date = ExecutionDateName(record.GameDate);
        if (record.GameTime is { } time) date += $" {time / 100:00}:{time % 100:00}";
        string text = $"{date} · 本地 {LocalTimestamp(record.RecordedAt)}\n{record.TaskTitle} · {ExecutionOutcomeName(record.Outcome)}\n{record.Summary}";
        if (!string.IsNullOrWhiteSpace(record.Reason) && !record.Summary.Contains(record.Reason, StringComparison.Ordinal))
            text += $"\n原因：{record.Reason}";
        else if (string.IsNullOrWhiteSpace(record.Reason) && !string.IsNullOrWhiteSpace(record.ReasonCode))
            text += $"\n原因：{record.ReasonCode}";
        return text;
    }

    public void ApplyActivity(string phase, string? summary, string? nextStep)
    {
        if (_paused)
        {
            if (phase is "completed" or "failed" && !string.IsNullOrWhiteSpace(summary)) LastResult = summary;
            ShowPause();
            return;
        }
        _hasWorkingActivity = phase == "working";
        string stage = phase switch
        {
            "planning" => "正在商量", "proposed" => "等你确认", "working" => "正在干活",
            "waiting" => "正在等待", "paused" => "已暂停", "completed" => "已完成",
            "failed" => "遇到问题", _ => "暂时待命",
        };
        // A fresh idle snapshot must not erase the last actual outcome.
        if (phase == "idle" && LastResult != null && !Running) return;
        SetPlanning(stage, summary, nextStep);
        if (phase != "working") Progress = null;
        if (phase is "completed" or "failed")
        {
            if (!string.IsNullOrWhiteSpace(summary)) LastResult = summary;
            Progress = null;
        }
        if (phase is "waiting" or "paused") WaitReason = summary;
        else WaitReason = null;
    }

    public static string DirectionName(string direction) => direction switch
    {
        "community" => "社区献祭", "decor" => "农场装修", "workhorse" => "日常干活", _ => "赚钱经营",
    };

    public void Begin(string text, bool planning)
    {
        if (_paused) { ShowPause(); return; }
        _cancelled = false;
        _hasWorkingActivity = false;
        Stage = planning ? "正在商量" : "正在安排";
        Current = text;
        Progress = WaitReason = null;
        NextStep = planning ? "方案出来后，回到伙伴身边确认或调整。" : "这里会持续显示工作进度。";
        Running = true;
    }

    public void SetPlanning(string stage, string? summary, string? nextStep = null)
    {
        if (_paused) { ShowPause(); return; }
        _cancelled = false;
        Stage = stage;
        if (!string.IsNullOrWhiteSpace(summary)) Current = summary;
        if (!string.IsNullOrWhiteSpace(nextStep)) NextStep = nextStep;
        Running = stage is "正在商量" or "正在安排" or "正在干活";
    }

    public void ApplyProjection(string direction, string? goal, IEnumerable<string> waiting, string? reason, bool paused,
        string? pauseReason = null, string? updatedAt = null, IEnumerable<string>? otherGoals = null)
    {
        _paused = paused;
        _pauseReason = pauseReason;
        UpdatedAt = updatedAt;
        OtherGoals = otherGoals?.Where(s => !string.IsNullOrWhiteSpace(s) && s != goal).Distinct().Take(5).ToArray() ?? Array.Empty<string>();
        Direction = direction;
        Goal = _cancelled ? null : goal;
        WaitReason = string.Join("；", waiting.Where(s => !string.IsNullOrWhiteSpace(s)));
        if (string.IsNullOrWhiteSpace(WaitReason)) WaitReason = FriendlyWait(reason);
        if (paused)
        {
            ShowPause();
        }
        else if (!Running && !string.IsNullOrWhiteSpace(WaitReason))
            NextStep = "可以回到对话调整安排，或等条件满足后再继续。";
    }

    public void ApplyConfirmedPause(bool paused)
    {
        bool wasPaused = _paused;
        _paused = paused;
        if (paused) ShowPause();
        else if (wasPaused)
        {
            if (_cancelled) { ShowCancelled(); return; }
            _hasWorkingActivity = false;
            Stage = "等待继续";
            Current = "伙伴已恢复，等待下一项实际进度。";
            Progress = WaitReason = null;
            Running = false;
            NextStep = "原有安排保留，收到实际进度后继续显示。";
        }
    }

    public void NativeProgress(string action, string phase, int completed, int total)
    {
        if (_paused) { ShowPause(); return; }
        _cancelled = false;
        if (_hasWorkingActivity)
        {
            // Native progress describes one step, while the saved work activity
            // names the whole job. A finished step must not replace the last job
            // result or claim that a multi-step job has already finished.
            Stage = phase == "已暂停" ? "已暂停" : "正在干活";
            Progress = total > 0 ? $"{action} {completed}/{total}" : null;
            Running = phase != "已暂停";
            return;
        }
        if (phase is "已完成" or "部分完成" or "未完成" or "cancelled")
        {
            string quantity = total > 0 ? $" {completed}/{total}" : string.Empty;
            string outcome = phase == "cancelled" ? "已取消" : phase;
            Complete($"{action}{quantity} · {outcome}", failed: phase != "已完成");
            return;
        }
        Stage = phase == "已暂停" ? "已暂停" : "正在干活";
        Current = $"{phase} · {action}";
        Progress = total > 0 ? $"{action} {completed}/{total}" : null;
        WaitReason = null;
        Running = phase != "已暂停";
        NextStep = Running ? "伙伴正在工作，需要时可暂停或取消。" : "点击继续可恢复，或取消当前工作。";
    }

    public void SetRestState(string state, string? reason)
    {
        if (_paused) { ShowPause(); return; }
        Stage = state switch { "sleeping" => "已就寝", "returning-home" => "回家", "winding-down" => "收尾中", _ => "等待休息" };
        Current = reason ?? (state == "sleeping" ? "伙伴已经上床休息。" : "准备回家睡觉，剩下的工作明天继续。");
        Progress = null;
        WaitReason = reason;
        Running = state is "winding-down" or "returning-home";
        NextStep = "明天醒来后继续安排。";
    }

    public void Complete(string text, bool failed = false)
    {
        _hasWorkingActivity = false;
        Stage = failed ? "遇到问题" : "本次结果";
        Current = LastResult = text;
        Running = false;
        Progress = WaitReason = null;
        NextStep = failed ? "查看原因后调整安排，或回到对话继续商量。" : "可以回到对话安排下一步，或直接交代工作。";
        if (_paused) ShowPause();
    }

    private void ShowPause()
    {
        Stage = _pauseReason?.Contains("模型", StringComparison.Ordinal) == true || _pauseReason?.Contains("失败", StringComparison.Ordinal) == true ? "故障暂停" : "已暂停";
        Current = WaitReason = _pauseReason ?? (_cancelled ? "当前安排已取消；伙伴仍处于暂停。" : "伙伴工作已暂停，已保存的安排仍保留。");
        Progress = null;
        NextStep = _cancelled ? "可以交代新工作；点击继续只解除暂停。" : "点击继续可恢复，或取消当前工作。";
        Running = false;
    }

    public void ApplyConfirmedCancel()
    {
        _cancelled = true;
        _hasWorkingActivity = false;
        Goal = null;
        ShowCancelled();
    }

    private void ShowCancelled()
    {
        if (_paused) { ShowPause(); return; }
        Stage = "已取消";
        Current = "当前安排已取消。";
        Progress = WaitReason = null;
        Running = false;
        NextStep = "可以交代新工作，或调整主动帮忙的偏好。";
    }

    public void Disconnected(string? reason = null, bool startupFailed = false)
    {
        _hasWorkingActivity = false;
        ConnectionProblem = reason ?? "伙伴服务尚未连接。";
        if (_connectionStage == null)
        {
            _stageBeforeConnectionProblem = Stage;
            _nextBeforeConnectionProblem = NextStep;
        }
        _connectionStage = startupFailed ? "启动失败" : "连接中断";
        if (!_paused)
        {
            Stage = _connectionStage;
            NextStep = "检查伙伴服务和模型登录，连接恢复后再试；原有安排仍保留。";
        }
        Running = false;
    }

    public void Connected()
    {
        ConnectionProblem = null;
        if (Stage == _connectionStage)
        {
            Stage = _stageBeforeConnectionProblem ?? "暂时待命";
            NextStep = _nextBeforeConnectionProblem ?? "回到对话继续商量。";
        }
        _connectionStage = _stageBeforeConnectionProblem = _nextBeforeConnectionProblem = null;
    }

    public static string? FriendlyWait(string? reason) => reason switch
    {
        null or "" => null,
        "NO_DUE_WORK" or "no_due_work" => "当前没有到时间的准备工作。",
        "PAUSED" or "paused" => "工作已暂停。",
        "BUSY" or "busy" => "当前任务名称暂未同步。",
        _ => reason.Any(c => c > 127) ? reason : "暂时没有可执行的下一步，回到对话看看安排。",
    };

    public void Reset()
    {
        _paused = false;
        _cancelled = false;
        _hasWorkingActivity = false;
        _pauseReason = UpdatedAt = null;
        OtherGoals = Array.Empty<string>();
        Direction = "earn";
        Stage = "还没有新安排";
        Current = "可以先商量，也可以直接交代一件事。";
        Progress = WaitReason = LastResult = Goal = null;
        NextStep = "找伙伴聊聊，或在下面输入工作。";
        Running = false;
        RecentExecutions = Array.Empty<RecentExecutionDto>();
        UsageTodayText = null;
        UsageSaveTodayText = UsageSaveTotalText = null;
        ConnectionProblem = _connectionStage = _stageBeforeConnectionProblem = _nextBeforeConnectionProblem = null;
    }
}
