namespace StardewAI.Companion.Mod.Transport;

/// <summary>
/// Handler invoked on the Stardew Valley main game thread when transport messages arrive.
/// Network threads MUST NOT call these methods directly; they are dispatched via the
/// main-thread bounded queue during game update ticks.
/// </summary>
public interface ITransportHandler
{
    /// <summary>
    /// Invoked on the game main thread when a skill.execute command is dequeued.
    /// Returns true if accepted; false if rejected by preflight validation.
    /// </summary>
    bool TryAcceptSkillExecute(
        SkillExecutePayload payload,
        EnvelopeDto envelope,
        out SkillResultPayload? rejectResult
    );

    /// <summary>
    /// Invoked on the game main thread when a skill.cancel command is dequeued.
    /// </summary>
    void OnCancelSkill(SkillCancelPayload payload, EnvelopeDto envelope);

    /// <summary>
    /// Invoked on the game main thread when a skill.pause command is dequeued.
    /// </summary>
    void OnPauseSkill(SkillPausePayload payload, EnvelopeDto envelope);

    /// <summary>
    /// Invoked on the game main thread when a skill.resume command is dequeued.
    /// </summary>
    void OnResumeSkill(SkillResumePayload payload, EnvelopeDto envelope);

    /// <summary>
    /// Invoked on the game main thread to capture the authoritative world & companion state.
    /// </summary>
    WorldSnapshotPayload CaptureCurrentSnapshot(long worldRevision);

    /// <summary>
    /// Invoked on the game main thread when the client WebSocket disconnects or session is lost.
    /// </summary>
    void OnTransportDisconnected(string reason);
}
