using System.Text.Json.Nodes;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class ConstructionCommandTests
{
    private static SkillExecutePayload Payload(string skill, WaterZoneParameters parameters) => new(
        "construction-command", "construction-task", skill, "0.1", 1, parameters,
        new ExecutionBudgets(60, 50, 0), "safe-point", "test-policy");

    private static (CompanionMechanicsCoordinator coordinator, TestNativeActionAdapter adapter) Harness()
    {
        var actor = new MechanicsActor("constructor", initialPose: new AuthoritativePose("Farm", 10, 10, FacingDirection.Down));
        var observer = new SimulatedWorldObserver();
        var adapter = new TestNativeActionAdapter();
        return (new CompanionMechanicsCoordinator(actor, observer, new SameMapNavigator(observer), new TestWateringCanAdapter(observer), nativeActionAdapter: adapter), adapter);
    }

    private static EnvelopeDto Envelope() => new("0.1", "skill.execute", "message", "runtime", 1, 1, DateTimeOffset.UtcNow, new JsonObject());

    [Theory]
    [InlineData("place-items")]
    [InlineData("remove-items")]
    public void MissingExpectedIdentityRejectsBeforeAnyNativeAction(string skill)
    {
        var (coordinator, adapter) = Harness();
        var payload = Payload(skill, new WaterZoneParameters("Farm", new() { new(10, 11) }));
        Assert.False(coordinator.TryAcceptSkillExecute(payload, Envelope(), out var rejection));
        Assert.Equal("INVALID_PARAMETERS", rejection!.Error!.Code);
        Assert.Equal(0, adapter.CallCount);
    }

    [Fact]
    public void CraftCountDispatchesIndependentCancellableIterations()
    {
        var (coordinator, adapter) = Harness();
        var payload = Payload("craft-items", new WaterZoneParameters("Farm", null) { RecipeName = "Wood Floor", ItemCount = 3 });
        Assert.True(coordinator.TryAcceptSkillExecute(payload, Envelope(), out _));
        for (int i = 0; i < 100; i++) coordinator.Update(null, i);
        Assert.Equal(3, adapter.CallCount);
        Assert.All(adapter.Calls, c => Assert.Equal(NativeActionKind.CraftItems, c.Kind));
    }

    [Theory]
    [InlineData(0)]
    [InlineData(65)]
    public void CraftCountOutsideBoundRejectsWithoutConsumption(int count)
    {
        var (coordinator, adapter) = Harness();
        Assert.False(coordinator.TryAcceptSkillExecute(Payload("craft-items", new WaterZoneParameters("Farm", null) { RecipeName = "Wood Floor", ItemCount = count }), Envelope(), out _));
        Assert.Equal(0, adapter.CallCount);
    }

    [Fact]
    public void BuildingMoveKeepsObservedIdentityAndDestination()
    {
        var (coordinator, adapter) = Harness();
        string id = Guid.NewGuid().ToString();
        Assert.True(coordinator.TryAcceptSkillExecute(Payload("move-building", new WaterZoneParameters("Farm", null) { BuildingName = id, Tile = new(10, 11) }), Envelope(), out _));
        for (int i = 0; i < 100; i++) coordinator.Update(null, i);
        var call = Assert.Single(adapter.Calls);
        Assert.Equal(NativeActionKind.MoveBuilding, call.Kind);
        Assert.Equal(id, call.Target.TargetId);
        Assert.Equal(new TileCoordinate(10, 11), call.Target.Tile);
    }

    [Fact]
    public void UnavailableSpatialObserverDoesNotReturnFabricatedMap()
    {
        var (coordinator, _) = Harness();
        Assert.False(coordinator.TryAcceptSkillExecute(Payload("inspect-location", new WaterZoneParameters("Farm", null)), Envelope(), out var result));
        Assert.Equal("SPATIAL_OBSERVATION_UNAVAILABLE", result!.Error!.Code);
        Assert.Null(result.Details);
    }
}
