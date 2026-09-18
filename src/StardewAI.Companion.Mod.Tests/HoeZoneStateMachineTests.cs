using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;
using StardewValley.Tools;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class HoeZoneStateMachineTests
{
    private (HoeZoneStateMachine machine, MechanicsActor actor, SimulatedWorldObserver observer, TestHoeAdapter adapter) CreateHarness(
        float initialStamina = 270f,
        TileCoordinate? initialTile = null,
        bool giveHoe = true,
        Action<string, TileCoordinate>? onHoed = null)
    {
        var startTile = initialTile ?? new TileCoordinate(10, 10);
        var initialPose = new AuthoritativePose("Farm", startTile, FacingDirection.Down);
        var actor = new MechanicsActor("test-companion", initialStamina, 270f, 40, 40, initialPose);
        if (giveHoe)
        {
            actor.Hoe = new Hoe();
        }
        var avatar = new CompanionAvatar(actor);
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);
        var adapter = new TestHoeAdapter(observer, onHoed);

        var machine = new HoeZoneStateMachine(actor, observer, navigator, adapter, avatar);
        return (machine, actor, observer, adapter);
    }

    [Fact]
    public void FullZoneHoeing_SuccessAndVerified()
    {
        var (machine, actor, observer, _) = CreateHarness();

        var t1 = new TileCoordinate(10, 12);
        var t2 = new TileCoordinate(11, 12);
        var t3 = new TileCoordinate(12, 12);

        // Ground is untilled by default
        var request = new HoeZoneRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            LocationId: "Farm",
            TargetTiles: new[] { t1, t2, t3 },
            MaxStamina: 50f
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.True(started);
        Assert.Null(earlyResult);

        machine.StepTicks(200);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.Succeeded, result.FinalState);
        Assert.Equal(3, result.HoedTiles.Count);
        Assert.Empty(result.SkippedTiles);
        Assert.Empty(result.FailedTiles);
        Assert.True(result.StaminaUsed > 0);

        // Observer state verification: all tiles must be tilled dirt
        Assert.True(observer.GetDirtState("Farm", t1).IsTilled);
        Assert.True(observer.GetDirtState("Farm", t2).IsTilled);
        Assert.True(observer.GetDirtState("Farm", t3).IsTilled);
    }

    [Fact]
    public void TargetFiltering_SkipsAlreadyTilledAndCropTiles()
    {
        var (machine, _, observer, _) = CreateHarness();

        var t1 = new TileCoordinate(10, 12); // already tilled
        var t2 = new TileCoordinate(11, 12); // has crop
        var t3 = new TileCoordinate(12, 12); // clear untilled ground

        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetDirt(t2, TileDirtState.DryDirt(hasCrop: true, cropId: "24")); // Parsnip crop

        var request = new HoeZoneRequest(
            CommandId: "cmd-2",
            TaskId: "task-2",
            LocationId: "Farm",
            TargetTiles: new[] { t1, t2, t3 },
            MaxStamina: 50f
        );

        bool started = machine.Start(request, out _);
        Assert.True(started);

        machine.StepTicks(200);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.Succeeded, result.FinalState);
        Assert.Single(result.HoedTiles);
        Assert.Equal(t3, result.HoedTiles[0].Tile);

        Assert.Equal(2, result.SkippedTiles.Count);
        Assert.Contains(result.SkippedTiles, s => s.Tile == t1 && s.Reason == "already-tilled");
        Assert.Contains(result.SkippedTiles, s => s.Tile == t2 && s.Reason == "has-crop");

        // Existing crop MUST be preserved untouched!
        var cropDirt = observer.GetDirtState("Farm", t2);
        Assert.True(cropDirt.HasCrop);
        Assert.Equal("24", cropDirt.CropId);
    }

    [Fact]
    public void StaminaExhaustion_SafeTerminalState()
    {
        // Give only 1.0 stamina, but hoe costs 1.8. Tile 1 hoes and exhausts companion. Second tile will be skipped.
        var (machine, _, _, adapter) = CreateHarness(initialStamina: 1.0f);
        adapter.StaminaCostPerTile = 1.8f;

        var t1 = new TileCoordinate(10, 12);
        var t2 = new TileCoordinate(11, 12);

        var request = new HoeZoneRequest(
            CommandId: "cmd-3",
            TaskId: "task-3",
            LocationId: "Farm",
            TargetTiles: new[] { t1, t2 },
            MaxStamina: 50f
        );

        bool started = machine.Start(request, out _);
        Assert.True(started);

        machine.StepTicks(200);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.PartiallySucceeded, result.FinalState);
        Assert.Single(result.HoedTiles);
        Assert.Single(result.SkippedTiles);
        Assert.Equal("stamina-exhausted", result.SkippedTiles[0].Reason);
    }

    [Fact]
    public void MissingHoeTool_RejectsExecution()
    {
        var (machine, _, _, _) = CreateHarness(giveHoe: false);

        var t1 = new TileCoordinate(10, 12);
        var request = new HoeZoneRequest(
            CommandId: "cmd-4",
            TaskId: "task-4",
            LocationId: "Farm",
            TargetTiles: new[] { t1 }
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.False(started);
        Assert.NotNull(earlyResult);
        Assert.Equal(ExecutionState.Rejected, earlyResult.FinalState);
        Assert.Equal("MISSING_TOOL", earlyResult.ErrorCode);
    }

    [Fact]
    public void Cancellation_ReachesSafePointTerminalState()
    {
        var (machine, _, _, _) = CreateHarness();

        var t1 = new TileCoordinate(10, 12);
        var t2 = new TileCoordinate(11, 12);

        var request = new HoeZoneRequest(
            CommandId: "cmd-5",
            TaskId: "task-5",
            LocationId: "Farm",
            TargetTiles: new[] { t1, t2 }
        );

        bool started = machine.Start(request, out _);
        Assert.True(started);

        // Step 1 tick, then request cancel
        machine.StepTicks(1);
        machine.RequestCancel("Player cancellation request");
        machine.StepTicks(50);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.Cancelled, result.FinalState);
    }

    [Fact]
    public void PauseAndResume_CompletesTask()
    {
        var (machine, _, observer, _) = CreateHarness();

        var t1 = new TileCoordinate(10, 12);
        var request = new HoeZoneRequest(
            CommandId: "cmd-6",
            TaskId: "task-6",
            LocationId: "Farm",
            TargetTiles: new[] { t1 }
        );

        bool started = machine.Start(request, out _);
        Assert.True(started);

        machine.StepTicks(2);
        machine.RequestPause();
        machine.StepTicks(1);
        Assert.True(machine.IsPaused);

        machine.StepTicks(50);
        // While paused, no hoe operation should have completed
        Assert.False(observer.GetDirtState("Farm", t1).IsTilled);

        machine.Resume();
        Assert.False(machine.IsPaused);

        machine.StepTicks(150);
        Assert.NotNull(machine.FinalResult);
        Assert.Equal(ExecutionState.Succeeded, machine.FinalResult.FinalState);
        Assert.True(observer.GetDirtState("Farm", t1).IsTilled);
    }
}
