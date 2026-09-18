using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class NativeActionStateMachineTests
{
    private (
        NativeActionStateMachine machine,
        MechanicsActor actor,
        SimulatedWorldObserver observer,
        TestNativeActionAdapter adapter) CreateHarness(
        float initialStamina = 270f,
        TileCoordinate? initialTile = null)
    {
        var startTile = initialTile ?? new TileCoordinate(10, 10);
        var initialPose = new AuthoritativePose("Farm", startTile, FacingDirection.Down);
        var actor = new MechanicsActor("test-companion", initialStamina, 270f, 40, 40, initialPose);
        var avatar = new CompanionAvatar(actor);
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);
        var adapter = new TestNativeActionAdapter();
        var machine = new NativeActionStateMachine(actor, observer, navigator, adapter, avatar);
        return (machine, actor, observer, adapter);
    }

    private static NativeActionRequest Request(
        NativeActionKind kind,
        params TileCoordinate[] tiles) => new(
        CommandId: "cmd-native-1",
        TaskId: "task-native-1",
        LocationId: "Farm",
        Kind: kind,
        SkillId: kind.ToString(),
        Targets: tiles.Select(t => new NativeActionTarget(t)).ToList(),
        MaxGameMinutes: 60
    );

    private sealed class ThrowingAdapter : StardewAI.Companion.Mod.Adapters.INativeActionAdapter
    {
        public int CallCount;
        public StardewAI.Companion.Mod.Adapters.NativeActionStepResult Execute(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
        {
            CallCount++;
            throw new InvalidOperationException("native effect precondition failed");
        }
    }

    [Fact]
    public void NativeException_EndsOnceWithoutRemainingRunningOrRetryingEffect()
    {
        var actor = new MechanicsActor("throwing", initialPose: new AuthoritativePose("Farm", new TileCoordinate(10, 10), FacingDirection.Down));
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var adapter = new ThrowingAdapter();
        var machine = new NativeActionStateMachine(actor, observer, new SameMapNavigator(observer), adapter);
        int terminals = 0;
        machine.OnCompleted += _ => terminals++;
        Assert.True(machine.Start(Request(NativeActionKind.ClearDebris, new TileCoordinate(10, 11)), out _));
        machine.StepTicks(100);
        Assert.False(machine.IsExecuting);
        Assert.Equal(ExecutionState.Failed, machine.CurrentState);
        Assert.Equal("NATIVE_ACTION_EXCEPTION", machine.FinalResult!.ToTransportPayload().Error!.Code);
        Assert.Equal(1, adapter.CallCount);
        Assert.Equal(1, terminals);
    }

    [Theory]
    [InlineData(NativeActionKind.RefillWateringCan)]
    [InlineData(NativeActionKind.ApplyFertilizer)]
    [InlineData(NativeActionKind.ClearDebris)]
    public void EmptyCanRefill_ReachesAdapterWithoutStartingWaterPourAnimation(NativeActionKind kind)
    {
        var (machine, actor, _, adapter) = CreateHarness();
        actor.WaterLeft = 0;
        Assert.True(machine.Start(Request(kind, new TileCoordinate(10, 11)), out _));
        for (int tick = 0; tick < 100 && machine.IsExecuting; tick++)
        {
            machine.StepTicks(1);
            Assert.False(actor.IsUsingTool);
        }
        Assert.Equal(1, adapter.CallCount);
        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);
    }

    [Fact]
    public void MultipleTargets_AllCompleteAndReportEffects()
    {
        var (machine, _, _, adapter) = CreateHarness();
        NativeActionResult? completed = null;
        machine.OnCompleted += r => completed = r;

        var request = Request(
            NativeActionKind.RefillWateringCan,
            new TileCoordinate(10, 12),
            new TileCoordinate(11, 12),
            new TileCoordinate(10, 11));

        Assert.True(machine.Start(request, out var early));
        Assert.Null(early);
        machine.StepTicks(400);

        Assert.False(machine.IsExecuting);
        Assert.Equal(ExecutionState.Succeeded, machine.CurrentState);
        Assert.NotNull(machine.FinalResult);
        var result = machine.FinalResult!;
        Assert.Equal(3, result.Effects.Count);
        Assert.Empty(result.Skipped);
        Assert.Empty(result.Failed);
        Assert.Equal(3, adapter.CallCount);
        Assert.NotNull(completed);
        Assert.Equal("succeeded", result.ToTransportPayload().TerminalState);
        Assert.Equal(3, result.ToTransportPayload().CompletedCount);
    }

    [Fact]
    public void PreconditionSkipsAreNotFailures()
    {
        var (machine, _, _, adapter) = CreateHarness();
        adapter.SimulatePrecondition = true;
        adapter.PreconditionSkipReason = "already-fertilized";

        Assert.True(machine.Start(Request(NativeActionKind.ApplyFertilizer, new TileCoordinate(10, 12)), out _));
        machine.StepTicks(200);

        var result = machine.FinalResult!;
        Assert.Equal(ExecutionState.Succeeded, result.FinalState);
        Assert.Empty(result.Effects);
        Assert.Single(result.Skipped);
        Assert.Equal("already-fertilized", result.Skipped[0].Reason);
    }

    [Fact]
    public void NativeFailureProducesFailedTerminalState()
    {
        var (machine, _, _, adapter) = CreateHarness();
        adapter.SimulateFailure = true;
        adapter.FailureMessage = "Weed at (10,12) was not removed by the native tool action.";

        Assert.True(machine.Start(Request(NativeActionKind.ClearDebris, new TileCoordinate(10, 12)), out _));
        machine.StepTicks(200);

        var result = machine.FinalResult!;
        Assert.Equal(ExecutionState.Failed, result.FinalState);
        Assert.Single(result.Failed);
        Assert.Equal("Weed at (10,12) was not removed by the native tool action.", result.ErrorMessage);
        Assert.Equal("failed", result.ToTransportPayload().TerminalState);
    }

    [Fact]
    public void PlayerActionRequiredIsPropagated()
    {
        var (machine, _, _, adapter) = CreateHarness();
        adapter.SimulatePrecondition = true;
        adapter.PreconditionSkipReason = "missing-tool:milk pail";
        adapter.SimulatePlayerActionRequired = true;

        Assert.True(machine.Start(Request(NativeActionKind.CollectAnimalProduce, new TileCoordinate(10, 12)), out _));
        machine.StepTicks(200);

        var payload = machine.FinalResult!.ToTransportPayload();
        Assert.True(payload.PlayerActionRequired);
        Assert.Equal(1, payload.SkippedCount);
    }

    [Fact]
    public void FeedAnimalsSelfTargetExecutesWithoutMapMismatch()
    {
        var (machine, _, _, adapter) = CreateHarness();
        var request = new NativeActionRequest(
            CommandId: "cmd-feed",
            TaskId: "task-feed",
            LocationId: "Coop",
            Kind: NativeActionKind.FeedAnimals,
            SkillId: "feed-animals",
            Targets: new[] { new NativeActionTarget(new TileCoordinate(10, 10), "Coop") }
        );

        Assert.True(machine.Start(request, out var early));
        Assert.Null(early);
        machine.StepTicks(100);

        Assert.Equal(ExecutionState.Succeeded, machine.FinalResult!.FinalState);
        Assert.Single(adapter.Calls);
        Assert.Equal("Coop", adapter.Calls[0].Target.TargetId);
    }

    [Fact]
    public void FeedAnimals_CompanionInsideCoopAndPlayerOnFarm_Executes_AndWrongMapPreconditionIsRejected()
    {
        // 1. Companion is inside Coop, player is on Farm: execution proceeds and succeeds without map mismatch
        var (machine1, actor1, observer1, adapter1) = CreateHarness();
        observer1.CurrentLocationName = "Farm";
        actor1.UpdatePose("Coop", new TileCoordinate(6, 4), FacingDirection.Down);
        Assert.Equal("Farm", observer1.CurrentLocationName);
        Assert.Equal("Coop", actor1.LocationName);

        var req1 = new NativeActionRequest(
            CommandId: "cmd-feed-1",
            TaskId: "task-feed-1",
            LocationId: "Coop",
            Kind: NativeActionKind.FeedAnimals,
            SkillId: "feed-animals",
            Targets: new[] { new NativeActionTarget(new TileCoordinate(6, 4), "Coop") }
        );

        Assert.True(machine1.Start(req1, out var early1));
        Assert.Null(early1);
        machine1.StepTicks(100);

        Assert.Equal(ExecutionState.Succeeded, machine1.FinalResult!.FinalState);
        Assert.Single(adapter1.Calls);
        Assert.Equal("Coop", adapter1.Calls[0].Target.TargetId);

        // 2. Companion is on wrong map (e.g. still on Farm): returns wrong-map, state machine terminates in Rejected,
        // and is never reported as Succeeded or completed
        var (machine2, actor2, observer2, adapter2) = CreateHarness();
        observer2.CurrentLocationName = "Farm";
        actor2.UpdatePose("Farm", new TileCoordinate(10, 10), FacingDirection.Down);
        adapter2.SimulatePrecondition = true;
        adapter2.PreconditionSkipReason = "wrong-map";

        var req2 = new NativeActionRequest(
            CommandId: "cmd-feed-2",
            TaskId: "task-feed-2",
            LocationId: "Coop",
            Kind: NativeActionKind.FeedAnimals,
            SkillId: "feed-animals",
            Targets: new[] { new NativeActionTarget(actor2.Tile, "Coop") }
        );

        Assert.True(machine2.Start(req2, out var early2));
        Assert.Null(early2);
        machine2.StepTicks(100);

        var result = machine2.FinalResult!;
        Assert.NotEqual(ExecutionState.Succeeded, result.FinalState);
        Assert.Equal(ExecutionState.Rejected, result.FinalState);
        Assert.Equal("wrong-map", result.ErrorCode);
        Assert.Empty(result.Effects);
        Assert.Single(result.Skipped);
        Assert.Equal("wrong-map", result.Skipped[0].Reason);

        var payload = result.ToTransportPayload();
        Assert.Equal("rejected", payload.TerminalState);
        Assert.Equal(0, payload.CompletedCount);
        Assert.Equal(1, payload.SkippedCount);
        Assert.Equal("wrong-map", payload.Error?.Code);
    }

    [Fact]
    public void HarvestToolResolver_ShearsRequiresRealInventoryTool()
    {
        var actor = new MechanicsActor("shears-regression",
            initialPose: new AuthoritativePose("Farm", new TileCoordinate(10, 10), FacingDirection.Down));
        var resolver = typeof(StardewAI.Companion.Mod.Adapters.NormalNativeActionAdapter)
            .GetMethod("ResolveHarvestTool", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static)!;
        Assert.Null(resolver.Invoke(null, new object?[] { actor, "Shears", null }));
        Assert.True(actor.TryAddItemToInventory(new InventoryItem(
            itemId: "(T)Shears", name: "Shears", stack: 1, slotIndex: 4, isTool: true)));
        Assert.IsType<StardewValley.Tools.Shears>(resolver.Invoke(null, new object?[] { actor, "Shears", null }));
        Assert.Null(resolver.Invoke(null, new object?[] { actor, "Milk Pail", null }));
    }

    [Theory]
    [InlineData("Coop", "Farm", true)]
    [InlineData("Farm", "Coop", false)]
    public void PetAnimalLocationValidationUsesCompanionNotPlayer(
        string companionLocation, string playerLocation, bool expectedAccepted)
    {
        var (machine, actor, observer, adapter) = CreateHarness();
        actor.UpdatePose(companionLocation, new TileCoordinate(4, 6), FacingDirection.Down);
        observer.CurrentLocationName = playerLocation;
        var request = new NativeActionRequest(
            CommandId: "cmd-pet-location",
            TaskId: "task-pet-location",
            LocationId: "Coop",
            Kind: NativeActionKind.PetAnimal,
            SkillId: "pet-animal",
            Targets: new[] { new NativeActionTarget(actor.Tile, "FixtureHen") });

        Assert.Equal(expectedAccepted, machine.Start(request, out var early));
        // This test covers the real validation gate, not simulated pet effects.
        Assert.Equal(0, adapter.CallCount);
        if (expectedAccepted)
        {
            Assert.Null(early);
            Assert.Equal(ExecutionState.Preparing, machine.CurrentState);
        }
        else
        {
            Assert.NotNull(early);
            Assert.Equal(ExecutionState.Rejected, early!.FinalState);
            Assert.Equal("LOCATION_MISMATCH", early.ErrorCode);
            Assert.Contains("'Farm'", early.ErrorMessage);
            Assert.Empty(early.Effects);
        }
    }

    [Fact]
    public void LocationMismatchIsRejectedBeforeExecution()
    {
        var (machine, _, _, adapter) = CreateHarness();
        var request = new NativeActionRequest(
            CommandId: "cmd-x",
            TaskId: "task-x",
            LocationId: "Town",
            Kind: NativeActionKind.PickupItems,
            SkillId: "pickup-items",
            Targets: new[] { new NativeActionTarget(new TileCoordinate(10, 12)) }
        );

        Assert.False(machine.Start(request, out var early));
        Assert.NotNull(early);
        Assert.Equal(ExecutionState.Rejected, early!.FinalState);
        Assert.Equal("LOCATION_MISMATCH", early.ErrorCode);
        Assert.Equal(0, adapter.CallCount);
    }

    [Fact]
    public void EmptyTargetsAreRejected()
    {
        var (machine, _, _, _) = CreateHarness();
        var request = new NativeActionRequest(
            CommandId: "cmd-empty",
            TaskId: "task-empty",
            LocationId: "Farm",
            Kind: NativeActionKind.PickupItems,
            SkillId: "pickup-items",
            Targets: Array.Empty<NativeActionTarget>()
        );

        Assert.False(machine.Start(request, out var early));
        Assert.Equal("INVALID_PARAMETERS", early!.ErrorCode);
    }

    [Fact]
    public void PauseAndResumeHoldAtSafeBoundary()
    {
        var (machine, _, _, _) = CreateHarness();

        Assert.True(machine.Start(Request(NativeActionKind.PickupItems, new TileCoordinate(10, 12)), out _));
        machine.StepTicks(1);
        machine.RequestPause();
        machine.StepTicks(1);
        Assert.True(machine.IsPaused);

        machine.Resume();
        machine.StepTicks(200);
        Assert.Equal(ExecutionState.Succeeded, machine.FinalResult!.FinalState);
    }

    [Fact]
    public void CancelReportsCancelledTerminalState()
    {
        var (machine, _, _, _) = CreateHarness();
        Assert.True(machine.Start(Request(NativeActionKind.PickupItems, new TileCoordinate(10, 12)), out _));
        machine.StepTicks(1);
        machine.RequestCancel("Player cancellation request");
        machine.StepTicks(50);

        var result = machine.FinalResult!;
        Assert.Equal(ExecutionState.Cancelled, result.FinalState);
        Assert.Equal("CANCELLED", result.ErrorCode);
        Assert.Equal("cancelled", result.ToTransportPayload().TerminalState);
    }

    [Fact]
    public void StaminaExhaustionEndsPartiallySucceeded()
    {
        var (machine, actor, _, _) = CreateHarness(initialStamina: 1.0f);
        Assert.True(machine.Start(Request(NativeActionKind.ClearDebris, new TileCoordinate(10, 12), new TileCoordinate(11, 12)), out _));
        machine.StepTicks(200);

        var result = machine.FinalResult!;
        Assert.Equal(ExecutionState.PartiallySucceeded, result.FinalState);
        Assert.Equal("STAMINA_EXHAUSTED", result.ErrorCode);
        Assert.True(actor.Stamina <= 0f);
    }

    [Fact]
    public void GameClockBudgetExceededEndsWithBudgetError()
    {
        var (machine, _, observer, _) = CreateHarness();
        Assert.True(machine.Start(Request(NativeActionKind.PickupItems, new TileCoordinate(10, 12)), out _));

        // Walk the game clock past the request's minute budget; the machine must
        // terminate with an explicit BUDGET_EXCEEDED code instead of continuing.
        observer.TimeOfDay = 900;
        machine.Update(null, 1);

        var result = machine.FinalResult!;
        Assert.Equal(ExecutionState.Failed, result.FinalState);
        Assert.Equal("BUDGET_EXCEEDED", result.ErrorCode);
    }

    // ------------------------------------------------------------------
    // Structured progress (Q5) and tool-surface contracts (Q1/Q4)
    // ------------------------------------------------------------------

    [Fact]
    public void ProgressReportsRealPhasesAndTargetTotal()
    {
        var (machine, _, _, _) = CreateHarness();
        var phases = new List<NativeActionProgress>();
        machine.OnProgress += p => phases.Add(p);

        Assert.True(machine.Start(Request(
            NativeActionKind.RefillWateringCan,
            new TileCoordinate(10, 12),
            new TileCoordinate(11, 12)), out _));
        machine.StepTicks(400);

        Assert.Contains(phases, p => p.Phase == "started" && p.Total == 2);
        Assert.Contains(phases, p => p.Phase == "navigating");
        Assert.Contains(phases, p => p.Phase == "verifying");
        var completed = phases.Last();
        Assert.Equal("completed", completed.Phase);
        Assert.Equal(2, completed.Completed);
        Assert.Equal(2, completed.Total);
        Assert.All(phases, p => Assert.Equal(NativeActionKind.RefillWateringCan.ToString(), p.Action));

        // The terminal payload carries the same structured progress for the bridge.
        var details = machine.FinalResult!.ToTransportPayload().Details!;
        Assert.Equal("completed", details["phase"]);
        Assert.Equal(2, details["completedCount"]);
        Assert.Equal(2, details["total"]);
    }

    [Fact]
    public void ProgressWaitingPhaseCarriesNativeSkipReason()
    {
        var (machine, _, _, adapter) = CreateHarness();
        adapter.SimulatePrecondition = true;
        adapter.PreconditionSkipReason = "missing-tool:Pickaxe";
        adapter.SimulatePlayerActionRequired = true;

        var phases = new List<NativeActionProgress>();
        machine.OnProgress += p => phases.Add(p);

        Assert.True(machine.Start(Request(NativeActionKind.ClearDebris, new TileCoordinate(10, 12)), out _));
        machine.StepTicks(200);

        var waiting = phases.FirstOrDefault(p => p.Phase == "waiting");
        Assert.NotNull(waiting);
        Assert.Equal("missing-tool:Pickaxe", waiting!.ReasonCode);
        Assert.Equal(0, waiting.Completed);
        Assert.Equal(1, waiting.Total);
    }

    [Fact]
    public void OfflineActorTools_AreOnlyReportedWhenPresent()
    {
        var pose = new AuthoritativePose("Farm", new TileCoordinate(10, 10), FacingDirection.Down);
        var actor = new MechanicsActor("tool-companion", initialPose: pose);

        // Nothing is granted automatically: no axe/pickaxe/milk pail/shears.
        Assert.Null(actor.FindTool<StardewValley.Tools.Axe>());
        Assert.Null(actor.FindTool<StardewValley.Tools.Pickaxe>());
        Assert.Null(actor.FindTool<StardewValley.Tools.MilkPail>());
        Assert.Null(actor.FindTool<StardewValley.Tools.Shears>());

        // The player providing the tool makes it resolvable and persisted.
        Assert.True(actor.TryAddItemToInventory(new InventoryItem(
            itemId: "(T)Pickaxe", name: "Pickaxe", stack: 1, slotIndex: 3, isTool: true)));
        Assert.True(actor.TryAddItemToInventory(new InventoryItem(
            itemId: "(T)MilkPail", name: "Milk Pail", stack: 1, slotIndex: 4, isTool: true)));

        Assert.NotNull(actor.FindTool<StardewValley.Tools.Pickaxe>());
        Assert.NotNull(actor.FindTool<StardewValley.Tools.MilkPail>());
        Assert.Null(actor.FindTool<StardewValley.Tools.Shears>());

        var names = actor.GetToolNames();
        Assert.Contains("Pickaxe", names);
        Assert.Contains("MilkPail", names);
        Assert.DoesNotContain("Shears", names);

        var state = actor.CapturePersistentState();
        Assert.Contains(state.Inventory, i => i.ItemId.Contains("Pickaxe") && i.IsTool);
        Assert.Contains(state.Inventory, i => i.ItemId.Contains("MilkPail") && i.IsTool);
    }

    [Theory]
    [InlineData("Milk Pail", true)]
    [InlineData("MilkPail", true)]
    [InlineData("Unknown Pail", false)]
    public void HarvestToolResolver_UsesNativeNameAndRequiresInventory(string harvestTool, bool supported)
    {
        var actor = new MechanicsActor("milk-name-regression",
            initialPose: new AuthoritativePose("Farm", new TileCoordinate(10, 10), FacingDirection.Down));
        var resolver = typeof(StardewAI.Companion.Mod.Adapters.NormalNativeActionAdapter)
            .GetMethod("ResolveHarvestTool", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static)!;
        Assert.Null(resolver.Invoke(null, new object?[] { actor, harvestTool, null }));
        Assert.True(actor.TryAddItemToInventory(new InventoryItem(
            itemId: "(T)MilkPail", name: "Milk Pail", stack: 1, slotIndex: 4, isTool: true)));
        var resolved = resolver.Invoke(null, new object?[] { actor, harvestTool, null });
        if (supported)
            Assert.IsType<StardewValley.Tools.MilkPail>(resolved);
        else
            Assert.Null(resolved);
    }
}
