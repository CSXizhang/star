using Microsoft.Xna.Framework;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>One model-selected idle action. Only observes the native clock; never advances it.</summary>
public sealed class WaitStateMachine : ISkillExecutionMachine
{
    private readonly IFarmerActor _actor;
    private readonly IWorldObserver _observer;
    private readonly Func<string?> _blocked;
    private SkillExecutePayload? _request;
    private string _season = "";
    private int _day, _startTime, _lastTime, _elapsed;
    private string? _cancelReason;

    public WaitStateMachine(IFarmerActor actor, IWorldObserver observer, Func<string?> blocked)
        => (_actor, _observer, _blocked) = (actor, observer, blocked);

    public event Action<SkillResultPayload>? Completed;
    public bool IsExecuting => _request is not null;
    public bool IsPaused { get; private set; }
    public int TotalTargets => 0;
    public int CurrentTargetIndex => 0;
    public string? ActiveTaskId => _request?.TaskId;

    public bool Start(SkillExecutePayload request, out SkillResultPayload? rejection)
    {
        string? error = IsExecuting ? "Another wait is already active."
            : request.Parameters.WaitMinutes is not int minutes || minutes < 1 || minutes > 1440
                ? "waitMinutes must be an integer from 1 to 1440."
                : _blocked();
        rejection = error is null ? null : new(request.CommandId, request.TaskId, "rejected", 0, 0, 0,
            _observer.WorldRevision, new(), Error: new("WAIT_UNAVAILABLE", error), SkillId: "wait");
        if (error is not null) return false;
        _request = request;
        _season = _observer.CurrentSeason;
        _day = _observer.DayOfMonth;
        _startTime = _lastTime = _observer.TimeOfDay;
        _elapsed = 0;
        _cancelReason = null;
        IsPaused = false;
        _actor.Halt();
        _actor.SetActiveTask(request.TaskId);
        return true;
    }

    public void Update(GameTime? time, long tickCount)
    {
        if (_request is null) return;
        if (_cancelReason is not null) { Finish("cancelled", "WAIT_CANCELLED", _cancelReason); return; }
        if (_blocked() is { } blocked) { Finish("cancelled", "WAIT_INTERRUPTED", blocked); return; }
        int now = _observer.TimeOfDay;
        if (_observer.CurrentSeason != _season || _observer.DayOfMonth != _day || now < _lastTime)
        {
            Finish("cancelled", "WORLD_CHANGED", "The day or clock changed; observe the new world before choosing more work.");
            return;
        }
        if (!IsPaused) _elapsed += Minutes(now) - Minutes(_lastTime);
        _lastTime = now;
        if (!IsPaused && _elapsed >= _request.Parameters.WaitMinutes)
            Finish("succeeded", null, null);
    }

    private static int Minutes(int time) => time / 100 * 60 + time % 100;
    public void RequestCancel(string reason) => _cancelReason = reason;
    public void RequestPause() => IsPaused = true;
    public void Resume() => IsPaused = false;

    private void Finish(string state, string? code, string? message)
    {
        var request = _request!;
        _request = null;
        IsPaused = false;
        if (_actor.ActiveTaskId == request.TaskId) _actor.SetActiveTask(null);
        Completed?.Invoke(new(request.CommandId, request.TaskId, state, state == "succeeded" ? 1 : 0, 0, 0,
            _observer.WorldRevision, new(), new(0, 0, _elapsed),
            Error: code is null ? null : new(code, message!), SkillId: "wait",
            Details: new Dictionary<string, object> { ["requestedGameMinutes"] = request.Parameters.WaitMinutes!.Value,
                ["elapsedGameMinutes"] = _elapsed, ["startTime"] = _startTime, ["endTime"] = _observer.TimeOfDay }));
    }
}
