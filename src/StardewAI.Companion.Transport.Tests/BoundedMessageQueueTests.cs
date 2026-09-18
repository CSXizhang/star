using System.Text.Json.Nodes;
using Xunit;

namespace StardewAI.Companion.Mod.Transport.Tests;

public class BoundedMessageQueueTests
{
    private static EnvelopeDto CreateEnvelope(string messageType, string messageId, string? commandId = null)
    {
        var payload = new JsonObject();
        if (commandId != null)
        {
            payload["commandId"] = commandId;
        }

        return new EnvelopeDto(
            ProtocolVersion: "0.1",
            MessageType: messageType,
            MessageId: messageId,
            SenderInstanceId: "sender-1",
            SequenceNumber: 0,
            WorldRevision: 1,
            SentAt: DateTimeOffset.UtcNow,
            Payload: payload
        );
    }

    [Fact]
    public void NormalQueue_CapacityLimitEnforced()
    {
        var queue = new BoundedMessageQueue(maxNormalCapacity: 3, maxPriorityCapacity: 2);

        Assert.True(queue.TryEnqueue(CreateEnvelope("skill.pause", "m1"), out _));
        Assert.True(queue.TryEnqueue(CreateEnvelope("skill.pause", "m2"), out _));
        Assert.True(queue.TryEnqueue(CreateEnvelope("skill.pause", "m3"), out _));

        // 4th normal message must be rejected
        Assert.False(queue.TryEnqueue(CreateEnvelope("skill.pause", "m4"), out _));
        Assert.Equal(3, queue.NormalCount);
    }

    [Fact]
    public void CancelBeforeExecute_PurgesMatchingQueuedExecuteCommand()
    {
        var queue = new BoundedMessageQueue(maxNormalCapacity: 5, maxPriorityCapacity: 5);

        // Enqueue an execute command for cmd-100
        var execEnv = CreateEnvelope("skill.execute", "exec-msg-1", "cmd-100");
        Assert.True(queue.TryEnqueue(execEnv, out _));
        Assert.Equal(1, queue.NormalCount);

        // Enqueue a cancel targeting cmd-100
        var cancelEnv = CreateEnvelope("skill.cancel", "cancel-msg-1", "cmd-100");
        bool enqueued = queue.TryEnqueue(cancelEnv, out bool purged);

        Assert.True(enqueued);
        Assert.True(purged, "Cancel must purge the matching queued execute command");
        Assert.Equal(0, queue.NormalCount); // Purged from normal queue!
        Assert.Equal(1, queue.PriorityCount);
        Assert.True(queue.IsCommandTombstoned("cmd-100"));

        // If another execute for cmd-100 arrives, it must be rejected by tombstone
        Assert.False(queue.TryEnqueue(execEnv, out bool purgedLater));
        Assert.True(purgedLater);
    }

    [Fact]
    public void PriorityQueue_BoundsAndCoalescesDuplicateCancels()
    {
        var queue = new BoundedMessageQueue(maxNormalCapacity: 5, maxPriorityCapacity: 2);

        var cancel1 = CreateEnvelope("skill.cancel", "c1", "cmd-A");
        var cancel1Duplicate = CreateEnvelope("skill.cancel", "c2", "cmd-A");
        var cancel2 = CreateEnvelope("skill.cancel", "c3", "cmd-B");
        var cancel3 = CreateEnvelope("skill.cancel", "c4", "cmd-C");

        Assert.True(queue.TryEnqueue(cancel1, out _));
        // Duplicate cancel for cmd-A should coalesce without consuming extra capacity
        Assert.True(queue.TryEnqueue(cancel1Duplicate, out _));
        Assert.Equal(1, queue.PriorityCount);

        // Add second distinct cancel
        Assert.True(queue.TryEnqueue(cancel2, out _));
        Assert.Equal(2, queue.PriorityCount);

        // 3rd distinct cancel must exceed capacity of 2
        Assert.False(queue.TryEnqueue(cancel3, out _));
    }

    [Fact]
    public void Dequeue_PriorityMessagesReturnedBeforeNormalMessages()
    {
        var queue = new BoundedMessageQueue(maxNormalCapacity: 5, maxPriorityCapacity: 5);

        queue.TryEnqueue(CreateEnvelope("skill.pause", "normal-1"), out _);
        queue.TryEnqueue(CreateEnvelope("skill.cancel", "cancel-1", "cmd-X"), out _);
        queue.TryEnqueue(CreateEnvelope("skill.pause", "normal-2"), out _);

        Assert.True(queue.TryDequeue(out var first));
        Assert.NotNull(first);
        Assert.Equal("skill.cancel", first.MessageType);

        Assert.True(queue.TryDequeue(out var second));
        Assert.NotNull(second);
        Assert.Equal("normal-1", second.MessageId);

        Assert.True(queue.TryDequeue(out var third));
        Assert.NotNull(third);
        Assert.Equal("normal-2", third.MessageId);
    }
}
