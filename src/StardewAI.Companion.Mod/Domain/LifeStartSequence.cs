namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// §1.8 "开始一起生活" start sequence, expressed as pure logic so it can be
/// unit-tested without any game types.
///
/// Required order (contract §1.8): ① persist the companion profile
/// (<c>life.profile.set</c>, onboarded=true) and wait for its
/// <c>life.profile.state</c> confirmed ack; ② confirm the goal/budget via
/// <c>autonomy.control set_preferences</c> (goal mapped from play-style) and wait
/// for its ack; ③ only then switch to free mode via <c>autonomy.control
/// set_mode</c> and wait for its ack. The caller (ModEntry) performs the actual
/// sends and renders the outcome; this class only decides which step the
/// sequence is on and what to send next. Any rejected ack aborts the sequence
/// while leaving the real persisted state untouched, so the player can retry.
/// </summary>
public sealed class LifeStartSequence
{
    /// <summary>Current position of the sequence.</summary>
    public LifeStartStep Step { get; private set; } = LifeStartStep.Idle;

    /// <summary>Save the sequence was started for (<c>null</c> when idle).</summary>
    public string? SaveId { get; private set; }

    /// <summary>Companion name captured at start; retained after completion for the celebration message.</summary>
    public string? CompanionName { get; private set; }

    /// <summary>Request ID of the in-flight <c>life.profile.set</c>.</summary>
    public string? ProfileRequestId { get; private set; }

    /// <summary>Request ID of the in-flight <c>set_preferences</c> control.</summary>
    public string? GoalRequestId { get; private set; }

    /// <summary>Request ID of the in-flight <c>set_mode</c> control.</summary>
    public string? ModeRequestId { get; private set; }

    /// <summary>Goal text to send with <c>set_preferences</c> once the profile is confirmed.</summary>
    public string? Goal { get; private set; }

    /// <summary>Human-readable reason set when the last call returned <see cref="LifeStartAdvance.Abort"/>.</summary>
    public string? AbortReason { get; private set; }

    /// <summary>UTC timestamp of the last step send; the caller owns the timeout check.</summary>
    public DateTime StepSentAtUtc { get; set; }

    private string _playStyle = "earn";

    /// <summary>
    /// Starts the sequence at step ①. Does nothing if a sequence is already in flight.
    /// </summary>
    public void Begin(string saveId, string companionName, string playStyle, string profileRequestId)
    {
        if (Step != LifeStartStep.Idle) return;
        SaveId = saveId;
        CompanionName = companionName;
        _playStyle = playStyle;
        ProfileRequestId = profileRequestId;
        GoalRequestId = null;
        ModeRequestId = null;
        Goal = null;
        AbortReason = null;
        Step = LifeStartStep.AwaitProfileConfirm;
    }

    /// <summary>
    /// Feeds the <c>life.profile.state</c> ack for step ①.
    /// On confirmation advances to step ② (<see cref="LifeStartAdvance.SendGoalPreference"/>);
    /// on rejection aborts; anything else is ignored.
    /// </summary>
    public LifeStartAdvance ApplyProfileState(string requestId, string status, string? reason)
    {
        if (Step != LifeStartStep.AwaitProfileConfirm ||
            !string.Equals(ProfileRequestId, requestId, StringComparison.Ordinal))
            return LifeStartAdvance.Ignore;

        if (!string.Equals(status, "confirmed", StringComparison.OrdinalIgnoreCase))
        {
            AbortReason = string.IsNullOrWhiteSpace(reason) ? "设置未确认。" : $"设置未确认：{reason}";
            Abort();
            return LifeStartAdvance.Abort;
        }

        Goal = MapGoal(_playStyle);
        GoalRequestId = NewRequestId();
        Step = LifeStartStep.AwaitGoalAck;
        return LifeStartAdvance.SendGoalPreference;
    }

    /// <summary>
    /// Feeds an <c>autonomy.control</c> ack for steps ②/③.
    /// Step ② confirmation advances to step ③ (<see cref="LifeStartAdvance.SendModeFree"/>);
    /// step ③ confirmation completes the sequence (<see cref="LifeStartAdvance.Complete"/>);
    /// a rejection aborts; anything else is ignored.
    /// </summary>
    public LifeStartAdvance ApplyControlAck(string requestId, string status)
    {
        bool confirmed = string.Equals(status, "confirmed", StringComparison.OrdinalIgnoreCase);

        if (Step == LifeStartStep.AwaitGoalAck &&
            string.Equals(GoalRequestId, requestId, StringComparison.Ordinal))
        {
            if (!confirmed)
            {
                AbortReason = "目标设置未确认。";
                Abort();
                return LifeStartAdvance.Abort;
            }

            ModeRequestId = NewRequestId();
            Step = LifeStartStep.AwaitModeAck;
            return LifeStartAdvance.SendModeFree;
        }

        if (Step == LifeStartStep.AwaitModeAck &&
            string.Equals(ModeRequestId, requestId, StringComparison.Ordinal))
        {
            if (!confirmed)
            {
                AbortReason = "自由模式未确认。";
                Abort();
                return LifeStartAdvance.Abort;
            }

            Finish();
            return LifeStartAdvance.Complete;
        }

        return LifeStartAdvance.Ignore;
    }

    /// <summary>Aborts an in-flight sequence (no-op when idle) and drops the companion name.</summary>
    public void Abort()
    {
        if (Step == LifeStartStep.Idle) return;
        Finish();
        CompanionName = null;
    }

    /// <summary>
    /// Maps a play-style key to the Chinese goal text for <c>set_preferences</c>.
    /// community/decor are planning-stage capabilities and must be labeled as such.
    /// </summary>
    public static string MapGoal(string playStyle) => playStyle switch
    {
        "earn" => "优先赚钱：收获出货、按需补种，遵守每日购买上限",
        "workhorse" => "任劳任怨：浇水除草收获、喂动物、收机器成品等日常杂务",
        "community" => "社区中心献祭（规划中）",
        "decor" => "农场装修（规划中）",
        _ => "优先赚钱：收获出货、按需补种，遵守每日购买上限",
    };

    private void Finish()
    {
        Step = LifeStartStep.Idle;
        SaveId = null;
        ProfileRequestId = null;
        GoalRequestId = null;
        ModeRequestId = null;
        Goal = null;
    }

    private static string NewRequestId() => Guid.NewGuid().ToString("N")[..8];
}

/// <summary>Position of the §1.8 start sequence.</summary>
public enum LifeStartStep
{
    /// <summary>No sequence in flight.</summary>
    Idle,
    /// <summary>Step ①: <c>life.profile.set</c> sent; awaiting its confirmed ack.</summary>
    AwaitProfileConfirm,
    /// <summary>Step ②: <c>set_preferences</c> (goal) sent; awaiting its ack.</summary>
    AwaitGoalAck,
    /// <summary>Step ③: <c>set_mode</c> (free) sent; awaiting its ack.</summary>
    AwaitModeAck,
}

/// <summary>Decision produced by feeding an ack into <see cref="LifeStartSequence"/>.</summary>
public enum LifeStartAdvance
{
    /// <summary>The ack does not belong to the current step; do nothing.</summary>
    Ignore,
    /// <summary>The current step was rejected; the sequence has been aborted, see <see cref="LifeStartSequence.AbortReason"/>.</summary>
    Abort,
    /// <summary>Profile confirmed; send <c>set_preferences</c> with <see cref="LifeStartSequence.Goal"/>.</summary>
    SendGoalPreference,
    /// <summary>Goal confirmed; send <c>set_mode</c> with mode "free".</summary>
    SendModeFree,
    /// <summary>Free-mode ack confirmed; the sequence is complete.</summary>
    Complete,
}
