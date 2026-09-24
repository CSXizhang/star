using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class WaterZoneStateMachineTests
{
    private (WaterZoneStateMachine machine, MechanicsActor actor, SimulatedWorldObserver observer, TestWateringCanAdapter adapter) CreateHarness(
        float initialStamina = 270f,
        int initialWater = 40,
        TileCoordinate? initialTile = null,
        Action<string, TileCoordinate>? onWatered = null)
    {
        var startTile = initialTile ?? new TileCoordinate(10, 10);
        var initialPose = new AuthoritativePose("Farm", startTile, FacingDirection.Down);
        var actor = new MechanicsActor("test-companion", initialStamina, 270f, initialWater, 40, initialPose);
        var avatar = new CompanionAvatar(actor);
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);
        var adapter = new TestWateringCanAdapter(observer, onWatered);

        var machine = new WaterZoneStateMachine(actor, observer, navigator, adapter, avatar);
        return (machine, actor, observer, adapter);
    }

    [Fact]
    public void FullZoneWatering_SuccessAndVerified()
    {
        var (machine, actor, observer, _) = CreateHarness();

        var t1 = new TileCoordinate(10, 12);
        var t2 = new TileCoordinate(11, 12);
        var t3 = new TileCoordinate(12, 12);

        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetDirt(t2, TileDirtState.DryDirt());
        observer.SetDirt(t3, TileDirtState.DryDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            LocationId: "Farm",
            TargetTiles: new[] { t1, t2, t3 },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.True(started);
        Assert.Null(earlyResult);

        // Advance tick-driven state machine
        machine.StepTicks(200);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.Succeeded, result.FinalState);
        Assert.Equal(3, result.WateredTiles.Count);
        Assert.Empty(result.SkippedTiles);
        Assert.Empty(result.FailedTiles);
        Assert.Equal(6.0f, result.StaminaUsed);
        Assert.Equal(3, result.WaterUsed);

        Assert.Equal(264.0f, actor.Stamina);
        Assert.Equal(37, actor.WaterLeft);

        // Verification via observer
        Assert.True(observer.GetDirtState("Farm", t1).IsWatered);
        Assert.True(observer.GetDirtState("Farm", t2).IsWatered);
        Assert.True(observer.GetDirtState("Farm", t3).IsWatered);
    }

    [Fact]
    public void TargetFiltering_SkipsAlreadyWateredTiles()
    {
        var (machine, actor, observer, _) = CreateHarness();

        var t1 = new TileCoordinate(10, 12); // already watered
        var t2 = new TileCoordinate(11, 12); // dry

        observer.SetDirt(t1, TileDirtState.WateredDirt());
        observer.SetDirt(t2, TileDirtState.DryDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-2",
            TaskId: "task-2",
            LocationId: "Farm",
            TargetTiles: new[] { t1, t2 },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        bool started = machine.Start(request, out _);
        Assert.True(started);

        machine.StepTicks(100);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.Succeeded, result.FinalState);
        Assert.Single(result.WateredTiles);
        Assert.Equal(t2, result.WateredTiles[0]);
        Assert.Single(result.SkippedTiles);
        Assert.Equal(t1, result.SkippedTiles[0].Tile);
        Assert.Equal("AlreadyWatered", result.SkippedTiles[0].Reason);
        Assert.Equal(2.0f, result.StaminaUsed);
        Assert.Equal(1, result.WaterUsed);
    }

    [Fact]
    public void EmptyCan_PreconditionRejectsImmediately()
    {
        var (machine, actor, observer, _) = CreateHarness(initialWater: 0);

        var t1 = new TileCoordinate(10, 12);
        observer.SetDirt(t1, TileDirtState.DryDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-3",
            TaskId: "task-3",
            LocationId: "Farm",
            TargetTiles: new[] { t1 },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        bool started = machine.Start(request, out var earlyResult);

        Assert.False(started);
        Assert.NotNull(earlyResult);
        Assert.Equal(ExecutionState.Rejected, earlyResult.FinalState);
        Assert.Equal("CAN_EMPTY", earlyResult.ErrorCode);
        Assert.Empty(earlyResult.WateredTiles);
        Assert.True(earlyResult.RetryRecommended);
    }

    [Fact]
    public void SafeCancellation_DuringNavigation_HaltsAndReportsCancelled()
    {
        var (machine, actor, observer, _) = CreateHarness();

        var t1 = new TileCoordinate(10, 15);
        observer.SetDirt(t1, TileDirtState.DryDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-4",
            TaskId: "task-4",
            LocationId: "Farm",
            TargetTiles: new[] { t1 },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        machine.Start(request, out _);
        machine.StepTicks(2); // Actor is moving
        Assert.True(machine.IsExecuting);

        // Cancel mid-navigation
        machine.RequestCancel("Player commanded stop");
        machine.StepTicks(1);

        Assert.False(machine.IsExecuting);
        Assert.Equal(ExecutionState.Cancelled, machine.FinalResult!.FinalState);
        Assert.Empty(machine.FinalResult.WateredTiles);
    }

    [Fact]
    public void SafeCancellation_MidExecution_ReportsPartiallySucceeded()
    {
        var t1 = new TileCoordinate(10, 12);
        var t2 = new TileCoordinate(11, 12);

        WaterZoneStateMachine? machineRef = null;
        var (machine, actor, observer, _) = CreateHarness(onWatered: (loc, tile) =>
        {
            if (tile == t1)
            {
                machineRef!.RequestCancel("Cancel requested after first tile");
            }
        });
        machineRef = machine;

        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetDirt(t2, TileDirtState.DryDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-4b",
            TaskId: "task-4b",
            LocationId: "Farm",
            TargetTiles: new[] { t1, t2 },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        machine.Start(request, out _);
        machine.StepTicks(150);

        Assert.False(machine.IsExecuting);
        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.PartiallySucceeded, result.FinalState);
        Assert.Single(result.WateredTiles);
        Assert.Equal(t1, result.WateredTiles[0]);
        Assert.Equal(2.0f, result.StaminaUsed);
        Assert.Equal(1, result.WaterUsed);
    }

    [Fact]
    public void SafePause_And_Resume()
    {
        var (machine, actor, observer, _) = CreateHarness();

        var t1 = new TileCoordinate(10, 12);
        observer.SetDirt(t1, TileDirtState.DryDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-pause",
            TaskId: "task-pause",
            LocationId: "Farm",
            TargetTiles: new[] { t1 },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        machine.Start(request, out _);
        machine.StepTicks(2);

        // Pause
        machine.RequestPause();
        machine.StepTicks(1);

        Assert.True(machine.IsPaused);

        // Step while paused: does not advance
        machine.StepTicks(5);
        Assert.True(machine.IsPaused);
        Assert.Empty(machine.FinalResult?.WateredTiles ?? new List<TileCoordinate>());

        // Resume
        machine.Resume();
        Assert.False(machine.IsPaused);

        // Complete execution
        machine.StepTicks(100);
        Assert.Equal(ExecutionState.Succeeded, machine.FinalResult!.FinalState);
        Assert.Single(machine.FinalResult.WateredTiles);
    }

    [Fact]
    public void WaterBudgetExhausted_HaltsSafelyWithPartialSuccess()
    {
        var (machine, actor, observer, _) = CreateHarness();

        var t1 = new TileCoordinate(10, 12);
        var t2 = new TileCoordinate(11, 12);

        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetDirt(t2, TileDirtState.DryDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-5",
            TaskId: "task-5",
            LocationId: "Farm",
            TargetTiles: new[] { t1, t2 },
            MaxStamina: 50f,
            MaxWater: 1, // only budget for 1 tile!
            MaxGameMinutes: 60
        );

        machine.Start(request, out _);
        machine.StepTicks(150);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.PartiallySucceeded, result.FinalState);
        Assert.Single(result.WateredTiles);
        Assert.Equal(1, result.WaterUsed);
        Assert.Equal(2.0f, result.StaminaUsed);
    }

    [Fact]
    public void StaminaBudgetExhausted_HaltsSafelyWithPartialSuccess()
    {
        var (machine, actor, observer, _) = CreateHarness();

        var t1 = new TileCoordinate(10, 12);
        var t2 = new TileCoordinate(11, 12);

        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetDirt(t2, TileDirtState.DryDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-6",
            TaskId: "task-6",
            LocationId: "Farm",
            TargetTiles: new[] { t1, t2 },
            MaxStamina: 2.0f, // only enough for 1 tile
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        machine.Start(request, out _);
        machine.StepTicks(150);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.PartiallySucceeded, result.FinalState);
        Assert.Single(result.WateredTiles);
        Assert.Equal(2.0f, result.StaminaUsed);
    }

    [Fact]
    public void UnreachableTile_RecordedAsFailed()
    {
        var (machine, actor, observer, _) = CreateHarness();

        var target = new TileCoordinate(20, 20);
        observer.SetDirt(target, TileDirtState.DryDirt());

        // Completely enclose target with obstacles
        observer.SetPassable(new TileCoordinate(20, 19), false);
        observer.SetPassable(new TileCoordinate(21, 20), false);
        observer.SetPassable(new TileCoordinate(20, 21), false);
        observer.SetPassable(new TileCoordinate(19, 20), false);

        var request = new WaterZoneRequest(
            CommandId: "cmd-7",
            TaskId: "task-7",
            LocationId: "Farm",
            TargetTiles: new[] { target },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        machine.Start(request, out _);
        machine.StepTicks(10);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.Failed, result.FinalState);
        Assert.Single(result.FailedTiles);
        Assert.Contains("no passable adjacent standing positions", result.FailedTiles[0].Reason);
    }

    [Fact]
    public void PausedCancel_ServicesCancelFromPausedState()
    {
        var (machine, actor, observer, _) = CreateHarness();
        var target = new TileCoordinate(10, 12);
        observer.SetDirt(target, TileDirtState.DryDirt());

        var request = new WaterZoneRequest("cmd-pc", "task-pc", "Farm", new[] { target }, 50f, 20, 60);
        machine.Start(request, out _);

        // Pause during navigation
        machine.RequestPause();
        machine.StepTicks(1);
        Assert.True(machine.IsPaused);

        // Request cancel while paused
        machine.RequestCancel("Cancel while paused");
        machine.StepTicks(1);

        Assert.False(machine.IsExecuting);
        Assert.Equal(ExecutionState.Cancelled, machine.FinalResult!.FinalState);
        Assert.Equal("Cancel while paused", machine.FinalResult.ErrorMessage);
    }

    [Fact]
    public void ZeroFirstActionBudget_RejectsImmediately()
    {
        var (machine, _, observer, _) = CreateHarness();
        var target = new TileCoordinate(10, 12);
        observer.SetDirt(target, TileDirtState.DryDirt());

        // Zero stamina budget
        var zeroStaminaReq = new WaterZoneRequest("cmd-b1", "task-b1", "Farm", new[] { target }, 0f, 20, 60);
        bool started1 = machine.Start(zeroStaminaReq, out var result1);
        Assert.False(started1);
        Assert.NotNull(result1);
        Assert.Equal(ExecutionState.Rejected, result1.FinalState);
        Assert.Equal("BUDGET_EXHAUSTED", result1.ErrorCode);

        // Zero water budget
        var zeroWaterReq = new WaterZoneRequest("cmd-b2", "task-b2", "Farm", new[] { target }, 50f, 0, 60);
        bool started2 = machine.Start(zeroWaterReq, out var result2);
        Assert.False(started2);
        Assert.NotNull(result2);
        Assert.Equal(ExecutionState.Rejected, result2.FinalState);
        Assert.Equal("BUDGET_EXHAUSTED", result2.ErrorCode);
    }

    [Fact]
    public void DynamicObstacleBeforeEnteringTile_RefusesEntryAndReplans()
    {
        var (machine, actor, observer, _) = CreateHarness();
        var target = new TileCoordinate(10, 13);
        observer.SetDirt(target, TileDirtState.DryDirt());

        var request = new WaterZoneRequest("cmd-obs", "task-obs", "Farm", new[] { target }, 50f, 20, 60);
        machine.Start(request, out _);

        // Step 5 ticks towards (10, 11)
        machine.StepTicks(5);
        Assert.Equal(ExecutionState.Navigating, machine.CurrentState);

        // Dynamic obstacle appears on the path tile (10, 11)
        observer.SetPassable(new TileCoordinate(10, 11), false);

        // State machine must detect obstacle BEFORE entering, refuse entry, and replan around via (9, 11) or similar
        machine.StepTicks(10);

        // Actor must NOT have snapped into the impassable tile (10, 11)
        Assert.NotEqual(new TileCoordinate(10, 11), actor.Tile);
    }

    [Fact]
    public void PhaseSafeResume_DoesNotRepeatProcessedTargetOrDoubleCost()
    {
        int t1WaterCount = 0;
        int t2WaterCount = 0;
        var t1 = new TileCoordinate(10, 12);
        var t2 = new TileCoordinate(10, 13);

        var (machine, actor, observer, _) = CreateHarness(onWatered: (loc, tile) =>
        {
            if (tile == t1) t1WaterCount++;
            if (tile == t2) t2WaterCount++;
        });

        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetDirt(t2, TileDirtState.DryDirt());

        var request = new WaterZoneRequest("cmd-psr", "task-psr", "Farm", new[] { t1, t2 }, 50f, 20, 60);
        machine.Start(request, out _);

        // Step until t1 is processed and state pauses or we request pause
        while (t1WaterCount == 0 && machine.IsExecuting)
        {
            machine.StepTicks(1);
        }

        // Pause after first target
        machine.RequestPause();
        while (!machine.IsPaused && machine.IsExecuting)
        {
            machine.StepTicks(1);
        }

        Assert.True(machine.IsPaused);
        Assert.Equal(1, t1WaterCount);

        // Resume execution
        machine.Resume();
        machine.StepTicks(100);

        // Verify: target 1 was watered exactly once (no double cost), and target 2 was watered
        Assert.Equal(1, t1WaterCount);
        Assert.Equal(1, t2WaterCount);
        Assert.Equal(ExecutionState.Succeeded, machine.FinalResult!.FinalState);
    }

    [Fact]
    public void MeasuredClockDelta_ExceedsBudget_HaltsWithBudgetExhausted()
    {
        var (machine, actor, observer, _) = CreateHarness();
        var target = new TileCoordinate(10, 12);
        observer.SetDirt(target, TileDirtState.DryDirt());
        observer.TimeOfDay = 600; // 6:00 AM

        var request = new WaterZoneRequest("cmd-time", "task-time", "Farm", new[] { target }, 50f, 20, MaxGameMinutes: 10);
        machine.Start(request, out _);

        // Advance game clock by 20 in-game minutes (600 -> 620)
        observer.TimeOfDay = 620;
        machine.StepTicks(1);

        Assert.False(machine.IsExecuting);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal("BUDGET_EXHAUSTED", machine.FinalResult.ErrorCode);
    }

    private class FaultyToolActor : MechanicsActor
    {
        public bool ThrowOnBeginUsing { get; set; }
        public bool SkipEffectPoint { get; set; }

        public FaultyToolActor(AuthoritativePose initialPose)
            : base("faulty-actor", initialPose: initialPose)
        {
        }

        public override void BeginUsingTool()
        {
            if (ThrowOnBeginUsing)
                throw new InvalidOperationException("Simulated native tool invocation error.");
            base.BeginUsingTool();
        }

        public override ToolAnimationPhase UpdateToolAnimation(Microsoft.Xna.Framework.GameTime? time, long tickCount)
        {
            if (SkipEffectPoint)
            {
                // Advance straight to Completed without ever returning EffectPoint
                return ToolAnimationPhase.Completed;
            }
            return base.UpdateToolAnimation(time, tickCount);
        }
    }

    [Fact]
    public void ToolAnimationCompleted_WithoutEffect_FailsClosedAndDoesNotSucceed()
    {
        var startTile = new TileCoordinate(10, 10);
        var initialPose = new AuthoritativePose("Farm", startTile, FacingDirection.Down);
        var faultyActor = new FaultyToolActor(initialPose) { SkipEffectPoint = true };
        var avatar = new CompanionAvatar(faultyActor);
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);
        var adapter = new TestWateringCanAdapter(observer);

        var machine = new WaterZoneStateMachine(faultyActor, observer, navigator, adapter, avatar);

        var target = new TileCoordinate(10, 11);
        observer.SetDirt(target, TileDirtState.DryDirt());

        var request = new WaterZoneRequest("cmd-fail-effect", "task-fail-effect", "Farm", new[] { target }, 50f, 20, 60);
        machine.Start(request, out _);

        // Advance ticks through navigation and tool animation completion
        machine.StepTicks(100);

        Assert.False(machine.IsExecuting);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal(ExecutionState.Failed, machine.FinalResult.FinalState);
        Assert.Single(machine.FinalResult.FailedTiles);
        Assert.Equal(target, machine.FinalResult.FailedTiles[0].Tile);
        Assert.Contains("without executing watering effect", machine.FinalResult.FailedTiles[0].Reason);
        Assert.False(observer.GetDirtState("Farm", target).IsWatered);
    }

    [Fact]
    public void ActorException_DuringExecution_FailsClosedWithExecutionException()
    {
        var startTile = new TileCoordinate(10, 10);
        var initialPose = new AuthoritativePose("Farm", startTile, FacingDirection.Down);
        var faultyActor = new FaultyToolActor(initialPose) { ThrowOnBeginUsing = true };
        var avatar = new CompanionAvatar(faultyActor);
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);
        var adapter = new TestWateringCanAdapter(observer);

        var machine = new WaterZoneStateMachine(faultyActor, observer, navigator, adapter, avatar);

        var target = new TileCoordinate(10, 11);
        observer.SetDirt(target, TileDirtState.DryDirt());

        var request = new WaterZoneRequest("cmd-exc", "task-exc", "Farm", new[] { target }, 50f, 20, 60);
        machine.Start(request, out _);

        // Advance ticks: arrives at tile, faces target, calls BeginUsingTool which throws
        machine.StepTicks(100);

        Assert.False(machine.IsExecuting);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal(ExecutionState.Failed, machine.FinalResult.FinalState);
        Assert.Equal("EXECUTION_EXCEPTION", machine.FinalResult.ErrorCode);
        Assert.Single(machine.FinalResult.FailedTiles);
        Assert.Contains("Simulated native tool invocation error", machine.FinalResult.FailedTiles[0].Reason);
    }

    [Fact]
    public void FullyFailedExecution_SurfacesTopLevelErrorMessageInResultAndPayload()
    {
        var startTile = new TileCoordinate(10, 10);
        var initialPose = new AuthoritativePose("Farm", startTile, FacingDirection.Down);
        var faultyActor = new FaultyToolActor(initialPose) { SkipEffectPoint = true };
        var avatar = new CompanionAvatar(faultyActor);
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);
        var adapter = new TestWateringCanAdapter(observer);

        var machine = new WaterZoneStateMachine(faultyActor, observer, navigator, adapter, avatar);

        var target = new TileCoordinate(10, 11);
        observer.SetDirt(target, TileDirtState.DryDirt());

        var request = new WaterZoneRequest("cmd-err-surface", "task-err-surface", "Farm", new[] { target }, 50f, 20, 60);
        machine.Start(request, out _);
        machine.StepTicks(100);

        Assert.False(machine.IsExecuting);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal(ExecutionState.Failed, machine.FinalResult.FinalState);
        Assert.NotNull(machine.FinalResult.ErrorMessage);
        Assert.Contains("without executing watering effect", machine.FinalResult.ErrorMessage);
        Assert.Equal("EXECUTION_FAILED", machine.FinalResult.ErrorCode);

        var payload = machine.FinalResult.ToTransportPayload();
        Assert.Equal("failed", payload.TerminalState);
        Assert.NotNull(payload.Error);
        Assert.Equal("EXECUTION_FAILED", payload.Error.Code);
        Assert.Contains("without executing watering effect", payload.Error.Message);
    }

    [Fact]
    public void AllTargetsAlreadyWatered_SkippedWithZeroResourceConsumption()
    {
        var (machine, actor, observer, _) = CreateHarness();

        var t1 = new TileCoordinate(10, 12);
        var t2 = new TileCoordinate(11, 12);

        observer.SetDirt(t1, TileDirtState.WateredDirt());
        observer.SetDirt(t2, TileDirtState.WateredDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-all-watered",
            TaskId: "task-all-watered",
            LocationId: "Farm",
            TargetTiles: new[] { t1, t2 },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        float initialStamina = actor.Stamina;
        int initialWater = actor.WaterLeft;

        bool started = machine.Start(request, out var earlyResult);

        Assert.True(started);
        Assert.NotNull(earlyResult);
        Assert.Equal(ExecutionState.Succeeded, earlyResult.FinalState);
        Assert.Empty(earlyResult.WateredTiles);
        Assert.Equal(2, earlyResult.SkippedTiles.Count);
        Assert.All(earlyResult.SkippedTiles, s => Assert.Equal("AlreadyWatered", s.Reason));
        Assert.Equal(0f, earlyResult.StaminaUsed);
        Assert.Equal(0, earlyResult.WaterUsed);
        Assert.Equal(initialStamina, actor.Stamina);
        Assert.Equal(initialWater, actor.WaterLeft);
        Assert.False(machine.IsExecuting);
    }

    [Fact]
    public void LowStamina_PreconditionRejectsImmediatelyWithInsufficientStamina()
    {
        // Initial stamina 1.0f is less than NormalWateringCanAdapter.BaseStaminaCost (2.0f)
        var (machine, actor, observer, _) = CreateHarness(initialStamina: 1.0f);

        var t1 = new TileCoordinate(10, 12);
        observer.SetDirt(t1, TileDirtState.DryDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-low-stamina",
            TaskId: "task-low-stamina",
            LocationId: "Farm",
            TargetTiles: new[] { t1 },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        bool started = machine.Start(request, out var earlyResult);

        Assert.False(started);
        Assert.NotNull(earlyResult);
        Assert.Equal(ExecutionState.Rejected, earlyResult.FinalState);
        Assert.Equal("INSUFFICIENT_STAMINA", earlyResult.ErrorCode);
        Assert.True(earlyResult.RetryRecommended);
        Assert.Empty(earlyResult.WateredTiles);
        Assert.False(machine.IsExecuting);
    }

    [Fact]
    public void UnreachableTile_BoundedFailure_AllowsRemainingTilesToComplete()
    {
        var (machine, actor, observer, _) = CreateHarness();

        var t1 = new TileCoordinate(20, 20); // enclosed / unreachable
        var t2 = new TileCoordinate(10, 12); // open / reachable

        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetDirt(t2, TileDirtState.DryDirt());

        // Completely enclose t1
        observer.SetPassable(new TileCoordinate(20, 19), false);
        observer.SetPassable(new TileCoordinate(21, 20), false);
        observer.SetPassable(new TileCoordinate(20, 21), false);
        observer.SetPassable(new TileCoordinate(19, 20), false);

        var request = new WaterZoneRequest(
            CommandId: "cmd-bounded-fail",
            TaskId: "task-bounded-fail",
            LocationId: "Farm",
            TargetTiles: new[] { t1, t2 },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        bool started = machine.Start(request, out _);
        Assert.True(started);

        machine.StepTicks(150);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.PartiallySucceeded, result.FinalState);
        Assert.Single(result.WateredTiles);
        Assert.Equal(t2, result.WateredTiles[0]);
        Assert.Single(result.FailedTiles);
        Assert.Equal(t1, result.FailedTiles[0].Tile);
        Assert.Contains("no passable adjacent standing positions", result.FailedTiles[0].Reason);
        Assert.Equal(2.0f, result.StaminaUsed);
        Assert.Equal(1, result.WaterUsed);
        Assert.False(machine.IsExecuting);
    }

    [Fact]
    public void Cancellation_DuringWateringAfterEffectExecuted_CountsWateredTileAndProducesSingleTerminalResult()
    {
        var t1 = new TileCoordinate(10, 12);
        var t2 = new TileCoordinate(11, 12);

        WaterZoneStateMachine? machineRef = null;
        var (machine, actor, observer, _) = CreateHarness(onWatered: (loc, tile) =>
        {
            if (tile == t1)
            {
                // Cancel requested immediately after watering effect is executed
                machineRef!.RequestCancel("Mid-swing cancel after effect executed");
            }
        });
        machineRef = machine;

        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetDirt(t2, TileDirtState.DryDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-cancel-midswing",
            TaskId: "task-cancel-midswing",
            LocationId: "Farm",
            TargetTiles: new[] { t1, t2 },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        machine.Start(request, out _);

        // Step 150 ticks: will navigate to t1, begin watering, execute effect, receive cancel, resolve in-progress swing
        machine.StepTicks(150);

        Assert.False(machine.IsExecuting);
        var firstResult = machine.FinalResult;
        Assert.NotNull(firstResult);
        Assert.Equal(ExecutionState.PartiallySucceeded, firstResult.FinalState);
        Assert.Single(firstResult.WateredTiles);
        Assert.Equal(t1, firstResult.WateredTiles[0]);
        Assert.Equal(2.0f, firstResult.StaminaUsed);
        Assert.Equal(1, firstResult.WaterUsed);
        Assert.False(actor.IsUsingTool);

        // Additional ticks must NOT emit a second terminal result or change state
        machine.StepTicks(50);
        Assert.False(machine.IsExecuting);
        Assert.Same(firstResult, machine.FinalResult);
        Assert.False(observer.GetDirtState("Farm", t2).IsWatered);
    }

    private class LoopingAnimationActor : MechanicsActor
    {
        private readonly int[] _animSequence;
        private int _seqIndex;
        private int _lastAnimIndex = -1;
        private readonly int _frameCount;

        public LoopingAnimationActor(AuthoritativePose initialPose, int frameCount = 3)
            : base("looping-actor", initialPose: initialPose)
        {
            _frameCount = frameCount;
            // Native sequence: 0 (Windup), 1 (EffectPoint tick 1), 1 (EffectPoint tick 2), 2 (FollowThrough), 0 (Wrap back to 0 -> Completed)
            _animSequence = new[] { 0, 1, 1, 2, 0 };
        }

        public override ToolAnimationPhase UpdateToolAnimation(Microsoft.Xna.Framework.GameTime? time, long tickCount)
        {
            if (!IsUsingTool) return ToolAnimationPhase.None;

            int animIndex = _seqIndex < _animSequence.Length ? _animSequence[_seqIndex++] : 0;
            bool completedOnePass = _frameCount > 0 && _lastAnimIndex >= _frameCount - 1 && animIndex < _lastAnimIndex;
            _lastAnimIndex = animIndex;

            if (completedOnePass)
            {
                return ToolAnimationPhase.Completed;
            }
            else if (animIndex == 0)
            {
                return ToolAnimationPhase.Windup;
            }
            else if (animIndex == 1)
            {
                return ToolAnimationPhase.EffectPoint;
            }
            else
            {
                return ToolAnimationPhase.FollowThrough;
            }
        }
    }

    [Fact]
    public void LoopingAnimation_WrapAroundDetectsCompletion_AndEffectPointExecutesExactlyOnce()
    {
        var startTile = new TileCoordinate(10, 10);
        var initialPose = new AuthoritativePose("Farm", startTile, FacingDirection.Down);
        var loopingActor = new LoopingAnimationActor(initialPose);
        var avatar = new CompanionAvatar(loopingActor);
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);

        int toolActionCount = 0;
        var adapter = new TestWateringCanAdapter(observer, onWatered: (_, _) => toolActionCount++);

        var machine = new WaterZoneStateMachine(loopingActor, observer, navigator, adapter, avatar);

        var target = new TileCoordinate(10, 11);
        observer.SetDirt(target, TileDirtState.DryDirt());

        var request = new WaterZoneRequest("cmd-loop-wrap", "task-loop-wrap", "Farm", new[] { target }, 50f, 20, 60);
        machine.Start(request, out _);

        machine.StepTicks(50);

        Assert.False(machine.IsExecuting);
        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.Succeeded, result.FinalState);
        Assert.Single(result.WateredTiles);
        Assert.Equal(target, result.WateredTiles[0]);
        // EffectPoint occurred for 2 ticks in sequence, but tool action was applied EXACTLY ONCE
        Assert.Equal(1, toolActionCount);
        Assert.Equal(2.0f, result.StaminaUsed);
        Assert.Equal(1, result.WaterUsed);
        Assert.True(observer.GetDirtState("Farm", target).IsWatered);
    }

    [Fact]
    public void DynamicTargetOrdering_PrefersShorterFeasiblePathOverScanOrder()
    {
        // A is closer by Manhattan but its approach is walled off; B has a shorter
        // real feasible path. Ordering must use feasible path length, not scan order.
        var wateredOrder = new List<TileCoordinate>();
        var (machine, actor, observer, _) = CreateHarness(onWatered: (_, tile) => wateredOrder.Add(tile));

        var blockedNearest = new TileCoordinate(10, 13);
        var clearAlternative = new TileCoordinate(11, 12);
        observer.SetDirt(blockedNearest, TileDirtState.DryDirt());
        observer.SetDirt(clearAlternative, TileDirtState.DryDirt());

        // Wall off the direct southern approach to A.
        observer.SetPassable(new TileCoordinate(10, 11), false);
        observer.SetPassable(new TileCoordinate(10, 12), false);

        var request = new WaterZoneRequest(
            CommandId: "cmd-order-cost",
            TaskId: "task-order-cost",
            LocationId: "Farm",
            // Scan order deliberately lists the walled-off tile first.
            TargetTiles: new[] { blockedNearest, clearAlternative },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        machine.Start(request, out _);
        machine.StepTicks(300);

        Assert.Equal(ExecutionState.Succeeded, machine.FinalResult!.FinalState);
        Assert.Equal(2, wateredOrder.Count);
        Assert.Equal(clearAlternative, wateredOrder[0]);
        Assert.DoesNotContain(wateredOrder, t => t == new TileCoordinate(10, 11));
    }

    [Fact]
    public void PlayerBlocksDirectPath_PlanRoutesAroundPlayerAndStillCompletes()
    {
        var (machine, actor, observer, _) = CreateHarness();
        var target = new TileCoordinate(10, 13);
        observer.SetDirt(target, TileDirtState.DryDirt());

        // Player stands directly on the only straight-line tile.
        observer.PlayerTile = new TileCoordinate(10, 11);

        var request = new WaterZoneRequest(
            CommandId: "cmd-player-plan",
            TaskId: "task-player-plan",
            LocationId: "Farm",
            TargetTiles: new[] { target },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        machine.Start(request, out _);
        machine.StepTicks(300);

        Assert.Equal(ExecutionState.Succeeded, machine.FinalResult!.FinalState);
        Assert.True(observer.GetDirtState("Farm", target).IsWatered);
        Assert.NotEqual(new TileCoordinate(10, 11), actor.Tile);
    }

    [Fact]
    public void PlayerStepsIntoPathDuringNavigation_ReplansAndDoesNotReenterBlockedTile()
    {
        var (machine, actor, observer, _) = CreateHarness();
        var target = new TileCoordinate(10, 13);
        observer.SetDirt(target, TileDirtState.DryDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-player-dyn",
            TaskId: "task-player-dyn",
            LocationId: "Farm",
            TargetTiles: new[] { target },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        machine.Start(request, out _);
        machine.StepTicks(1);
        Assert.Equal(ExecutionState.Navigating, machine.CurrentState);

        // Player steps into the tile the companion was about to enter.
        observer.PlayerTile = new TileCoordinate(10, 11);

        machine.StepTicks(300);

        Assert.Equal(ExecutionState.Succeeded, machine.FinalResult!.FinalState);
        Assert.True(observer.GetDirtState("Farm", target).IsWatered);
        Assert.NotEqual(new TileCoordinate(10, 11), actor.Tile);
    }

    [Theory]
    [InlineData("Farm", "FarmHouse", true)]
    [InlineData("FarmHouse", "Farm", false)]
    public void LocationValidationUsesCompanionNotPlayer(string companionLocation, string playerLocation, bool expectedAccepted)
    {
        // After an overnight pass-out the player wakes in the FarmHouse while the
        // companion is still on the Farm; zone validation must follow the companion.
        var (machine, actor, observer, _) = CreateHarness();
        actor.UpdatePose(companionLocation, new TileCoordinate(10, 10), FacingDirection.Down);
        observer.CurrentLocationName = playerLocation;
        observer.SetDirt(new TileCoordinate(10, 12), TileDirtState.DryDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-loc",
            TaskId: "task-loc",
            LocationId: "Farm",
            TargetTiles: new[] { new TileCoordinate(10, 12) },
            MaxStamina: 50f,
            MaxWater: 20,
            MaxGameMinutes: 60
        );

        Assert.Equal(expectedAccepted, machine.Start(request, out var early));
        if (expectedAccepted)
        {
            Assert.Null(early);
        }
        else
        {
            Assert.NotNull(early);
            Assert.Equal(ExecutionState.Rejected, early!.FinalState);
            Assert.Equal("LOCATION_MISMATCH", early.ErrorCode);
        }
    }
}

