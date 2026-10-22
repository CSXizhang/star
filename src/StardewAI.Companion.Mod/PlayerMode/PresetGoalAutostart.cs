using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using StardewModdingAPI;

namespace StardewAI.Companion.Mod.PlayerMode;

/// <summary>
/// Lifecycle phase of the independent real-player autostart test, surfaced on the
/// startup HUD so the run never looks like it is silently waiting for a manual F8.
/// </summary>
public enum PresetGoalPhase
{
    /// <summary>Preset loaded but readiness (save/player transport/model tool link) not met yet.</summary>
    Preparing,

    /// <summary>Dispatched through the chat pipeline; the model is reading state and planning.</summary>
    Planning,

    /// <summary>A goal is bound and native work is underway or being scheduled.</summary>
    Acting,

    /// <summary>The bound goal reached a verified terminal completed state.</summary>
    Done,

    /// <summary>The bound goal reached a verified terminal cancelled state; resuming it is impossible by design.</summary>
    Cancelled,

    /// <summary>Dispatch or the model turn failed; the pending state is retained for a safe retry.</summary>
    Failed,
}

/// <summary>
/// Independent real-player gameplay test autostart (isolated launcher opt-in only).
///
/// A persisted, editable, human-readable preset goal file (JSON: runId + goalText)
/// is dispatched through the production chat pipeline exactly as if the player had
/// typed it, once the save, player transport and model tool connection are all
/// ready. The model — never a parent/driver script — chooses the plan and calls the
/// real native action tools.
///
/// Durability contract:
/// <list type="bullet">
/// <item>The dispatch request id is a stable hash of (runId, saveId), so a bridge
/// reconnect or a game process restart never creates a duplicate goal.</item>
/// <item>A marker file under the mod data dir records dispatch/ack/done per request
/// id; an acknowledged run is owned by the runtime (goal scope) and is not re-fired.</item>
/// <item>A completed run stays complete until the run id changes (explicit new run).</item>
/// <item>F9 takeover releases authority; the preset is not restarted. An explicit
/// re-handoff resumes the already-bound goal rather than replaying finished work.</item>
/// </list>
///
/// This type holds only preset content, identity, marker persistence and phase. The
/// readiness gate, the actual send, reply routing and HUD rendering live in ModEntry.
/// When no preset file is configured the whole feature is inert (Enabled == false).
/// </summary>
public sealed class PresetGoalAutostart
{
    /// <summary>Stable request-id prefix the runtime bridge recognises as a preset run.</summary>
    public const string RequestIdPrefix = "preset-";

    private readonly Action<string, LogLevel> _log;
    private readonly string _markerPath;

    private bool _firedForThisAuthority; // in-memory; reset whenever the human takes over

    private PresetGoalAutostart(string runId, string goalText, string markerPath, Action<string, LogLevel> log)
    {
        RunId = runId;
        GoalText = goalText;
        _markerPath = markerPath;
        _log = log;
    }

    /// <summary>True when a usable preset was loaded; otherwise the feature is inert.</summary>
    public bool Enabled { get; private set; }

    /// <summary>Stable run identity. The same run id + save resumes; a new run id starts fresh.</summary>
    public string RunId { get; }

    /// <summary>The human-readable goal dispatched to the model.</summary>
    public string GoalText { get; }

    /// <summary>Stable per-run/save dispatch id, computed once the save id is known.</summary>
    public string? RequestId { get; private set; }

    /// <summary>Current lifecycle phase for the startup HUD.</summary>
    public PresetGoalPhase Phase { get; private set; } = PresetGoalPhase.Preparing;

    /// <summary>Actionable failure detail when <see cref="Phase"/> is <see cref="PresetGoalPhase.Failed"/>.</summary>
    public string? FailureReason { get; private set; }

    /// <summary>The bound goal id reported by the runtime, once acknowledged.</summary>
    public string? GoalId { get; private set; }

    public int? StartDay { get; private set; }
    public int MaxGameDays { get; private set; } = 1;
    // A legacy dispatched run without a durable start is fail-closed. The runtime
    // reconciles its actual sleep ledger; it never grants a fresh day allowance.
    // A transient Failed phase (backend error, a turn that ended before binding the
    // goal) must NOT consume the day budget: the window below is the only budget rule,
    // matching the runtime-side preset day boundary gate.
    public bool BusinessAllowed(int nativeDay) => Enabled && !_completed && !_cancelled
        && StartDay.HasValue && nativeDay >= StartDay.Value && nativeDay < StartDay.Value + MaxGameDays;
    public void InitializeNativeDay(int nativeDay)
    {
        if (!StartDay.HasValue && !_dispatched) StartDay = nativeDay;
        SaveMarker();
    }

    private bool _dispatched;
    private bool _acknowledged;
    private bool _completed;
    private bool _cancelled;
    private bool _dispatchFailed;

    /// <summary>
    /// Loads the preset from <paramref name="filePath"/> and binds it to this save.
    /// Returns a disabled instance (never null) when the path is absent or the file
    /// cannot be parsed; a malformed file surfaces as a Failed phase instead of
    /// silently doing nothing.
    /// </summary>
    public static PresetGoalAutostart Load(string? filePath, string saveId, string markerPath, Action<string, LogLevel> log)
    {
        if (string.IsNullOrWhiteSpace(filePath))
            return Disabled(markerPath, log);

        string runId;
        string goalText;
        int maxGameDays;
        try
        {
            string raw = File.ReadAllText(filePath);
            JsonObject node = JsonNode.Parse(raw) as JsonObject
                ?? throw new InvalidDataException("preset root is not an object");
            runId = (node["runId"]?.GetValue<string>() ?? "").Trim();
            goalText = (node["goalText"]?.GetValue<string>() ?? "").Trim();
            maxGameDays = node["maxGameDays"]?.GetValue<int>() ?? 1;
            if (maxGameDays < 1 || maxGameDays > 28) throw new InvalidDataException("maxGameDays must be 1..28");
            if (runId.Length == 0)
                throw new InvalidDataException("preset runId is empty");
            if (goalText.Length == 0)
                throw new InvalidDataException("preset goalText is empty");
        }
        catch (Exception ex)
        {
            log($"Preset goal file could not be used ({filePath}): {ex.Message}", LogLevel.Warn);
            var broken = new PresetGoalAutostart("invalid", "", markerPath, log) { Enabled = true };
            broken.Phase = PresetGoalPhase.Failed;
            broken.FailureReason = $"预设目标文件无效：{ex.Message}。请修正 {filePath} 后重开。";
            return broken;
        }

        var preset = new PresetGoalAutostart(runId, goalText, markerPath, log) { Enabled = true, MaxGameDays = maxGameDays };
        preset.RequestId = RequestIdPrefix + StableHash(runId + "\n" + saveId);
        preset.LoadMarker();
        return preset;
    }

    private static PresetGoalAutostart Disabled(string markerPath, Action<string, LogLevel> log)
        => new("", "", markerPath, log) { Enabled = false };

    private static string StableHash(string value)
    {
        byte[] hash = SHA256.HashData(Encoding.UTF8.GetBytes(value));
        var builder = new StringBuilder(12);
        for (int i = 0; i < 6; i++)
            builder.Append(hash[i].ToString("x2"));
        return builder.ToString();
    }

    /// <summary>Whether <paramref name="requestId"/> belongs to this preset run.</summary>
    public bool Matches(string? requestId)
        => Enabled && RequestId != null && string.Equals(RequestId, requestId, StringComparison.Ordinal);

    /// <summary>
    /// In-memory one-shot gate for the current authority session. Returns true at most
    /// once per agent-ownership grant; a human takeover resets it so an explicit
    /// re-handoff resumes the run. Persisted completion still vetoes a fire.
    /// </summary>
    public bool ShouldDispatchNow()
    {
        if (!Enabled || RequestId == null || _completed || _cancelled || _firedForThisAuthority)
            return false;
        if (Phase == PresetGoalPhase.Failed && _acknowledged && !_dispatchFailed)
            return false; // A model-side failure is surfaced; retry means an explicit new run.
        return true;
    }

    /// <summary>Records that the preset was sent for this authority session.</summary>
    public void MarkDispatched()
    {
        _firedForThisAuthority = true;
        _dispatched = true;
        if (Phase != PresetGoalPhase.Failed || _dispatchFailed)
            Phase = PresetGoalPhase.Planning;
        SaveMarker();
    }

    /// <summary>Records a send failure so the run stays pending and retryable.</summary>
    public void MarkDispatchFailed(string reason)
    {
        if (_completed || _cancelled) return;
        _dispatchFailed = true;
        _firedForThisAuthority = false; // allow the readiness loop to retry the send
        Phase = PresetGoalPhase.Failed;
        FailureReason = reason;
        SaveMarker();
    }

    /// <summary>F9 human takeover: stop auto-dispatch until an explicit re-handoff.</summary>
    public void NoteAuthorityHuman()
    {
        _firedForThisAuthority = false;
    }

    /// <summary>
    /// Applies a chat reply for this preset's request id. The first reply is the
    /// durable acknowledgement (the runtime recorded the request). The ONLY run
    /// completion authority is the bound WorkStore goal's verified terminal state,
    /// reported on the dedicated <paramref name="goalTerminal"/> field
    /// ("completed"/"cancelled"): generic reply status and commandComplete describe
    /// one chat turn or one selected step, never the whole run. A finished turn that
    /// never bound a goal is a surfaced missing-plan failure, not a silent farm day.
    /// </summary>
    public void NoteReply(string status, bool? commandComplete, string? goalId, string? goalTerminal)
    {
        // A matching reply proves this send has recovered. A transient wire
        // failure must not mask live work or prevent an acknowledged run resuming.
        if (_dispatchFailed && !_completed && !_cancelled)
        {
            _dispatchFailed = false;
            FailureReason = null;
            Phase = PresetGoalPhase.Planning;
        }
        if (!string.IsNullOrWhiteSpace(goalId))
            GoalId = goalId;
        if (!_acknowledged)
        {
            _acknowledged = true;
            if (Phase == PresetGoalPhase.Preparing || Phase == PresetGoalPhase.Planning)
                Phase = PresetGoalPhase.Acting;
        }
        if (goalTerminal == "completed")
        {
            _completed = true;
            Phase = PresetGoalPhase.Done;
        }
        else if (goalTerminal == "cancelled")
        {
            _cancelled = true;
            Phase = PresetGoalPhase.Cancelled;
            FailureReason = "本轮目标已被显式取消（终态），不会自动续跑；确认后用新一轮（-NewRun）重新开始。";
        }
        else if (status == "job-failed" && GoalId != null)
        {
            // A short native action can fail (closed shop, blocked tile).
            // Its receipt is input for the next model decision, not a run terminal.
            Phase = PresetGoalPhase.Planning;
            FailureReason = null;
        }
        else if (status == "failed")
        {
            Phase = PresetGoalPhase.Failed;
            FailureReason = "模型或执行未成功（" + status + "）；检查后台连接与额度后，用新的一次运行重试。";
        }
        else if (status is "job-running" or "selected" or "processing")
        {
            if (Phase is not (PresetGoalPhase.Done or PresetGoalPhase.Cancelled or PresetGoalPhase.Failed))
                Phase = PresetGoalPhase.Acting;
        }
        else if (GoalId == null && status is not "cancelled" &&
                 (commandComplete == true || status is "completed" or "decision-completed"))
        {
            // The turn finished without binding any WorkStore goal: a missing plan,
            // never a silently successful farm day. A turn cancelled by human
            // takeover is exempt so an explicit re-handoff can still resume.
            Phase = PresetGoalPhase.Failed;
            FailureReason = "模型本轮没有建立任何工作计划（无绑定目标），不能算作完成的农场日；检查模型输出后用新一轮重试。";
        }
        SaveMarker();
    }

    /// <summary>Marks the bound goal complete (reported by the runtime out-of-band).</summary>
    public void MarkGoalComplete()
    {
        _completed = true;
        Phase = PresetGoalPhase.Done;
        SaveMarker();
    }

    /// <summary>Reflects live native activity into the Acting/Planning distinction.</summary>
    public void NoteNativeActivity(bool nativeActive)
    {
        if (nativeActive && _dispatchFailed && _acknowledged && GoalId != null && !_completed && !_cancelled)
        {
            _dispatchFailed = false;
            FailureReason = null;
            Phase = PresetGoalPhase.Acting;
            SaveMarker();
        }
        if (!Enabled || _completed || _cancelled || Phase is PresetGoalPhase.Failed or PresetGoalPhase.Cancelled)
            return;
        if (!_acknowledged)
            return; // still preparing/planning until the runtime acknowledges
        Phase = nativeActive ? PresetGoalPhase.Acting : PresetGoalPhase.Planning;
    }

    /// <summary>Short Chinese HUD label for the current phase.</summary>
    public string HudLabel()
    {
        return Phase switch
        {
            PresetGoalPhase.Preparing => "自动测试·准备中（等待存档/连接/AI 授权）",
            PresetGoalPhase.Planning => "自动测试·规划中（模型读取真实状态）",
            PresetGoalPhase.Acting => "自动测试·执行中",
            PresetGoalPhase.Done => "自动测试·已完成",
            PresetGoalPhase.Cancelled => "自动测试·已取消（目标终态，需新一轮）",
            PresetGoalPhase.Failed => "自动测试·失败",
            _ => "自动测试",
        };
    }

    private void LoadMarker()
    {
        try
        {
            if (RequestId == null || !File.Exists(_markerPath))
                return;
            JsonObject root = JsonNode.Parse(File.ReadAllText(_markerPath)) as JsonObject ?? new JsonObject();
            JsonObject? record = root[RequestId] as JsonObject;
            if (record == null)
                return;
            StartDay = record["startDay"]?.GetValue<int>();
            MaxGameDays = record["maxGameDays"]?.GetValue<int>() ?? MaxGameDays;
            _dispatched = record["dispatched"]?.GetValue<bool>() ?? false;
            _acknowledged = record["acknowledged"]?.GetValue<bool>() ?? false;
            _completed = record["completed"]?.GetValue<bool>() ?? false;
            _cancelled = record["cancelled"]?.GetValue<bool>() ?? false;
            GoalId = record["goalId"]?.GetValue<string>() ?? GoalId;
            string? failed = record["failedReason"]?.GetValue<string>();
            _dispatchFailed = record["dispatchFailed"]?.GetValue<bool>()
                ?? (failed?.StartsWith("预设目标发送失败", StringComparison.Ordinal) == true);
            if (_completed)
                Phase = PresetGoalPhase.Done;
            else if (_cancelled)
            {
                Phase = PresetGoalPhase.Cancelled;
                FailureReason = failed ?? "本轮目标已被取消（终态）。";
            }
            else if (!string.IsNullOrWhiteSpace(failed) && failed.Contains("job-failed") && _acknowledged && GoalId != null)
            {
                Phase = PresetGoalPhase.Planning;
                FailureReason = null;
            }
            else if (!string.IsNullOrWhiteSpace(failed))
            {
                Phase = PresetGoalPhase.Failed;
                FailureReason = failed;
            }
            else if (_acknowledged)
                Phase = PresetGoalPhase.Acting;
            else if (_dispatched)
                Phase = PresetGoalPhase.Planning;
        }
        catch (Exception ex)
        {
            _log($"Preset goal marker could not be read ({_markerPath}): {ex.Message}", LogLevel.Debug);
        }
    }

    private void SaveMarker()
    {
        if (!Enabled || RequestId == null || string.IsNullOrWhiteSpace(_markerPath))
            return;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_markerPath)!);
            JsonObject root = new();
            if (File.Exists(_markerPath))
            {
                try { root = JsonNode.Parse(File.ReadAllText(_markerPath)) as JsonObject ?? new JsonObject(); }
                catch { root = new JsonObject(); }
            }
            root[RequestId] = new JsonObject
            {
                ["startDay"] = StartDay,
                ["maxGameDays"] = MaxGameDays,
                ["runId"] = RunId,
                ["requestId"] = RequestId,
                ["goalText"] = GoalText,
                ["dispatched"] = _dispatched,
                ["acknowledged"] = _acknowledged,
                ["completed"] = _completed,
                ["cancelled"] = _cancelled,
                ["dispatchFailed"] = _dispatchFailed,
                ["goalId"] = GoalId,
                ["failedReason"] = Phase is PresetGoalPhase.Failed or PresetGoalPhase.Cancelled ? FailureReason : null,
                ["updatedAtUtc"] = DateTime.UtcNow.ToString("o"),
            };
            string temp = _markerPath + ".tmp";
            File.WriteAllText(temp, root.ToJsonString());
            File.Move(temp, _markerPath, true);
        }
        catch (Exception ex)
        {
            _log($"Preset goal marker could not be saved ({_markerPath}): {ex.Message}", LogLevel.Debug);
        }
    }
}
