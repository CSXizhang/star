using System.Text.Json;
using StardewAI.Companion.Mod.Discovery;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class DiscoveryServiceTests : IDisposable
{
    private readonly string _modDir;
    private readonly TestMonitor _monitor;

    public DiscoveryServiceTests()
    {
        _modDir = Path.Combine(Path.GetTempPath(), "DiscoveryServiceTests_" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(_modDir);
        _monitor = new TestMonitor();
    }

    public void Dispose()
    {
        if (Directory.Exists(_modDir))
        {
            try { Directory.Delete(_modDir, true); } catch { }
        }
    }

    [Fact]
    public void Publish_WritesDiscoveryJson_WithExactCoordinates_AndNoTokenInLogs()
    {
        var service = new DiscoveryService(_modDir, _monitor);
        string expectedPath = Path.Combine(_modDir, "data", "transport-discovery.json");
        Assert.Equal(expectedPath, service.DiscoveryPath);

        string saveId = "ExampleFarm_12345";
        string sessionId = "sess-abc";
        int port = 54321;
        string secretToken = "super-secret-token-xyz-123";

        service.Publish(saveId, sessionId, port, secretToken);

        Assert.True(File.Exists(expectedPath));
        string json = File.ReadAllText(expectedPath);

        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;

        Assert.Equal("0.1", root.GetProperty("protocolVersion").GetString());
        Assert.Equal("ws://127.0.0.1:54321/", root.GetProperty("endpoint").GetString());
        Assert.Equal("127.0.0.1", root.GetProperty("host").GetString());
        Assert.Equal(54321, root.GetProperty("port").GetInt32());
        Assert.Equal(secretToken, root.GetProperty("sessionToken").GetString());
        Assert.Equal(saveId, root.GetProperty("saveId").GetString());
        Assert.Equal(sessionId, root.GetProperty("gameSessionId").GetString());

        // CRITICAL INVARIANT: The secret token must NEVER appear in normal logs
        foreach (var log in _monitor.LoggedMessages)
        {
            Assert.DoesNotContain(secretToken, log);
        }
    }

    [Fact]
    public void Invalidate_RemovesDiscoveryFile()
    {
        var service = new DiscoveryService(_modDir, _monitor);
        service.Publish("save-1", "sess-1", 12345, "secret-token");
        Assert.True(File.Exists(service.DiscoveryPath));

        service.Invalidate();
        Assert.False(File.Exists(service.DiscoveryPath));
    }
}
