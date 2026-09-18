namespace StardewAI.Companion.Mod.Transport;

/// <summary>
/// Thread-safe strictly bounded message queue bridging background network threads and
/// the game main thread. Prioritizes skill cancellation messages, bounds both normal
/// and priority queues, coalesces redundant cancels, and supports cancel-before-execute
/// queue purging and tombstones.
/// </summary>
public sealed class BoundedMessageQueue
{
    private readonly object _lock = new();
    private readonly LinkedList<(EnvelopeDto Envelope, long Generation)> _priorityQueue = new();
    private readonly LinkedList<(EnvelopeDto Envelope, long Generation)> _normalQueue = new();
    private readonly HashSet<string> _cancelledCommandTombstones = new(StringComparer.Ordinal);
    private readonly int _maxNormalCapacity;
    private readonly int _maxPriorityCapacity;

    public int MaxNormalCapacity => _maxNormalCapacity;
    public int MaxPriorityCapacity => _maxPriorityCapacity;

    public int NormalCount
    {
        get
        {
            lock (_lock)
            {
                return _normalQueue.Count;
            }
        }
    }

    public int PriorityCount
    {
        get
        {
            lock (_lock)
            {
                return _priorityQueue.Count;
            }
        }
    }

    public int TotalCount
    {
        get
        {
            lock (_lock)
            {
                return _priorityQueue.Count + _normalQueue.Count;
            }
        }
    }

    public BoundedMessageQueue(int maxNormalCapacity = 64, int maxPriorityCapacity = 16)
    {
        if (maxNormalCapacity <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(maxNormalCapacity), "Normal capacity must be greater than zero.");
        }
        if (maxPriorityCapacity <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(maxPriorityCapacity), "Priority capacity must be greater than zero.");
        }

        _maxNormalCapacity = maxNormalCapacity;
        _maxPriorityCapacity = maxPriorityCapacity;
    }

    /// <summary>
    /// Enqueues an incoming envelope without generation tag (defaults to 0).
    /// </summary>
    public bool TryEnqueue(EnvelopeDto envelope, out bool purgedQueuedCommand)
        => TryEnqueue(envelope, 0, out purgedQueuedCommand);

    /// <summary>
    /// Enqueues an incoming envelope tagged with its receiving socket generation.
    /// If the envelope is a cancel command:
    /// 1. Immediately purges any matching execute command still waiting in the normal queue.
    /// 2. Records a tombstone for the command ID.
    /// 3. Coalesces redundant cancel messages or enqueues into the bounded priority queue.
    /// </summary>
    /// <returns>True if successfully enqueued or coalesced; false if queue capacity was exceeded.</returns>
    public bool TryEnqueue(EnvelopeDto envelope, long generation, out bool purgedQueuedCommand)
    {
        ArgumentNullException.ThrowIfNull(envelope);
        purgedQueuedCommand = false;

        lock (_lock)
        {
            bool isCancel = string.Equals(envelope.MessageType, "skill.cancel", StringComparison.OrdinalIgnoreCase);

            if (isCancel)
            {
                string? targetCommandId = ExtractTargetCommandId(envelope);
                if (!string.IsNullOrEmpty(targetCommandId))
                {
                    // Cancel-before-execute: record tombstone
                    _cancelledCommandTombstones.Add(targetCommandId);

                    // Purge from normal queue if it hasn't executed yet
                    for (var node = _normalQueue.First; node != null; node = node.Next)
                    {
                        var queuedCmdId = ExtractTargetCommandId(node.Value.Envelope);
                        if (string.Equals(queuedCmdId, targetCommandId, StringComparison.Ordinal))
                        {
                            _normalQueue.Remove(node);
                            purgedQueuedCommand = true;
                            break;
                        }
                    }

                    // Coalesce: check if already present in priority queue
                    foreach (var existing in _priorityQueue)
                    {
                        if (string.Equals(ExtractTargetCommandId(existing.Envelope), targetCommandId, StringComparison.Ordinal))
                        {
                            // Already queued for dispatch; coalesce safely
                            return true;
                        }
                    }
                }

                // Strictly bound priority queue
                if (_priorityQueue.Count >= _maxPriorityCapacity)
                {
                    return false;
                }

                _priorityQueue.AddLast((envelope, generation));
                return true;
            }

            // Normal messages: reject if command has already been tombstoned by an earlier cancel
            string? cmdId = ExtractTargetCommandId(envelope);
            if (!string.IsNullOrEmpty(cmdId) && _cancelledCommandTombstones.Contains(cmdId))
            {
                purgedQueuedCommand = true;
                return false;
            }

            if (_normalQueue.Count >= _maxNormalCapacity)
            {
                return false;
            }

            _normalQueue.AddLast((envelope, generation));
            return true;
        }
    }

    /// <summary>
    /// Checks whether a command ID has been cancelled / tombstoned before execution.
    /// </summary>
    public bool IsCommandTombstoned(string commandId)
    {
        ArgumentNullException.ThrowIfNull(commandId);
        lock (_lock)
        {
            return _cancelledCommandTombstones.Contains(commandId);
        }
    }

    /// <summary>
    /// Dequeues the next available envelope. Priority messages (cancellations) are
    /// always returned before normal messages.
    /// </summary>
    public bool TryDequeue(out EnvelopeDto? envelope)
        => TryDequeue(out envelope, out _);

    /// <summary>
    /// Dequeues the next available envelope along with its socket generation.
    /// Priority messages (cancellations) are always returned before normal messages.
    /// </summary>
    public bool TryDequeue(out EnvelopeDto? envelope, out long generation)
    {
        lock (_lock)
        {
            if (_priorityQueue.First != null)
            {
                var item = _priorityQueue.First.Value;
                envelope = item.Envelope;
                generation = item.Generation;
                _priorityQueue.RemoveFirst();
                return true;
            }

            if (_normalQueue.First != null)
            {
                var item = _normalQueue.First.Value;
                envelope = item.Envelope;
                generation = item.Generation;
                _normalQueue.RemoveFirst();
                return true;
            }

            envelope = null;
            generation = -1;
            return false;
        }
    }

    /// <summary>
    /// Purges all queued messages older than the given active socket generation.
    /// </summary>
    public void PurgeOlderThanGeneration(long activeGeneration)
    {
        lock (_lock)
        {
            for (var node = _priorityQueue.First; node != null;)
            {
                var next = node.Next;
                if (node.Value.Generation < activeGeneration)
                {
                    _priorityQueue.Remove(node);
                }
                node = next;
            }

            for (var node = _normalQueue.First; node != null;)
            {
                var next = node.Next;
                if (node.Value.Generation < activeGeneration)
                {
                    _normalQueue.Remove(node);
                }
                node = next;
            }
        }
    }

    /// <summary>
    /// Clears all pending messages from both queues and clears tombstones.
    /// </summary>
    public void Clear()
    {
        lock (_lock)
        {
            _priorityQueue.Clear();
            _normalQueue.Clear();
            _cancelledCommandTombstones.Clear();
        }
    }

    private static string? ExtractTargetCommandId(EnvelopeDto envelope)
    {
        if (envelope.Payload.TryGetPropertyValue("commandId", out var cNode) && cNode != null)
        {
            return cNode.ToString();
        }
        return envelope.CorrelationId;
    }
}
