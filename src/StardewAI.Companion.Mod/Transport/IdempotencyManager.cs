using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

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
    CapacityExceeded,
    ReceiptUnavailable
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
    private string? _completedArchiveDirectory;
    private bool _archiveUnavailable;
    private readonly Dictionary<string, ArchivedIdentity> _archived = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _archivedCommands = new(StringComparer.Ordinal);
    private sealed record ArchivedIdentity(string CommandId, string SemanticFingerprint, string Path);
    private sealed record CompletedReceipt(string IdempotencyKey, string CommandId,
        string SemanticFingerprint, DateTimeOffset CreatedAt, SkillResultPayload Result);
    // Match the live transport, including typed objects nested in result details.
    private static readonly JsonSerializerOptions ReceiptJson = new()
        { PropertyNamingPolicy = JsonNamingPolicy.CamelCase, MaxDepth = 128 };

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

    public IdempotencyManager(int maxCapacity = 256, string? completedArchiveDirectory = null)
    {
        _maxCapacity = maxCapacity > 0 ? maxCapacity : 256;
        if (completedArchiveDirectory is null) return;
        _completedArchiveDirectory = Path.GetFullPath(completedArchiveDirectory);
        try
        {
            Directory.CreateDirectory(_completedArchiveDirectory);
            foreach (string path in Directory.EnumerateFiles(_completedArchiveDirectory, "*.json"))
            {
                var receipt = ReadReceipt(path);
                if (!string.Equals(Path.GetFileName(path), ReceiptFileName(receipt.IdempotencyKey), StringComparison.Ordinal))
                    throw new InvalidDataException("Receipt identity does not match its archive path.");
                _archived.Add(receipt.IdempotencyKey, new(receipt.CommandId, receipt.SemanticFingerprint, path));
                _archivedCommands.TryAdd(receipt.CommandId, receipt.IdempotencyKey);
            }
        }
        catch (Exception ex) when (ex is IOException or InvalidDataException or UnauthorizedAccessException or JsonException or ArgumentException)
        {
            // Missing/corrupt receipts must never make an old command executable again.
            _archiveUnavailable = true;
        }
    }

    public bool ReceiptStorageUnavailable { get { lock (_lock) return _archiveUnavailable; } }

    private static string ReceiptFileName(string key) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(key))) + ".json";

    private static CompletedReceipt ReadReceipt(string path)
    {
        var receipt = JsonSerializer.Deserialize<CompletedReceipt>(File.ReadAllText(path), ReceiptJson);
        if (receipt is null || receipt.IdempotencyKey is null || receipt.CommandId is null
            || receipt.SemanticFingerprint is null || receipt.Result is null
            || receipt.Result.CommandId != receipt.CommandId)
            throw new InvalidDataException("Completed receipt has invalid identity or result.");
        return receipt;
    }

    private bool TryReadArchived(string key, ArchivedIdentity identity, out SkillResultPayload? result)
    {
        result = null;
        try
        {
            var receipt = ReadReceipt(identity.Path);
            if (receipt.IdempotencyKey != key || receipt.CommandId != identity.CommandId
                || receipt.SemanticFingerprint != identity.SemanticFingerprint)
                throw new InvalidDataException("Completed receipt identity changed.");
            result = receipt.Result;
            return true;
        }
        catch (Exception ex) when (ex is IOException or InvalidDataException or UnauthorizedAccessException or JsonException)
        {
            _archiveUnavailable = true;
            return false;
        }
    }

    private bool MakeRoom()
    {
        if (_archiveUnavailable) return false;
        if (_records.Count < _maxCapacity) return true;
        if (_completedArchiveDirectory is null) return false;
        var record = _records.Values.FirstOrDefault(r => r.Status == IdempotencyStatus.Completed && r.CachedResult is not null);
        if (record is null) return false; // Never remove an in-flight command.
        string path = Path.Combine(_completedArchiveDirectory, ReceiptFileName(record.IdempotencyKey));
        string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            var receipt = new CompletedReceipt(record.IdempotencyKey, record.CommandId,
                record.SemanticFingerprint, record.CreatedAt, record.CachedResult!);
            using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                JsonSerializer.Serialize(stream, receipt, ReceiptJson);
                stream.Flush(flushToDisk: true);
            }
            File.Move(temporary, path); // Publish atomically before freeing the hot slot.
            _archived.Add(record.IdempotencyKey, new(record.CommandId, record.SemanticFingerprint, path));
            _archivedCommands.TryAdd(record.CommandId, record.IdempotencyKey);
            _records.Remove(record.IdempotencyKey);
            return true;
        }
        catch (Exception ex) when (ex is IOException or InvalidDataException or UnauthorizedAccessException or JsonException)
        {
            _archiveUnavailable = true;
            return false; // Keep the original cached result if persistence failed.
        }
        finally
        {
            try { if (File.Exists(temporary)) File.Delete(temporary); }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { }
        }
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
            // Preserve every extended parameter (item identity, crafting recipe, building identity,
            // destination and region). Otherwise changing a construction request could replay an
            // unrelated cached success under the same key.
            var extras = System.Text.Json.JsonSerializer.SerializeToNode(payload.Parameters)!.AsObject();
            foreach (string key in new[] { "locationId", "tiles", "chestTile", "itemIds" }) extras.Remove(key);
            sb.Append(extras.ToJsonString()).Append('|');
            if (payload.SkillId is "place-items" or "remove-items")
                sb.Append(System.Text.Json.JsonSerializer.Serialize(payload.Parameters.Tiles)).Append('|');
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
    /// Completed receipts may leave the hot cache only after durable archival.
    /// Their identities and original results remain available for the whole session.
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
                if (_archived.TryGetValue(idempotencyKey, out var archived))
                {
                    cachedResult = null;
                    if (archived.CommandId != commandId || archived.SemanticFingerprint != semanticFingerprint)
                        return IdempotencyCheck.Conflict;
                    return TryReadArchived(idempotencyKey, archived, out cachedResult)
                        ? IdempotencyCheck.CompletedReplay : IdempotencyCheck.ReceiptUnavailable;
                }
                if (!MakeRoom())
                {
                    cachedResult = null;
                    return _archiveUnavailable ? IdempotencyCheck.ReceiptUnavailable : IdempotencyCheck.CapacityExceeded;
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
    /// Rejects if all hot entries are in flight or completed receipts cannot be preserved.
    /// </summary>
    public bool TryMarkInFlight(string idempotencyKey, string commandId, string semanticFingerprint)
    {
        lock (_lock)
        {
            if (_records.ContainsKey(idempotencyKey) || _archived.ContainsKey(idempotencyKey))
            {
                return true;
            }

            if (!MakeRoom())
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
            else if (!_archived.ContainsKey(idempotencyKey) && MakeRoom())
            {
                var newRecord = new IdempotencyRecord(idempotencyKey, result.CommandId, string.Empty, IdempotencyStatus.Completed)
                {
                    CachedResult = result
                };
                _records[idempotencyKey] = newRecord;
            }
        }
    }

    /// <summary>Reads a cached terminal by command identity without registering or executing work.</summary>
    public bool TryGetCompletedResult(string commandId, out SkillResultPayload? result)
    {
        lock (_lock)
        {
            result = _records.Values.FirstOrDefault(record => record.CommandId == commandId
                && record.Status == IdempotencyStatus.Completed)?.CachedResult;
            if (result is null && _archivedCommands.TryGetValue(commandId, out var key))
                return TryReadArchived(key, _archived[key], out result);
            return result is not null;
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
            _archived.Clear();
            _archivedCommands.Clear();
            if (_completedArchiveDirectory is not null)
            {
                // An explicit session reset gets a new directory; retain prior evidence.
                _completedArchiveDirectory = Path.Combine(_completedArchiveDirectory, "reset-" + Guid.NewGuid().ToString("N"));
                try { Directory.CreateDirectory(_completedArchiveDirectory); _archiveUnavailable = false; }
                catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { _archiveUnavailable = true; }
            }
        }
    }
}
