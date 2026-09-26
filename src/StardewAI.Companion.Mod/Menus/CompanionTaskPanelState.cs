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

    public void ApplyActivity(string phase, string? summary, string? nextStep)
    {
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
        Stage = planning ? "正在商量" : "正在安排";
        Current = text;
        Progress = WaitReason = null;
        NextStep = planning ? "方案出来后，回到伙伴身边确认或调整。" : "关闭面板，让游戏继续；这里会显示进度。";
        Running = true;
    }

    public void SetPlanning(string stage, string? summary, string? nextStep = null)
    {
        Stage = stage;
        if (!string.IsNullOrWhiteSpace(summary)) Current = summary;
        if (!string.IsNullOrWhiteSpace(nextStep)) NextStep = nextStep;
        Running = stage is "正在商量" or "正在安排" or "正在干活";
    }

    public void ApplyProjection(string direction, string? goal, IEnumerable<string> waiting, string? reason, bool paused)
    {
        Direction = direction;
        Goal = goal;
        WaitReason = string.Join("；", waiting.Where(s => !string.IsNullOrWhiteSpace(s)));
        if (string.IsNullOrWhiteSpace(WaitReason)) WaitReason = FriendlyWait(reason);
        if (paused)
        {
            Stage = "已暂停";
            NextStep = "点击继续可恢复，或取消当前工作。";
            Running = false;
        }
        else if (!Running && !string.IsNullOrWhiteSpace(WaitReason))
            NextStep = "可以回到对话调整安排，或等条件满足后再继续。";
    }

    public void NativeProgress(string action, string phase, int completed, int total)
    {
        Stage = phase == "已暂停" ? "已暂停" : "正在干活";
        Current = $"{phase} · {action}";
        Progress = total > 0 ? $"{action} {completed}/{total}" : null;
        WaitReason = null;
        Running = phase != "已暂停";
        NextStep = Running ? "关闭面板，让伙伴继续；需要时可暂停或取消。" : "点击继续可恢复，或取消当前工作。";
    }

    public void Complete(string text, bool failed = false)
    {
        Stage = failed ? "遇到问题" : "本次结果";
        Current = LastResult = text;
        Running = false;
        Progress = WaitReason = null;
        NextStep = failed ? "查看原因后调整安排，或回到对话继续商量。" : "可以回到对话安排下一步，或直接交代工作。";
    }

    public void Disconnected()
    {
        Stage = "连接中断";
        NextStep = "检查伙伴服务和模型登录，连接恢复后再试；原有安排仍保留。";
        Running = false;
    }

    public static string? FriendlyWait(string? reason) => reason switch
    {
        null or "" => null,
        "NO_DUE_WORK" or "no_due_work" => "当前没有到时间的准备工作。",
        "PAUSED" or "paused" => "工作已暂停。",
        "BUSY" or "busy" => "正在完成已有工作。",
        _ => reason.Any(c => c > 127) ? reason : "暂时没有可执行的下一步，回到对话看看安排。",
    };

    public void Reset()
    {
        Direction = "earn";
        Stage = "还没有新安排";
        Current = "可以先商量，也可以直接交代一件事。";
        Progress = WaitReason = LastResult = Goal = null;
        NextStep = "找伙伴聊聊，或在下面输入工作。";
        Running = false;
    }
}
