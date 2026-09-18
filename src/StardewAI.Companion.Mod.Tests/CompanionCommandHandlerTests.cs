using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class CompanionCommandHandlerTests
{
    private (CompanionMechanicsCoordinator coordinator, MechanicsActor actor, SimulatedWorldObserver observer) CreateCoordinatorHarness()
    {
        var pose = new AuthoritativePose("Farm", 10, 10, FacingDirection.Down);
        var actor = new MechanicsActor("cmd-companion", initialPose: pose);
        var avatar = new CompanionAvatar(actor);
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var navigator = new SameMapNavigator(observer);
        var adapter = new TestWateringCanAdapter(observer);

        var coordinator = new CompanionMechanicsCoordinator(actor, observer, navigator, adapter, avatar);
        return (coordinator, actor, observer);
    }

    #region ParseWaterCommand Tests

    [Fact]
    public void ParseWaterCommand_NullOrEmptyArgs_ReturnsFalseWithUsageMessage()
    {
        bool resultNull = CompanionCommandHandler.ParseWaterCommand(null, out _, out _, out _, out var errorNull);
        Assert.False(resultNull);
        Assert.NotNull(errorNull);
        Assert.Contains("Usage: ai_water", errorNull);

        bool resultEmpty = CompanionCommandHandler.ParseWaterCommand(Array.Empty<string>(), out _, out _, out _, out var errorEmpty);
        Assert.False(resultEmpty);
        Assert.NotNull(errorEmpty);
        Assert.Contains("Usage: ai_water", errorEmpty);

        bool resultSingle = CompanionCommandHandler.ParseWaterCommand(new[] { "10" }, out _, out _, out _, out var errorSingle);
        Assert.False(resultSingle);
        Assert.NotNull(errorSingle);
        Assert.Contains("Usage: ai_water", errorSingle);
    }

    [Fact]
    public void ParseWaterCommand_ValidTwoArgs_DefaultsRadiusToZero()
    {
        bool result = CompanionCommandHandler.ParseWaterCommand(new[] { "64", "15" }, out int x, out int y, out int radius, out string? error);

        Assert.True(result);
        Assert.Null(error);
        Assert.Equal(64, x);
        Assert.Equal(15, y);
        Assert.Equal(0, radius);
    }

    [Fact]
    public void ParseWaterCommand_ValidThreeArgs_ParsesRadius()
    {
        bool resultR1 = CompanionCommandHandler.ParseWaterCommand(new[] { "64", "15", "1" }, out int x1, out int y1, out int r1, out string? err1);
        Assert.True(resultR1);
        Assert.Null(err1);
        Assert.Equal(64, x1);
        Assert.Equal(15, y1);
        Assert.Equal(1, r1);

        bool resultR2 = CompanionCommandHandler.ParseWaterCommand(new[] { "30", "40", "2" }, out int x2, out int y2, out int r2, out string? err2);
        Assert.True(resultR2);
        Assert.Null(err2);
        Assert.Equal(30, x2);
        Assert.Equal(40, y2);
        Assert.Equal(2, r2);
    }

    [Theory]
    [InlineData("-1", "10", "Invalid x coordinate")]
    [InlineData("10", "-5", "Invalid y coordinate")]
    [InlineData("abc", "10", "Invalid x coordinate")]
    [InlineData("10", "xyz", "Invalid y coordinate")]
    public void ParseWaterCommand_InvalidCoordinates_ReturnsFalseWithSpecificError(string arg0, string arg1, string expectedErrorSubstring)
    {
        bool result = CompanionCommandHandler.ParseWaterCommand(new[] { arg0, arg1 }, out _, out _, out _, out string? error);
        Assert.False(result);
        Assert.NotNull(error);
        Assert.Contains(expectedErrorSubstring, error);
    }

    [Theory]
    [InlineData("-1")]
    [InlineData("3")]
    [InlineData("99")]
    [InlineData("radius")]
    public void ParseWaterCommand_InvalidRadius_ReturnsFalseWithRadiusError(string invalidRadius)
    {
        bool result = CompanionCommandHandler.ParseWaterCommand(new[] { "64", "15", invalidRadius }, out _, out _, out _, out string? error);
        Assert.False(result);
        Assert.NotNull(error);
        Assert.Contains("Invalid radius", error);
    }

    #endregion

    #region FindTilledUnwateredTiles Tests

    [Fact]
    public void FindTilledUnwateredTiles_RadiusZero_OnlyEvaluatesCenterTile()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        var center = new TileCoordinate(15, 20);

        // Untilled dirt -> returns empty
        var emptyResult = CompanionCommandHandler.FindTilledUnwateredTiles(observer, "Farm", center.X, center.Y, 0);
        Assert.Empty(emptyResult);

        // Tilled dry dirt -> returns center tile
        observer.SetDirt(center, TileDirtState.DryDirt(hasCrop: true));
        var matchedResult = CompanionCommandHandler.FindTilledUnwateredTiles(observer, "Farm", center.X, center.Y, 0);
        Assert.Single(matchedResult);
        Assert.Equal(center, matchedResult[0]);

        // Already watered -> returns empty
        observer.SetDirt(center, TileDirtState.WateredDirt());
        var wateredResult = CompanionCommandHandler.FindTilledUnwateredTiles(observer, "Farm", center.X, center.Y, 0);
        Assert.Empty(wateredResult);
    }

    [Fact]
    public void FindTilledUnwateredTiles_RadiusOne_Scans3x3WindowCorrectly()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        int cx = 10;
        int cy = 10;

        // Populate a 3x3 grid around (10, 10)
        // (10, 10): tilled dry (should include)
        observer.SetDirt(new TileCoordinate(10, 10), TileDirtState.DryDirt(hasCrop: true));
        // (9, 10): tilled dry (should include)
        observer.SetDirt(new TileCoordinate(9, 10), TileDirtState.DryDirt(hasCrop: true));
        // (11, 10): tilled watered (should exclude)
        observer.SetDirt(new TileCoordinate(11, 10), TileDirtState.WateredDirt());
        // (10, 9): untilled (should exclude - default state is untilled)
        // (10, 11): tilled dry (should include)
        observer.SetDirt(new TileCoordinate(10, 11), TileDirtState.DryDirt(hasCrop: true));

        var tiles = CompanionCommandHandler.FindTilledUnwateredTiles(observer, "Farm", cx, cy, 1);

        Assert.Equal(3, tiles.Count);
        Assert.Contains(new TileCoordinate(10, 10), tiles);
        Assert.Contains(new TileCoordinate(9, 10), tiles);
        Assert.Contains(new TileCoordinate(10, 11), tiles);
        Assert.DoesNotContain(new TileCoordinate(11, 10), tiles);
        Assert.DoesNotContain(new TileCoordinate(10, 9), tiles);
    }

    [Fact]
    public void FindTilledUnwateredTiles_RadiusTwo_Scans5x5WindowAndClampsRadius()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        int cx = 20;
        int cy = 20;

        // Set corner of 5x5 grid (dx = -2, dy = -2) -> (18, 18)
        var farCorner = new TileCoordinate(18, 18);
        observer.SetDirt(farCorner, TileDirtState.DryDirt(hasCrop: true));

        // Set tile outside 5x5 grid (dx = 3, dy = 0) -> (23, 20)
        var outsideTile = new TileCoordinate(23, 20);
        observer.SetDirt(outsideTile, TileDirtState.DryDirt(hasCrop: true));

        // Call with radius 5, which must be clamped to MaxRadius (2)
        var tiles = CompanionCommandHandler.FindTilledUnwateredTiles(observer, "Farm", cx, cy, 5);

        Assert.Single(tiles);
        Assert.Equal(farCorner, tiles[0]);
    }

    [Fact]
    public void FindTilledUnwateredTiles_NearOrigin_ExcludesNegativeTilesSafely()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        // Center at (0, 0), radius 1 would have negative x and y coords
        observer.SetDirt(new TileCoordinate(0, 0), TileDirtState.DryDirt(hasCrop: true));
        observer.SetDirt(new TileCoordinate(1, 0), TileDirtState.DryDirt(hasCrop: true));

        var tiles = CompanionCommandHandler.FindTilledUnwateredTiles(observer, "Farm", 0, 0, 1);

        Assert.Equal(2, tiles.Count);
        Assert.Contains(new TileCoordinate(0, 0), tiles);
        Assert.Contains(new TileCoordinate(1, 0), tiles);
        Assert.All(tiles, t => Assert.True(t.X >= 0 && t.Y >= 0));
    }

    [Fact]
    public void FindTilledUnwateredTiles_InvalidParameters_Throws()
    {
        var observer = new SimulatedWorldObserver { CurrentLocationName = "Farm" };
        Assert.Throws<ArgumentNullException>(() => CompanionCommandHandler.FindTilledUnwateredTiles(null!, "Farm", 0, 0, 0));
        Assert.Throws<ArgumentException>(() => CompanionCommandHandler.FindTilledUnwateredTiles(observer, "", 0, 0, 0));
    }

    #endregion

    #region FormatStatusLine Tests

    [Fact]
    public void FormatStatusLine_WhenIdle_ReturnsAiIdle()
    {
        string status = CompanionCommandHandler.FormatStatusLine(isExecuting: false, isPaused: false, currentTargetIndex: 0, totalTargets: 5);
        Assert.Equal("AI: idle", status);
    }

    [Fact]
    public void FormatStatusLine_WhenPaused_ReturnsAiPaused()
    {
        string status = CompanionCommandHandler.FormatStatusLine(isExecuting: true, isPaused: true, currentTargetIndex: 1, totalTargets: 3);
        Assert.Equal("AI: paused", status);
    }

    [Theory]
    [InlineData(0, 3, "AI: watering 1/3")]
    [InlineData(1, 3, "AI: watering 2/3")]
    [InlineData(2, 3, "AI: watering 3/3")]
    public void FormatStatusLine_WhenExecuting_ReturnsCurrentOverTotal(int currentIndex, int total, string expected)
    {
        string status = CompanionCommandHandler.FormatStatusLine(isExecuting: true, isPaused: false, currentTargetIndex: currentIndex, totalTargets: total);
        Assert.Equal(expected, status);
    }

    [Fact]
    public void FormatStatusLine_WhenTotalIsZero_ReturnsGenericWatering()
    {
        string status = CompanionCommandHandler.FormatStatusLine(isExecuting: true, isPaused: false, currentTargetIndex: 0, totalTargets: 0);
        Assert.Equal("AI: watering", status);
    }

    #endregion

    #region FormatStatusDetails Tests

    [Fact]
    public void FormatStatusDetails_WhenIdle_OutputsCorrectMetadata()
    {
        var (coordinator, actor, _) = CreateCoordinatorHarness();

        string details = CompanionCommandHandler.FormatStatusDetails(actor, coordinator.StateMachine);

        Assert.Contains("[Companion Status]", details);
        Assert.Contains("Position:  Farm (10, 10)", details);
        Assert.Contains("Stamina: 270.0/270.0", details);
        Assert.Contains("Water: 40/40", details);
        Assert.Contains("Task:      none (idle)", details);
        Assert.Contains("Progress:  idle", details);
        Assert.Contains("Paused:    False", details);
    }

    [Fact]
    public void FormatStatusDetails_WhenExecuting_OutputsTaskAndProgress()
    {
        var (coordinator, actor, observer) = CreateCoordinatorHarness();
        var t1 = new TileCoordinate(10, 11);
        observer.SetDirt(t1, TileDirtState.DryDirt());

        var request = new WaterZoneRequest(
            CommandId: "cmd-details-test",
            TaskId: "task-details-123",
            LocationId: "Farm",
            TargetTiles: new[] { t1 },
            MaxStamina: 50f,
            MaxWater: 10,
            MaxGameMinutes: 30
        );

        bool started = coordinator.TryStartWaterZone(request, envelope: null, out _, out _);
        Assert.True(started);

        string details = CompanionCommandHandler.FormatStatusDetails(actor, coordinator.StateMachine);

        Assert.Contains("[Companion Status]", details);
        Assert.Contains("Task:      task-details-123", details);
        Assert.Contains("Progress:  0/1 watered (0 skipped, 0 failed)", details);
        Assert.Contains("Paused:    False", details);
    }

    #endregion

    #region Notification Events & Task Continuity Tests

    [Fact]
    public void Coordinator_FiresNotifications_OnStartPauseResumeComplete()
    {
        var (coordinator, actor, observer) = CreateCoordinatorHarness();
        var t1 = new TileCoordinate(10, 12);
        observer.SetDirt(t1, TileDirtState.DryDirt());

        var notifications = new List<string>();
        coordinator.OnNotification += msg => notifications.Add(msg);

        var request = new WaterZoneRequest(
            CommandId: "cmd-notif",
            TaskId: "task-notif",
            LocationId: "Farm",
            TargetTiles: new[] { t1 },
            MaxStamina: 50f,
            MaxWater: 10,
            MaxGameMinutes: 60
        );

        // 1. Start
        bool started = coordinator.TryStartWaterZone(request, envelope: null, out _, out _);
        Assert.True(started);
        Assert.Contains(notifications, n => n.Contains("Watering task started"));

        // 2. Pause
        coordinator.StateMachine.RequestPause();
        coordinator.StateMachine.StepTicks(1);
        Assert.Contains(notifications, n => n.Contains("Task paused."));

        // 3. Resume
        coordinator.StateMachine.Resume();
        Assert.Contains(notifications, n => n.Contains("Task resumed."));

        // 4. Complete
        coordinator.StateMachine.StepTicks(100);
        Assert.Contains(notifications, n => n.Contains("Watering task completed"));
        Assert.Equal(ExecutionState.Succeeded, coordinator.StateMachine.CurrentState);
    }

    [Fact]
    public void Coordinator_FiresNotification_OnCancellation()
    {
        var (coordinator, actor, observer) = CreateCoordinatorHarness();
        var t1 = new TileCoordinate(10, 12);
        observer.SetDirt(t1, TileDirtState.DryDirt());

        var notifications = new List<string>();
        coordinator.OnNotification += msg => notifications.Add(msg);

        var request = new WaterZoneRequest(
            CommandId: "cmd-cancel-notif",
            TaskId: "task-cancel-notif",
            LocationId: "Farm",
            TargetTiles: new[] { t1 },
            MaxStamina: 50f,
            MaxWater: 10,
            MaxGameMinutes: 60
        );

        coordinator.TryStartWaterZone(request, envelope: null, out _, out _);
        coordinator.StateMachine.RequestCancel("User cancelled.");
        coordinator.StateMachine.StepTicks(1);

        Assert.Contains(notifications, n => n.Contains("Task cancelled"));
    }

    [Fact]
    public void Coordinator_TaskContinuity_SequentialTasksExecuteCleanly()
    {
        var (coordinator, actor, observer) = CreateCoordinatorHarness();

        var t1 = new TileCoordinate(10, 11);
        var t2 = new TileCoordinate(10, 12);
        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetDirt(t2, TileDirtState.DryDirt());

        // --- Task 1 ---
        var req1 = new WaterZoneRequest(
            CommandId: "cmd-seq-1",
            TaskId: "task-seq-1",
            LocationId: "Farm",
            TargetTiles: new[] { t1 },
            MaxStamina: 50f,
            MaxWater: 10,
            MaxGameMinutes: 60
        );

        bool started1 = coordinator.TryStartWaterZone(req1, envelope: null, out _, out _);
        Assert.True(started1);

        coordinator.StateMachine.StepTicks(100);
        Assert.Equal(ExecutionState.Succeeded, coordinator.StateMachine.CurrentState);
        Assert.Null(actor.ActiveTaskId);
        Assert.False(coordinator.StateMachine.IsExecuting);

        // --- Task 2: immediate dispatch after task 1 completion ---
        var req2 = new WaterZoneRequest(
            CommandId: "cmd-seq-2",
            TaskId: "task-seq-2",
            LocationId: "Farm",
            TargetTiles: new[] { t2 },
            MaxStamina: 50f,
            MaxWater: 10,
            MaxGameMinutes: 60
        );

        bool started2 = coordinator.TryStartWaterZone(req2, envelope: null, out _, out string? reject2);
        Assert.True(started2, $"Task 2 rejected unexpectedly: {reject2}");
        Assert.True(coordinator.StateMachine.IsExecuting);
        Assert.Equal("task-seq-2", actor.ActiveTaskId);

        coordinator.StateMachine.StepTicks(100);
        Assert.Equal(ExecutionState.Succeeded, coordinator.StateMachine.CurrentState);
        Assert.Null(actor.ActiveTaskId);
        Assert.False(coordinator.StateMachine.IsExecuting);

        // Verify both tiles were actually watered
        Assert.True(observer.GetDirtState("Farm", t1).IsWatered);
        Assert.True(observer.GetDirtState("Farm", t2).IsWatered);
    }

    [Fact]
    public void Coordinator_TaskContinuity_AcceptsNextTaskAfterCancellation()
    {
        var (coordinator, actor, observer) = CreateCoordinatorHarness();

        var t1 = new TileCoordinate(10, 15);
        var t2 = new TileCoordinate(10, 11);
        observer.SetDirt(t1, TileDirtState.DryDirt());
        observer.SetDirt(t2, TileDirtState.DryDirt());

        // --- Task 1: Start and immediately cancel ---
        var req1 = new WaterZoneRequest(
            CommandId: "cmd-cancel-1",
            TaskId: "task-cancel-1",
            LocationId: "Farm",
            TargetTiles: new[] { t1 },
            MaxStamina: 50f,
            MaxWater: 10,
            MaxGameMinutes: 60
        );

        coordinator.TryStartWaterZone(req1, envelope: null, out _, out _);
        coordinator.StateMachine.RequestCancel("Player cancelled task 1");
        coordinator.StateMachine.StepTicks(1);
        Assert.False(coordinator.StateMachine.IsExecuting);
        Assert.Null(actor.ActiveTaskId);

        // --- Task 2: Submitting a new task after cancellation must succeed ---
        var req2 = new WaterZoneRequest(
            CommandId: "cmd-cancel-2",
            TaskId: "task-cancel-2",
            LocationId: "Farm",
            TargetTiles: new[] { t2 },
            MaxStamina: 50f,
            MaxWater: 10,
            MaxGameMinutes: 60
        );

        bool started2 = coordinator.TryStartWaterZone(req2, envelope: null, out _, out string? rejectReason);
        Assert.True(started2, $"Task 2 rejected after cancellation: {rejectReason}");
        Assert.True(coordinator.StateMachine.IsExecuting);

        coordinator.StateMachine.StepTicks(100);
        Assert.Equal(ExecutionState.Succeeded, coordinator.StateMachine.CurrentState);
        Assert.Null(actor.ActiveTaskId);
        Assert.True(observer.GetDirtState("Farm", t2).IsWatered);
    }

    #endregion

    #region TryParseInGameCommand Tests

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    public void TryParseInGameCommand_NullOrWhitespace_ReturnsFalseWithUsageMessage(string? input)
    {
        bool result = CompanionCommandHandler.TryParseInGameCommand(input, out var cmd, out string? error);
        Assert.False(result);
        Assert.Null(cmd);
        Assert.NotNull(error);
        Assert.Contains("Command cannot be empty", error);
    }

    [Theory]
    [InlineData("water 64 20", 64, 20, 0)]
    [InlineData("water 64 20 0", 64, 20, 0)]
    [InlineData("water 64 20 1", 64, 20, 1)]
    [InlineData("water 64 20 2", 64, 20, 2)]
    [InlineData("WATER 10 30 1", 10, 30, 1)]
    [InlineData("  water   15   25   2  ", 15, 25, 2)]
    public void TryParseInGameCommand_WaterEnglish_ValidInputs_ParsesCorrectly(string input, int expectedX, int expectedY, int expectedRadius)
    {
        bool result = CompanionCommandHandler.TryParseInGameCommand(input, out var cmd, out string? error);
        Assert.True(result);
        Assert.Null(error);
        Assert.NotNull(cmd);
        Assert.Equal(InGameCommandType.Water, cmd!.Type);
        Assert.Equal(expectedX, cmd.X);
        Assert.Equal(expectedY, cmd.Y);
        Assert.Equal(expectedRadius, cmd.Radius);
    }

    [Theory]
    [InlineData("浇水 64 20", 64, 20, 0)]
    [InlineData("浇水 64 20 0", 64, 20, 0)]
    [InlineData("浇水 64 20 1", 64, 20, 1)]
    [InlineData("浇水 64 20 2", 64, 20, 2)]
    [InlineData("  浇水   100   200   1  ", 100, 200, 1)]
    public void TryParseInGameCommand_WaterChineseAlias_ValidInputs_ParsesCorrectly(string input, int expectedX, int expectedY, int expectedRadius)
    {
        bool result = CompanionCommandHandler.TryParseInGameCommand(input, out var cmd, out string? error);
        Assert.True(result);
        Assert.Null(error);
        Assert.NotNull(cmd);
        Assert.Equal(InGameCommandType.Water, cmd!.Type);
        Assert.Equal(expectedX, cmd.X);
        Assert.Equal(expectedY, cmd.Y);
        Assert.Equal(expectedRadius, cmd.Radius);
    }

    [Theory]
    [InlineData("water", "Usage: water")]
    [InlineData("water 64", "Usage: water")]
    [InlineData("浇水", "Usage: water")]
    [InlineData("浇水 64", "Usage: water")]
    [InlineData("water -1 20", "Invalid x coordinate")]
    [InlineData("water abc 20", "Invalid x coordinate")]
    [InlineData("浇水 -5 20", "Invalid x coordinate")]
    [InlineData("water 64 -1", "Invalid y coordinate")]
    [InlineData("water 64 xyz", "Invalid y coordinate")]
    [InlineData("浇水 64 -10", "Invalid y coordinate")]
    [InlineData("water 64 20 -1", "Invalid radius")]
    [InlineData("water 64 20 3", "Invalid radius")]
    [InlineData("water 64 20 99", "Invalid radius")]
    [InlineData("water 64 20 bad", "Invalid radius")]
    [InlineData("浇水 64 20 5", "Invalid radius")]
    public void TryParseInGameCommand_Water_InvalidArguments_ReturnsFalseWithSpecificError(string input, string expectedSubstring)
    {
        bool result = CompanionCommandHandler.TryParseInGameCommand(input, out var cmd, out string? error);
        Assert.False(result);
        Assert.Null(cmd);
        Assert.NotNull(error);
        Assert.Contains(expectedSubstring, error);
    }

    [Theory]
    [InlineData("pause", InGameCommandType.Pause)]
    [InlineData("PAUSE", InGameCommandType.Pause)]
    [InlineData("  Pause  ", InGameCommandType.Pause)]
    [InlineData("暂停", InGameCommandType.Pause)]
    [InlineData("  暂停  ", InGameCommandType.Pause)]
    [InlineData("resume", InGameCommandType.Resume)]
    [InlineData("RESUME", InGameCommandType.Resume)]
    [InlineData("Resume", InGameCommandType.Resume)]
    [InlineData("继续", InGameCommandType.Resume)]
    [InlineData("cancel", InGameCommandType.Cancel)]
    [InlineData("CANCEL", InGameCommandType.Cancel)]
    [InlineData("Cancel", InGameCommandType.Cancel)]
    [InlineData("取消", InGameCommandType.Cancel)]
    [InlineData("status", InGameCommandType.Status)]
    [InlineData("STATUS", InGameCommandType.Status)]
    [InlineData("Status", InGameCommandType.Status)]
    [InlineData("状态", InGameCommandType.Status)]
    public void TryParseInGameCommand_ControlVerbs_EnglishAndChinese_ParsesCorrectly(string input, InGameCommandType expectedType)
    {
        bool result = CompanionCommandHandler.TryParseInGameCommand(input, out var cmd, out string? error);
        Assert.True(result);
        Assert.Null(error);
        Assert.NotNull(cmd);
        Assert.Equal(expectedType, cmd!.Type);
    }

    [Theory]
    [InlineData("jump")]
    [InlineData("unknown_verb")]
    [InlineData("做饭")]
    public void TryParseInGameCommand_UnknownCommand_ReturnsFalseWithHelpfulMessage(string input)
    {
        bool result = CompanionCommandHandler.TryParseInGameCommand(input, out var cmd, out string? error);
        Assert.False(result);
        Assert.Null(cmd);
        Assert.NotNull(error);
        Assert.Contains("Unknown command", error);
    }

    #endregion
}
