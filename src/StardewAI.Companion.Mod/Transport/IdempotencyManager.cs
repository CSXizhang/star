using System.Security.Cryptography;
using System.Text;

namespace StardewAI.Companion.Mod.Transport;

public enum IdempotencyStatus
{
    InFlight,
    Completed
}

public enum IdempotencyCheck
{
    New,
    InFlightDuplicate,
    CompletedReplay,
    Conflict,
    CapacityExceeded
}

public sealed class IdempotencyRecord
{
    public string IdempotencyKey { get; }
    public string CommandId { get; }
    public string SemanticFingerprint { get; }
    public IdempotencyStatus Status { get; set; }
    public SkillResultPayload? CachedResult { get; set; }
    public DateTimeOffset CreatedAt { get; init; } = DateTimeOffset.UtcNow;

    public IdempotencyRecord(string idempotencyKey, string commandId, string semanticFingerprint, IdempotencyStatus status)
    {
        IdempotencyKey = idempotencyKey;
        CommandId = commandId;
        SemanticFingerprint = semanticFingerprint;
        Status = status;
    }
}

/// <summary>
/// Manages recently received idempotency keys within the active game session.
/// Enforces idempotent execution: replaying completed results, acknowledging in-flight
/// tasks, rejecting conflicting parameter submissions, and refusing silent evictions.
/// </summary>
public sealed class IdempotencyManager
{
    private readonly object _lock = new();
    private readonly Dictionary<string, IdempotencyRecord> _records = new(StringComparer.Ordinal);
    private readonly int _maxCapacity;

    public int Count
    {
        get
        {
            lock (_lock)
            {
                return _records.Count;
            }
        }
    }

    public int MaxCapacity => _maxCapacity;

    public IdempotencyManager(int maxCapacity = 256)
    {
        _maxCapacity = maxCapacity > 0 ? maxCapacity : 256;
    }

    /// <summary>
    /// Computes a canonical, full semantic fingerprint of a skill.execute command.
    /// Covers commandId, taskId, skillId, skillVersion, expectedWorldRevision,
    /// cancelPolicy, policyDecisionId, locationId, canonical sorted tiles (absent for
    /// chest skills), chestTile, canonical sorted itemIds, and all budgets.
    /// </summary>
    public static string ComputeSemanticFingerprint(SkillExecutePayload payload)
    {
        ArgumentNullException.ThrowIfNull(payload);

        var sb = new StringBuilder(256);
        sb.Append(payload.CommandId).Append('|')
          .Append(payload.TaskId).Append('|')
          .Append(payload.SkillId).Append('|')
          .Append(payload.SkillVersion).Append('|')
          .Append(payload.ExpectedWorldRevision).Append('|')
          .Append(payload.CancelPolicy).Append('|')
          .Append(payload.PolicyDecisionId).Append('|');

        if (payload.Parameters != null)
        {
            sb.Append(payload.Parameters.LocationId).Append('|');
            if (payload.Parameters.Tiles != null)
            {
                // Sort tiles deterministically by X then Y
                var sortedTiles = payload.Parameters.Tiles
                    .OrderBy(t => t.X)
                    .ThenBy(t => t.Y);

                foreach (var tile in sortedTiles)
                {
                    sb.Append(tile.X).Append(',').Append(tile.Y).Append(';');
                }
            }
            sb.Append('|');

            // Chest skills: chest tile normalized as "x,y" (empty when absent)
            if (payload.Parameters.ChestTile is { } chestTile)
            {
                sb.Append(chestTile.X).Append(',').Append(chestTile.Y);
            }
            sb.Append('|');

            // Chest skills: item ids sorted deterministically (empty when absent)
            if (payload.Parameters.ItemIds != null)
            {
                foreach (var itemId in payload.Parameters.ItemIds.OrderBy(i => i, StringComparer.Ordinal))
                {
                    sb.Append(itemId).Append(';');
                }
            }
            sb.Append('|');
        }

        if (payload.Budgets != null)
        {
            sb.Append(payload.Budgets.MaxGameMinutes).Append('|')
              .Append(payload.Budgets.MaxStamina.ToString("F2")).Append('|')
              .Append(payload.Budgets.MaxWater);
        }

        byte[] hashBytes = SHA256.HashData(Encoding.UTF8.GetBytes(sb.ToString()));
        return Convert.ToHexString(hashBytes).ToLowerInvariant();
    }

    /// <summary>
    /// Checks the given idempotency key against session history.
    /// Never evicts used keys in the same session; rejects new keys if capacity is exhausted.
    /// </summary>
    public IdempotencyCheck Check(
        string idempotencyKey,
        string commandId,
        string semanticFingerprint,
        out SkillResultPayload? cachedResult)
    {
        ArgumentNullException.ThrowIfNull(idempotencyKey);
        ArgumentNullException.ThrowIfNull(commandId);
        ArgumentNullException.ThrowIfNull(semanticFingerprint);

        lock (_lock)
        {
            if (!_records.TryGetValue(idempotencyKey, out var existing))
            {
                if (_records.Count >= _maxCapacity)
                {
                    cachedResult = null;
                    return IdempotencyCheck.CapacityExceeded;
                }

                cachedResult = null;
                return IdempotencyCheck.New;
            }

            // Conflict check: if the key was previously used for different command semantics
            if (!string.Equals(existing.CommandId, commandId, StringComparison.Ordinal) ||
                !string.Equals(existing.SemanticFingerprint, semanticFingerprint, StringComparison.Ordinal))
            {
                cachedResult = null;
                return IdempotencyCheck.Conflict;
            }

            if (existing.Status == IdempotencyStatus.InFlight)
            {
                cachedResult = null;
                return IdempotencyCheck.InFlightDuplicate;
            }

            cachedResult = existing.CachedResult;
            return IdempotencyCheck.CompletedReplay;
        }
    }

    /// <summary>
    /// Registers an idempotency key as currently in-flight.
    /// Rejects silently if capacity is full without eviction.
    /// </summary>
    public bool TryMarkInFlight(string idempotencyKey, string commandId, string semanticFingerprint)
    {
        lock (_lock)
        {
            if (_records.ContainsKey(idempotencyKey))
            {
                return true;
            }

            if (_records.Count >= _maxCapacity)
            {
                return false;
            }

            var record = new IdempotencyRecord(idempotencyKey, commandId, semanticFingerprint, IdempotencyStatus.InFlight);
            _records[idempotencyKey] = record;
            return true;
        }
    }

    /// <summary>
    /// Stores the terminal result for an idempotency key upon task completion.
    /// Updates the existing record or records a new one if space permits.
    /// </summary>
    public void MarkCompleted(string idempotencyKey, SkillResultPayload result)
    {
        lock (_lock)
        {
            if (_records.TryGetValue(idempotencyKey, out var record))
            {
                record.Status = IdempotencyStatus.Completed;
                record.CachedResult = result;
            }
            else if (_records.Count < _maxCapacity)
            {
                var newRecord = new IdempotencyRecord(idempotencyKey, result.CommandId, string.Empty, IdempotencyStatus.Completed)
                {
                    CachedResult = result
                };
                _records[idempotencyKey] = newRecord;
            }
        }
    }

    /// <summary>
    /// Clears all recorded idempotency keys (e.g. upon save reload or session reset).
    /// </summary>
    public void Clear()
    {
        lock (_lock)
        {
            _records.Clear();
        }
    }
}
