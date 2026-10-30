using System.Text.Json.Nodes;
using Microsoft.Xna.Framework;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
namespace StardewAI.Companion.Mod.Extensions;

/// <summary>Lifecycle adapter only; gameplay decisions and native operations belong to generated code.</summary>
public sealed class ExtensionExecutionMachine : ISkillExecutionMachine
{
    private readonly IFarmerActor actor;
    private readonly Func<bool> allowed;
    private IExtensionExecution? execution;
    private CancellationTokenSource? cancellation;
    private Action<string, ExtensionOutcome>? completed;
    public bool IsExecuting => execution != null;
    public bool IsPaused { get; private set; }
    public int TotalTargets => 1;
    public int CurrentTargetIndex => 0;
    public string? ActiveTaskId { get; private set; }
    public ExtensionExecutionMachine(IFarmerActor actor, Func<bool> allowed) { this.actor = actor; this.allowed = allowed; }
    public void Start(string taskId, Func<CancellationToken, IExtensionExecution> begin, Action<string, ExtensionOutcome> complete)
    {
        if (IsExecuting) throw new InvalidOperationException("Extension already running.");
        cancellation = new(); completed = complete; ActiveTaskId = taskId; actor.SetActiveTask(taskId);
        try { execution = begin(cancellation.Token); }
        catch { cancellation.Dispose(); cancellation = null; completed = null; ActiveTaskId = null; actor.SetActiveTask(null); throw; }
    }
    public void Update(GameTime? time, long tickCount)
    {
        if (!IsExecuting) return;
        if (!allowed()) { RequestCancel("Control or actor binding changed."); return; }
        if (IsPaused) return;
        try { if (execution!.Tick(time ?? new GameTime()) is { } result) Finish(result.Succeeded ? "succeeded" : "failed", result); }
        catch (Exception ex) { Finish("failed", new(false, new JsonObject(), ex.Message)); }
    }
    private void Finish(string state, ExtensionOutcome outcome)
    {
        var current = execution; execution = null;
        var callback = completed; completed = null;
        try { current?.Dispose(); } catch (Exception ex) { state = "failed"; outcome = outcome with { Succeeded = false, Error = "Extension cleanup failed: " + ex.Message }; }
        finally { cancellation?.Dispose(); cancellation = null; IsPaused = false; ActiveTaskId = null; actor.Halt(); actor.SetActiveTask(null); }
        callback?.Invoke(state, outcome);
    }
    public void RequestCancel(string reason)
    {
        if (!IsExecuting) return;
        try { cancellation?.Cancel(); } catch (Exception ex) { reason += " Cancellation: " + ex.Message; }
        try { execution!.Cancel(); } catch (Exception ex) { reason += " Cleanup: " + ex.Message; }
        Finish("cancelled", new(false, new JsonObject(), reason));
    }
    public void RequestPause() { IsPaused = true; actor.Halt(); }
    public void Resume() { if (allowed()) IsPaused = false; }
}
