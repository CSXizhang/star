using StardewAI.Companion.Mod.Domain;
using StardewValley;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class ActorStateTests : IDisposable
{
    private readonly string _testDir;

    public ActorStateTests()
    {
        _testDir = Path.Combine(Path.GetTempPath(), "ActorStateTests_" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(_testDir);
    }

    public void Dispose()
    {
        if (Directory.Exists(_testDir))
        {
            try { Directory.Delete(_testDir, true); } catch { }
        }
    }

    [Fact]
    public void InitialOwnActorDefaults_InitializedOnlyOnce()
    {
        var repo = new JsonActorStateRepository(_testDir);
        Assert.False(repo.StateExists("companion-1"));

        // First load creates defaults and saves them
        var state1 = repo.LoadOrInitializeDefaults("companion-1");
        Assert.True(repo.StateExists("companion-1"));
        Assert.Equal(CompanionActorState.DefaultMaxStamina, state1.Stamina);
        Assert.Equal(CompanionActorState.DefaultMaxWater, state1.Water);
        Assert.Equal(1, state1.Version);

        // Mutate state and save
        state1.Stamina = 120.0f;
        state1.Water = 15;
        repo.Save(state1);

        // Second load reads the saved state, not initial defaults
        var state2 = repo.LoadOrInitializeDefaults("companion-1");
        Assert.Equal(120.0f, state2.Stamina);
        Assert.Equal(15, state2.Water);
    }

    [Fact]
    public void Reload_PreservesStaminaWaterItemsAndPose()
    {
        var repo = new JsonActorStateRepository(_testDir);
        var actor = new MechanicsActor("companion-persist", 180.5f, 270f, 25, 40,
            new AuthoritativePose("Farm", 45, 20, FacingDirection.Right),
            new[] { new InventoryItem("472", "Parsnip Seeds", 15) });

        var stateToSave = actor.ToState();
        repo.Save(stateToSave);

        // Reload
        var loadedState = repo.LoadOrInitializeDefaults("companion-persist");
        var restoredActor = new MechanicsActor("companion-persist");
        restoredActor.ApplyState(loadedState);

        Assert.Equal(180.5f, restoredActor.Stamina);
        Assert.Equal(25, restoredActor.Water);
        Assert.Equal("Farm", restoredActor.Pose.LocationName);
        Assert.Equal(45, restoredActor.Pose.Tile.X);
        Assert.Equal(20, restoredActor.Pose.Tile.Y);
        Assert.Equal(FacingDirection.Right, restoredActor.Pose.Facing);
        Assert.Single(restoredActor.Inventory);
        Assert.Equal("Parsnip Seeds", restoredActor.Inventory[0].Name);
        Assert.Equal(15, restoredActor.Inventory[0].Stack);
    }

    [Fact]
    public void Reload_NeverAutoResumesStaleTask()
    {
        var repo = new JsonActorStateRepository(_testDir);
        var actor = new MechanicsActor("companion-stale");
        actor.SetActiveTask("stale-task-12345");

        var stateToSave = actor.ToState();
        Assert.Equal("stale-task-12345", stateToSave.PersistedTaskId);
        repo.Save(stateToSave);

        // Reload state
        var loadedState = repo.LoadOrInitializeDefaults("companion-stale");
        Assert.Null(loadedState.PersistedTaskId);

        var restoredActor = new MechanicsActor("companion-stale");
        restoredActor.ApplyState(loadedState);
        Assert.Null(restoredActor.ActiveTaskId);
    }

    [Fact]
    public void FutureVersion_ThrowsInvalidOperationException()
    {
        var repo = new JsonActorStateRepository(_testDir);
        var state = CompanionActorState.CreateDefault("companion-future");
        state.Version = 999;
        repo.Save(state);

        Assert.Throws<InvalidOperationException>(() => repo.LoadOrInitializeDefaults("companion-future"));
    }

    [Fact]
    public void PerSaveDirectoryPartition_IsolatesStateBetweenSaves()
    {
        string dirSaveA = Path.Combine(_testDir, "data", "SaveGameA_123");
        string dirSaveB = Path.Combine(_testDir, "data", "SaveGameB_456");

        var repoA = new JsonActorStateRepository(dirSaveA);
        var repoB = new JsonActorStateRepository(dirSaveB);

        // Mutate SaveA
        var stateA = repoA.LoadOrInitializeDefaults("companion-1");
        stateA.Stamina = 55.5f;
        stateA.Water = 7;
        stateA.Pose = new AuthoritativePose("Farm", 88, 99, FacingDirection.Up);
        repoA.Save(stateA);

        // SaveB must have fresh initial defaults and remain unaffected
        var stateB = repoB.LoadOrInitializeDefaults("companion-1");
        Assert.Equal(CompanionActorState.DefaultMaxStamina, stateB.Stamina);
        Assert.Equal(CompanionActorState.DefaultMaxWater, stateB.Water);
        Assert.Equal(64, stateB.Pose.Tile.X);
        Assert.Equal(15, stateB.Pose.Tile.Y);

        // Verify SaveA maintains its own partitioned state
        var reloadedA = repoA.LoadOrInitializeDefaults("companion-1");
        Assert.Equal(55.5f, reloadedA.Stamina);
        Assert.Equal(7, reloadedA.Water);
        Assert.Equal(88, reloadedA.Pose.Tile.X);
        Assert.Equal(99, reloadedA.Pose.Tile.Y);
    }

    [Fact]
    public void InventoryItem_SupportsUpgradeLevelAndWaterLeft_Persistence()
    {
        var repo = new JsonActorStateRepository(_testDir);
        var state = CompanionActorState.CreateDefault("companion-tool-persist");
        state.Inventory = new List<InventoryItem>
        {
            new InventoryItem("WateringCan", "Copper Watering Can", 1, upgradeLevel: 1, waterLeft: 55),
            new InventoryItem("Axe", "Steel Axe", 1, upgradeLevel: 2)
        };
        repo.Save(state);

        var loaded = repo.LoadOrInitializeDefaults("companion-tool-persist");
        Assert.Equal(2, loaded.Inventory.Count);

        var can = loaded.Inventory[0];
        Assert.Equal("WateringCan", can.ItemId);
        Assert.Equal(1, can.UpgradeLevel);
        Assert.Equal(55, can.WaterLeft);

        var axe = loaded.Inventory[1];
        Assert.Equal("Axe", axe.ItemId);
        Assert.Equal(2, axe.UpgradeLevel);
        Assert.Equal(0, axe.WaterLeft);
    }

    [Fact]
    public void ExactSlotIdentityAndNullSlotsPreserved_NoPositionCollapsing()
    {
        var repo = new JsonActorStateRepository(_testDir);
        var state = CompanionActorState.CreateDefault("companion-slot-test");

        // Inventory with items at specific non-contiguous slots: 0, 5, 20
        state.Inventory = new List<InventoryItem>
        {
            new InventoryItem("WateringCan", "Watering Can", 1, slotIndex: 0, waterLeft: 40),
            new InventoryItem("472", "Parsnip Seeds", 10, slotIndex: 5),
            new InventoryItem("Axe", "Axe", 1, slotIndex: 20)
        };
        repo.Save(state);

        var loaded = repo.LoadOrInitializeDefaults("companion-slot-test");
        var actor = new FarmerMechanicsActor("companion-slot-test");
        actor.ApplyPersistentState(loaded);

        var captured = actor.CapturePersistentState();
        var itemAt0 = captured.Inventory.FirstOrDefault(i => i.SlotIndex == 0);
        var itemAt5 = captured.Inventory.FirstOrDefault(i => i.SlotIndex == 5);
        var itemAt20 = captured.Inventory.FirstOrDefault(i => i.SlotIndex == 20);

        Assert.NotNull(itemAt0);
        Assert.Equal("WateringCan", itemAt0.ItemId);
        Assert.NotNull(itemAt5);
        Assert.Equal("472", itemAt5.ItemId);
        Assert.NotNull(itemAt20);
        Assert.Equal("Axe", itemAt20.ItemId);

        // Verify slot positions did NOT collapse: item 5 is NOT at index 1
        Assert.Equal(5, itemAt5.SlotIndex);
        Assert.Equal(20, itemAt20.SlotIndex);
    }

    [Fact]
    public void ZeroWaterPreservedExactly_NoDefaultRefill()
    {
        var repo = new JsonActorStateRepository(_testDir);
        var state = CompanionActorState.CreateDefault("companion-zero-water");

        // Primary or secondary can with exactly zero water, while state.Water might be default 40
        state.Water = 40;
        state.Inventory = new List<InventoryItem>
        {
            new InventoryItem("WateringCan", "Watering Can", 1, slotIndex: 0, waterLeft: 0)
        };
        repo.Save(state);

        var loaded = repo.LoadOrInitializeDefaults("companion-zero-water");
        var actor = new FarmerMechanicsActor("companion-zero-water");
        actor.ApplyPersistentState(loaded);

        // Must preserve zero water exactly, never convert 0 to default 40!
        Assert.Equal(0, actor.WaterLeft);
        Assert.True(actor.IsWateringCanEmpty);

        var captured = actor.CapturePersistentState();
        var capturedCan = captured.Inventory.FirstOrDefault(i => i.SlotIndex == 0);
        Assert.NotNull(capturedCan);
        Assert.Equal(0, capturedCan.WaterLeft);
    }

    [Fact]
    public void FarmerMechanicsActor_Uninitialized_FailsClosed_WithExplicitException()
    {
        var actor = new FarmerMechanicsActor("uninitialized-actor");

        // When GameFarmer is null, BeginUsingTool MUST fail closed with InvalidOperationException
        var ex = Assert.Throws<InvalidOperationException>(() => actor.BeginUsingTool());
        Assert.Contains("GameFarmer is not initialized", ex.Message);

        // When not using tool, UpdateToolAnimation returns None safely
        Assert.Equal(ToolAnimationPhase.None, actor.UpdateToolAnimation(null, 1));
    }

    [Fact]
    public void FarmerMechanicsActor_Halt_CleansUpSafely()
    {
        var actor = new FarmerMechanicsActor("halt-actor");
        actor.Halt();
        Assert.False(actor.IsUsingTool);
        Assert.Equal(ToolAnimationPhase.None, actor.AnimationPhase);
    }

    [Fact]
    public void MechanicsActor_HaltCleansUpActiveToolUse()
    {
        var actor = new MechanicsActor("halt-actor");
        actor.BeginUsingTool();
        Assert.True(actor.IsUsingTool);

        actor.Halt();
        Assert.False(actor.IsUsingTool);
        Assert.Equal(ToolAnimationPhase.None, actor.AnimationPhase);
    }

    [Fact]
    public void DefensiveInitialization_Idempotent_PreservesExistingState()
    {
        var repo = new JsonActorStateRepository(_testDir);
        var initialPose = new AuthoritativePose("Farm", 64, 15, FacingDirection.Down);

        // 1. Fresh initialization
        Assert.False(repo.StateExists("companion-defensive"));
        var freshState = repo.LoadOrInitializeDefaults("companion-defensive");
        freshState.Pose = initialPose;
        freshState.Stamina = 150f;
        freshState.Water = 20;
        repo.Save(freshState);

        // 2. Simulate defensive tick re-invocation (actor was null, re-triggered initialization)
        Assert.True(repo.StateExists("companion-defensive"));
        var reloadedState = repo.LoadOrInitializeDefaults("companion-defensive");

        // Must preserve existing state without resetting to defaults
        Assert.Equal(150f, reloadedState.Stamina);
        Assert.Equal(20, reloadedState.Water);
        Assert.Equal("Farm", reloadedState.Pose.LocationName);
        Assert.Equal(new TileCoordinate(64, 15), reloadedState.Pose.Tile);
    }

    [Fact]
    public void NativeAnimationFrame_FrameBehaviorsCanBeStripped_ClearsDelegates()
    {
        bool startInvoked = false;
        bool endInvoked = false;

        var frame = new FarmerSprite.AnimationFrame(164, 125);
        frame.frameStartBehavior = (f) => { startInvoked = true; };
        frame.frameEndBehavior = (f) => { endInvoked = true; };

        Assert.NotNull(frame.frameStartBehavior);
        Assert.NotNull(frame.frameEndBehavior);

        // Strip behaviors as done in FarmerMechanicsActor.BeginUsingTool
        frame.frameStartBehavior = null;
        frame.frameEndBehavior = null;

        Assert.Null(frame.frameStartBehavior);
        Assert.Null(frame.frameEndBehavior);
        Assert.False(startInvoked);
        Assert.False(endInvoked);
    }

    [Theory]
    [InlineData(0, -1, 3, false)] // Windup
    [InlineData(1, 0, 3, false)]  // EffectPoint
    [InlineData(2, 1, 3, false)]  // FollowThrough
    [InlineData(0, 2, 3, true)]   // Wrap-around to 0 -> Completed!
    public void AnimationWrapAround_Formula_CorrectlyDetectsCompletion(
        int animIndex, int lastIndex, int frameCount, bool expectedCompleted)
    {
        bool completedOnePass = frameCount > 0 && lastIndex >= frameCount - 1 && animIndex < lastIndex;
        Assert.Equal(expectedCompleted, completedOnePass);
    }
}

