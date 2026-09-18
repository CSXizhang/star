using Xunit;

namespace StardewAI.Companion.Mod.Transport.Tests;

public class IdempotencyManagerTests
{
    private static SkillExecutePayload CreateSamplePayload(
        string commandId = "cmd-1",
        string taskId = "task-1",
        string locationId = "Farm",
        List<TileCoord>? tiles = null,
        int maxGameMinutes = 60,
        float maxStamina = 50.0f,
        int maxWater = 20)
    {
        return new SkillExecutePayload(
            CommandId: commandId,
            TaskId: taskId,
            SkillId: "water-zone",
            SkillVersion: "0.1",
            ExpectedWorldRevision: 1,
            Parameters: new WaterZoneParameters(
                LocationId: locationId,
                Tiles: tiles ?? new List<TileCoord> { new(64, 15), new(64, 16) }
            ),
            Budgets: new ExecutionBudgets(
                MaxGameMinutes: maxGameMinutes,
                MaxStamina: maxStamina,
                MaxWater: maxWater
            ),
            CancelPolicy: "safe-point",
            PolicyDecisionId: "policy-allow"
        );
    }

    [Fact]
    public void Fingerprint_IdenticalPayloads_ProduceSameHash()
    {
        var p1 = CreateSamplePayload();
        var p2 = CreateSamplePayload();

        var hash1 = IdempotencyManager.ComputeSemanticFingerprint(p1);
        var hash2 = IdempotencyManager.ComputeSemanticFingerprint(p2);

        Assert.Equal(hash1, hash2);
    }

    [Fact]
    public void Fingerprint_TileOrderVariation_ProducesSameCanonicalHash()
    {
        // Tiles in (64, 15), (64, 16) vs (64, 16), (64, 15)
        var p1 = CreateSamplePayload(tiles: new List<TileCoord> { new(64, 15), new(64, 16) });
        var p2 = CreateSamplePayload(tiles: new List<TileCoord> { new(64, 16), new(64, 15) });

        var hash1 = IdempotencyManager.ComputeSemanticFingerprint(p1);
        var hash2 = IdempotencyManager.ComputeSemanticFingerprint(p2);

        Assert.Equal(hash1, hash2);
    }

    [Fact]
    public void Fingerprint_DifferentTilesSameCount_ProducesDifferentHash()
    {
        // Regression test: HandleSkillExecute previously hashed LocationId:Tiles.Count
        var p1 = CreateSamplePayload(tiles: new List<TileCoord> { new(64, 15), new(64, 16) });
        var p2 = CreateSamplePayload(tiles: new List<TileCoord> { new(10, 20), new(30, 40) });

        var hash1 = IdempotencyManager.ComputeSemanticFingerprint(p1);
        var hash2 = IdempotencyManager.ComputeSemanticFingerprint(p2);

        Assert.NotEqual(hash1, hash2);
    }

    [Fact]
    public void Fingerprint_DifferentBudgets_ProducesDifferentHash()
    {
        var p1 = CreateSamplePayload(maxStamina: 50.0f);
        var p2 = CreateSamplePayload(maxStamina: 40.0f);

        var hash1 = IdempotencyManager.ComputeSemanticFingerprint(p1);
        var hash2 = IdempotencyManager.ComputeSemanticFingerprint(p2);

        Assert.NotEqual(hash1, hash2);
    }

    [Fact]
    public void Fingerprint_DifferentTaskId_ProducesDifferentHash()
    {
        var p1 = CreateSamplePayload(taskId: "task-1");
        var p2 = CreateSamplePayload(taskId: "task-2");

        var hash1 = IdempotencyManager.ComputeSemanticFingerprint(p1);
        var hash2 = IdempotencyManager.ComputeSemanticFingerprint(p2);

        Assert.NotEqual(hash1, hash2);
    }

    [Fact]
    public void Capacity_NoSilentEviction_RejectsNewWorkWhenFull()
    {
        var manager = new IdempotencyManager(maxCapacity: 3);

        Assert.True(manager.TryMarkInFlight("k1", "c1", "fp1"));
        Assert.True(manager.TryMarkInFlight("k2", "c2", "fp2"));
        Assert.True(manager.TryMarkInFlight("k3", "c3", "fp3"));
        Assert.Equal(3, manager.Count);

        // Fourth key must be rejected due to capacity exceeded, NOT evict k1
        var check4 = manager.Check("k4", "c4", "fp4", out _);
        Assert.Equal(IdempotencyCheck.CapacityExceeded, check4);

        Assert.False(manager.TryMarkInFlight("k4", "c4", "fp4"));

        // Crucial: verify k1 is STILL present and intact (no silent eviction)
        var check1 = manager.Check("k1", "c1", "fp1", out _);
        Assert.Equal(IdempotencyCheck.InFlightDuplicate, check1);
    }

    [Fact]
    public void Conflict_PreservesOriginalRecord()
    {
        var manager = new IdempotencyManager(maxCapacity: 10);
        manager.TryMarkInFlight("k1", "c1", "fp1");

        var originalResult = new SkillResultPayload(
            CommandId: "c1",
            TaskId: "t1",
            TerminalState: "succeeded",
            CompletedCount: 2,
            SkippedCount: 0,
            FailedCount: 0,
            FinalWorldRevision: 5,
            Effects: new List<Dictionary<string, object>>()
        );
        manager.MarkCompleted("k1", originalResult);

        // Conflicting check with different fingerprint
        var checkConflict = manager.Check("k1", "c1", "different-fp", out var cachedResult);
        Assert.Equal(IdempotencyCheck.Conflict, checkConflict);
        Assert.Null(cachedResult);

        // Replay check with original fingerprint must STILL return the original result
        var checkReplay = manager.Check("k1", "c1", "fp1", out var replayed);
        Assert.Equal(IdempotencyCheck.CompletedReplay, checkReplay);
        Assert.NotNull(replayed);
        Assert.Equal("c1", replayed.CommandId);
        Assert.Equal("succeeded", replayed.TerminalState);
    }
}
