using System.Text.Json;
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
