using Microsoft.Xna.Framework;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>
/// Common tick-driven lifecycle shared by all skill execution state machines.
/// Enables the coordinator and ModEntry to drive, pause, resume, and cancel
/// whichever machine is currently active without skill-specific knowledge.
/// </summary>
public interface ISkillExecutionMachine
{
    bool IsExecuting { get; }
    bool IsPaused { get; }
    int TotalTargets { get; }
    int CurrentTargetIndex { get; }
    string? ActiveTaskId { get; }

    void Update(GameTime? time, long tickCount);
    void RequestCancel(string reason);
    void RequestPause();
    void Resume();
}
