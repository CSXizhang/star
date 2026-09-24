using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class PlantZoneStateMachineTests
{
    private (PlantZoneStateMachine machine, MechanicsActor actor, SimulatedWorldObserver observer, TestPlantAdapter adapter) CreateHarness(
        string seedItemId = "472", // Parsnip Seeds
        int initialSeedCount = 10,
        TileCoordinate? initialTile = null,
        Action<string, TileCoordinate, string>? onPlanted = null)
    {
        var startTile = initialTile ?? new TileCoordinate(10, 10);
        var initialPose = new AuthoritativePose("Farm", startTile, FacingDirection.Down);
        var actor = new MechanicsActor("test-companion", 270f, 270f, 40, 40, initialPose);

        if (initialSeedCount > 0)
        {
            actor.TryAddItemToInventory(new InventoryItem(seedItemId, "Parsnip Seeds", initialSeedCount));
        }

        var avatar = new CompanionAvatar(actor);
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);
        var adapter = new TestPlantAdapter(observer, onPlanted);

        var machine = new PlantZoneStateMachine(actor, observer, navigator, adapter, avatar);
        return (machine, actor, observer, adapter);
    }

    [Fact]
    public void FullZonePlanting_SuccessAndVerified()
    {
        var (machine, actor, observer, _) = CreateHarness(seedItemId: "472", initialSeedCount: 5);

        var t1 = new TileCoordinate(10, 12);
        var t2 = new TileCoordinate(11, 12);
        var t3 = new TileCoordinate(12, 12);

        // Precondition: ground must be tilled soil
        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetDirt(t2, TileDirtState.DryDirt());
        observer.SetDirt(t3, TileDirtState.DryDirt());

        var request = new PlantZoneRequest(
            CommandId: "cmd-1",
            TaskId: "task-1",
            LocationId: "Farm",
            SeedItemId: "472",
            TargetTiles: new[] { t1, t2, t3 }
        );

        bool started = machine.Start(request, out var earlyResult);
        Assert.True(started);
        Assert.Null(earlyResult);

        machine.StepTicks(200);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.Succeeded, result.FinalState);
        Assert.Equal(3, result.PlantedTiles.Count);
        Assert.Empty(result.SkippedTiles);
        Assert.Empty(result.FailedTiles);

        // Seeds deducted: started with 5, planted 3 -> 2 remaining
        Assert.Equal(2, actor.GetItemCount("472"));
        Assert.Equal(2, result.RemainingSeedStack);

        // World verification: dirt now has crop
        Assert.True(observer.GetDirtState("Farm", t1).HasCrop);
        Assert.True(observer.GetDirtState("Farm", t2).HasCrop);
        Assert.True(observer.GetDirtState("Farm", t3).HasCrop);
        Assert.Equal("472", observer.GetDirtState("Farm", t1).CropId);
    }

    [Fact]
    public void TargetFiltering_SkipsUntilledAndCropTiles()
    {
        var (machine, actor, observer, _) = CreateHarness(seedItemId: "472", initialSeedCount: 5);

        var t1 = new TileCoordinate(10, 12); // untilled ground
        var t2 = new TileCoordinate(11, 12); // already has crop
        var t3 = new TileCoordinate(12, 12); // tilled empty soil

        observer.SetDirt(t2, TileDirtState.DryDirt(hasCrop: true, cropId: "existing-crop-id"));
        observer.SetDirt(t3, TileDirtState.DryDirt());

        var request = new PlantZoneRequest(
            CommandId: "cmd-2",
            TaskId: "task-2",
            LocationId: "Farm",
            SeedItemId: "472",
            TargetTiles: new[] { t1, t2, t3 }
        );

        bool started = machine.Start(request, out _);
        Assert.True(started);

        machine.StepTicks(200);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.Succeeded, result.FinalState);
        Assert.Single(result.PlantedTiles);
        Assert.Equal(t3, result.PlantedTiles[0].Tile);

        Assert.Equal(2, result.SkippedTiles.Count);
        Assert.Contains(result.SkippedTiles, s => s.Tile == t1 && s.Reason == "not-tilled");
        Assert.Contains(result.SkippedTiles, s => s.Tile == t2 && s.Reason == "already-has-crop");

        // Existing crop preserved untouched
        Assert.Equal("existing-crop-id", observer.GetDirtState("Farm", t2).CropId);

        // Only 1 seed deducted
        Assert.Equal(4, actor.GetItemCount("472"));
    }

    [Fact]
    public void OutOfSeeds_SafeTerminalState()
    {
        // Only 1 seed available, but 3 tiles requested
        var (machine, actor, observer, _) = CreateHarness(seedItemId: "472", initialSeedCount: 1);

        var t1 = new TileCoordinate(10, 12);
        var t2 = new TileCoordinate(11, 12);
        var t3 = new TileCoordinate(12, 12);

        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetDirt(t2, TileDirtState.DryDirt());
        observer.SetDirt(t3, TileDirtState.DryDirt());

        var request = new PlantZoneRequest(
            CommandId: "cmd-3",
            TaskId: "task-3",
            LocationId: "Farm",
            SeedItemId: "472",
            TargetTiles: new[] { t1, t2, t3 }
        );

        bool started = machine.Start(request, out _);
        Assert.True(started);

        machine.StepTicks(200);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.PartiallySucceeded, result.FinalState);
        Assert.Single(result.PlantedTiles);
        Assert.True(result.OutOfSeeds);
        Assert.Equal(2, result.SkippedTiles.Count);
        Assert.All(result.SkippedTiles, s => Assert.Equal("out-of-seeds", s.Reason));
        Assert.Equal(0, actor.GetItemCount("472"));
    }

    [Fact]
    public void SeasonMismatch_SkipsTiles()
    {
        var (machine, actor, observer, adapter) = CreateHarness(seedItemId: "472", initialSeedCount: 5);
        adapter.AllowedSeasons.Clear();
        adapter.AllowedSeasons.Add("summer"); // Only summer allowed, but observer is spring

        var t1 = new TileCoordinate(10, 12);
        observer.SetDirt(t1, TileDirtState.DryDirt());

        var request = new PlantZoneRequest(
            CommandId: "cmd-4",
            TaskId: "task-4",
            LocationId: "Farm",
            SeedItemId: "472",
            TargetTiles: new[] { t1 }
        );

        bool started = machine.Start(request, out _);
        Assert.True(started);

        machine.StepTicks(100);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.Failed, result.FinalState);
        Assert.Empty(result.PlantedTiles);
        Assert.Single(result.SkippedTiles);
        Assert.Equal("invalid-season", result.SkippedTiles[0].Reason);
        // Seeds NOT deducted
        Assert.Equal(5, actor.GetItemCount("472"));
    }

    [Fact]
    public void DeductionFailure_DoesNotPlantFreeCrop()
    {
        var (machine, _, observer, adapter) = CreateHarness(seedItemId: "472", initialSeedCount: 5);
        adapter.SimulateConsumeItemFailure = true;

        var t1 = new TileCoordinate(10, 12);
        observer.SetDirt(t1, TileDirtState.DryDirt());

        var request = new PlantZoneRequest(
            CommandId: "cmd-5",
            TaskId: "task-5",
            LocationId: "Farm",
            SeedItemId: "472",
            TargetTiles: new[] { t1 }
        );

        bool started = machine.Start(request, out _);
        Assert.True(started);

        machine.StepTicks(100);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.Failed, result.FinalState);
        Assert.Empty(result.PlantedTiles);
        Assert.Single(result.FailedTiles);

        // Crop MUST NOT be present in world
        Assert.False(observer.GetDirtState("Farm", t1).HasCrop);
    }

    [Fact]
    public void Cancellation_ReachesSafePointTerminalState()
    {
        var (machine, _, observer, _) = CreateHarness(seedItemId: "472", initialSeedCount: 5);

        var t1 = new TileCoordinate(10, 12);
        var t2 = new TileCoordinate(11, 12);
        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetDirt(t2, TileDirtState.DryDirt());

        var request = new PlantZoneRequest(
            CommandId: "cmd-6",
            TaskId: "task-6",
            LocationId: "Farm",
            SeedItemId: "472",
            TargetTiles: new[] { t1, t2 }
        );

        bool started = machine.Start(request, out _);
        Assert.True(started);

        machine.StepTicks(1);
        machine.RequestCancel("Player requested cancellation");
        machine.StepTicks(50);

        var result = machine.FinalResult;
        Assert.NotNull(result);
        Assert.Equal(ExecutionState.Cancelled, result.FinalState);
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

        var request = new PlantZoneRequest(
            CommandId: "cmd-loc",
            TaskId: "task-loc",
            LocationId: "Farm",
            SeedItemId: "472",
            TargetTiles: new[] { new TileCoordinate(10, 12) }
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
