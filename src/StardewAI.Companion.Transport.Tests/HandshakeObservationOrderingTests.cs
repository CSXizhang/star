using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Transport.Tests;

public class HandshakeObservationOrderingTests
{
    [Fact]
    public async Task ObservationBeforeHelloCannotPrecedeWelcomeOnWire()
    {
        var handler = new Handler();
        using var server = new WebSocketTransportServer(0, "test-token", handler);
        server.Start();
        using var client = new ClientWebSocket();
        client.Options.SetRequestHeader("Authorization", "Bearer test-token");
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await client.ConnectAsync(new Uri($"ws://127.0.0.1:{server.Port}/"), deadline.Token);
        // A native observation from the independent life channel can arrive here.
        await server.SendSnapshotAsync(handler.CaptureCurrentSnapshot(7), 7);
        var hello = Encoding.UTF8.GetBytes("{\"protocolVersion\":\"0.1\",\"messageType\":\"runtime.hello\",\"messageId\":\"hello\",\"senderInstanceId\":\"test\",\"sequenceNumber\":0,\"worldRevision\":1,\"sentAt\":\"2026-10-07T00:00:00Z\",\"payload\":{\"runtimeVersion\":\"test\",\"supportedProtocolVersions\":[\"0.1\"]}}");
        await client.SendAsync(new ArraySegment<byte>(hello), WebSocketMessageType.Text, true, deadline.Token);
        byte[] buffer = new byte[8192];
        var first = await client.ReceiveAsync(new ArraySegment<byte>(buffer), deadline.Token);
        using var document = JsonDocument.Parse(buffer.AsMemory(0, first.Count));
        Assert.Equal("mod.welcome", document.RootElement.GetProperty("messageType").GetString());
        client.Abort();
    }

    private sealed class Handler : ITransportHandler
    {
        public bool TryAcceptSkillExecute(SkillExecutePayload payload, EnvelopeDto envelope, out SkillResultPayload? rejectResult)
        { rejectResult = null; return false; }
        public void OnCancelSkill(SkillCancelPayload payload, EnvelopeDto envelope) { }
        public void OnPauseSkill(SkillPausePayload payload, EnvelopeDto envelope) { }
        public void OnResumeSkill(SkillResumePayload payload, EnvelopeDto envelope) { }
        public void OnTransportDisconnected(string reason) { }
        public WorldSnapshotPayload CaptureCurrentSnapshot(long revision) => new(
            CapturedRevision: revision,
            Companion: new CompanionSnapshot("Farm", 64, 15, 2, 270, 270, 1, 40, true, "idle"),
            World: new WorldStateSnapshot("Farm", 600, "spring", 2, false));
    }
}
