using System.Text.Json;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>
/// The compact decision context reads real native weather and the companion
/// wallet. These tests pin the wire contract: the new fields are serialized, and
/// an older snapshot without them deserializes to null (unknown) rather than a
/// fabricated zero/clear.
/// </summary>
public class SnapshotSchemaTests
{
    [Theory]
    [InlineData(0, false)]
    [InlineData(100, true)]
    public void ActualRefillSnapshotCarriesNearbyScopeCenterRadiusAndTruncation(int count, bool truncated)
    {
        var actor = new MechanicsActor("refill-scope");
        actor.UpdatePose("Farm", new(20, 20), FacingDirection.Down);
        var observer = new SimulatedWorldObserver();
        observer.SetRefillTiles(Enumerable.Range(0, count)
            .Select(i => new TileCoordinate(15 + i % 10, 15 + i / 10)));
        var coordinator = new CompanionMechanicsCoordinator(actor, observer,
            new SameMapNavigator(observer), new TestWateringCanAdapter(observer));
        using var json = JsonDocument.Parse(JsonSerializer.Serialize(coordinator.CaptureCurrentSnapshot(1), Options));
        var farming = json.RootElement.GetProperty("farming");
        Assert.Equal("Farm", farming.GetProperty("location").GetString());
        Assert.Equal("near-companion", farming.GetProperty("refillWaterScope").GetString());
        Assert.Equal(12, farming.GetProperty("refillWaterRadius").GetInt32());
        Assert.Equal(20, farming.GetProperty("refillWaterCenter").GetProperty("x").GetInt32());
        Assert.Equal(20, farming.GetProperty("refillWaterCenter").GetProperty("y").GetInt32());
        Assert.Equal(truncated, farming.GetProperty("refillWaterTilesTruncated").GetBoolean());
        Assert.False(farming.GetProperty("refillWaterMapComplete").GetBoolean());
        Assert.Equal(count == 0 ? 0 : 16, farming.GetProperty("refillWaterTiles").GetArrayLength());
    }

    [Theory]
    [InlineData("Farm", false, "observed")]
    [InlineData("Coop-unique-one", false, "not-observed")]
    [InlineData("Farm", true, "unavailable")]
    public void ActualFarmWorkSnapshotIdentifiesUnobservedMapsAndFailures(string location, bool failScan, string status)
    {
        var actor = new MechanicsActor("farm-scope");
        actor.UpdatePose(location, new(1, 1), FacingDirection.Down);
        var observer = new SimulatedWorldObserver { CurrentLocationName = location };
        if (failScan) observer.FarmWorkScanFailure = new InvalidOperationException("Unavailable farm scan");
        var coordinator = new CompanionMechanicsCoordinator(actor, observer,
            new SameMapNavigator(observer), new TestWateringCanAdapter(observer));
        using var json = JsonDocument.Parse(JsonSerializer.Serialize(coordinator.CaptureCurrentSnapshot(1), Options));
        foreach (var farm in new[] { json.RootElement.GetProperty("farmWork"), json.RootElement.GetProperty("world").GetProperty("farmWork") })
        {
            Assert.Equal(location, farm.GetProperty("locationId").GetString());
            Assert.Equal(status, farm.GetProperty("observationStatus").GetString());
        }
    }

    [Fact]
    public void FarmScopeAndDailyUsageWireStayBackwardCompatibleWithoutInventingUsage()
    {
        var unknown = FarmWorkSnapshot.CreateEmpty();
        Assert.Null(unknown.LocationId);
        Assert.Equal("unknown", unknown.ObservationStatus);
        const string legacy = "{\"requestId\":\"r\",\"saveId\":\"s\",\"mode\":\"free\",\"paused\":false,\"dailySpend\":0,\"status\":\"ok\"}";
        var state = JsonSerializer.Deserialize<AutonomyStatePayload>(legacy, Options)!;
        Assert.Null(state.UsageTodayText);
        state = state with { UsageTodayText = "2026-10-02 · 今日token 1,000 · API估算 unavailable" };
        var encoded = JsonSerializer.Serialize(state, Options);
        Assert.Equal(state.UsageTodayText, JsonSerializer.Deserialize<AutonomyStatePayload>(encoded, Options)!.UsageTodayText);
    }
    [Fact]
    public void Ground_item_native_quality_survives_snapshot_wire_and_missing_is_unknown()
    {
        var actor = new MechanicsActor("quality-test");
        actor.UpdatePose("Farm", new TileCoordinate(1, 1), FacingDirection.Down);
        var observer = new SimulatedWorldObserver();
        observer.SetGroundItems(new[]
        {
            new GroundItemScanInfo(new(1, 1), "object", "(O)176", "Egg", 1, false, false, true, null, Quality: 2),
            new GroundItemScanInfo(new(2, 1), "dropped", "(O)176", "Egg", 1, true, false, true, null),
        });
        var coordinator = new CompanionMechanicsCoordinator(actor, observer,
            new SameMapNavigator(observer), new TestWateringCanAdapter(observer));
        using var json = JsonDocument.Parse(JsonSerializer.Serialize(coordinator.CaptureCurrentSnapshot(1), Options));
        var items = json.RootElement.GetProperty("farming").GetProperty("groundItems");
        Assert.Equal(2, items[0].GetProperty("quality").GetInt32());
        Assert.False(items[1].TryGetProperty("quality", out _));
    }

    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull,
    };

    [Fact]
    public void Snapshot_publishes_native_weather_and_companion_wallet()
    {
        var payload = new WorldSnapshotPayload(
            CapturedRevision: 42,
            Companion: new CompanionSnapshot(
                LocationId: "Farm",
                TileX: 61,
                TileY: 17,
                FacingDirection: 2,
                Stamina: 210.5f,
                MaxStamina: 270,
                WaterCanLevel: 33,
                MaxWaterCanLevel: 40,
                HasWateringCan: true,
                Activity: "idle",
                AvailableMoney: 1250,
                MoneyStatus: "ok"),
            World: new WorldStateSnapshot(
                CurrentLocation: "Farm",
                TimeOfDay: 610,
                Season: "spring",
                DayOfMonth: 6,
                IsRaining: false,
                Year: 1,
                WeatherIcon: "5"),
            Inventory: null);

        var json = JsonSerializer.Serialize(payload, Options);

        Assert.Contains("\"weatherIcon\":\"5\"", json);
        Assert.Contains("\"availableMoney\":1250", json);
        Assert.Contains("\"moneyStatus\":\"ok\"", json);
        // isRaining=false must never be serialized as a "clear" weather claim.
        Assert.DoesNotContain("clear", json);
    }

    [Fact]
    public void Older_snapshot_without_new_fields_stays_unknown()
    {
        const string legacy =
            "{" +
            "\"capturedRevision\":1," +
            "\"companion\":{\"locationId\":\"Farm\",\"tileX\":10,\"tileY\":10,\"facingDirection\":2," +
            "\"stamina\":100,\"maxStamina\":270,\"waterCanLevel\":40,\"maxWaterCanLevel\":40," +
            "\"hasWateringCan\":true,\"activity\":\"idle\"}," +
            "\"world\":{\"currentLocation\":\"Farm\",\"timeOfDay\":600,\"season\":\"winter\"," +
            "\"dayOfMonth\":3,\"isRaining\":false,\"year\":1}" +
            "}";

        var payload = JsonSerializer.Deserialize<WorldSnapshotPayload>(legacy, Options);

        Assert.NotNull(payload);
        Assert.Null(payload!.World.WeatherIcon);
        Assert.Null(payload.Companion.AvailableMoney);
        Assert.Null(payload.Companion.MoneyStatus);
        Assert.False(payload.World.IsRaining);
    }

    [Fact]
    public void Snapshot_without_wallet_omits_the_fields_instead_of_zero()
    {
        var payload = new WorldSnapshotPayload(
            CapturedRevision: 1,
            Companion: new CompanionSnapshot(
                LocationId: "Farm", TileX: 1, TileY: 1, FacingDirection: 0,
                Stamina: 10, MaxStamina: 270, WaterCanLevel: 0, MaxWaterCanLevel: 40,
                HasWateringCan: true, Activity: "idle",
                AvailableMoney: null, MoneyStatus: "missing"),
            World: new WorldStateSnapshot(
                CurrentLocation: "Farm", TimeOfDay: 600, Season: "spring",
                DayOfMonth: 1, IsRaining: true, Year: 1, WeatherIcon: null),
            Inventory: null);

        var json = JsonSerializer.Serialize(payload, Options);

        Assert.DoesNotContain("\"availableMoney\"", json);
        Assert.DoesNotContain("\"weatherIcon\"", json);
        Assert.Contains("\"moneyStatus\":\"missing\"", json);
    }
}
