using System.Text.Json;

namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// File-based JSON implementation of IActorStateRepository.
/// Stores actor state per companion ID and manages initial defaults,
/// version validation, and stale-task suppression on reload.
/// </summary>
public sealed class JsonActorStateRepository : IActorStateRepository
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        PropertyNameCaseInsensitive = true
    };

    private readonly string _storageDirectory;
    private readonly object _ioLock = new();

    public JsonActorStateRepository(string storageDirectory)
    {
        if (string.IsNullOrWhiteSpace(storageDirectory))
            throw new ArgumentException("Storage directory cannot be null or whitespace.", nameof(storageDirectory));
        _storageDirectory = storageDirectory;
        Directory.CreateDirectory(_storageDirectory);
    }

    private string GetFilePath(string companionId)
    {
        var sanitized = string.Concat(companionId.Split(Path.GetInvalidFileNameChars()));
        return Path.Combine(_storageDirectory, $"companion-actor-{sanitized}.json");
    }

    public bool StateExists(string companionId = "default-companion")
    {
        lock (_ioLock)
        {
            return File.Exists(GetFilePath(companionId));
        }
    }

    public CompanionActorState LoadOrInitializeDefaults(string companionId = "default-companion")
    {
        lock (_ioLock)
        {
            var filePath = GetFilePath(companionId);
            if (!File.Exists(filePath))
            {
                // Initial own Actor defaults only once
                var defaults = CompanionActorState.CreateDefault(companionId);
                Save(defaults);
                return defaults;
            }

            var json = File.ReadAllText(filePath);
            var state = JsonSerializer.Deserialize<CompanionActorState>(json, JsonOptions)
                ?? CompanionActorState.CreateDefault(companionId);

            // Version migration check
            if (state.Version > CompanionActorState.CurrentSchemaVersion)
            {
                throw new InvalidOperationException(
                    $"Unsupported future actor state schema version {state.Version} (current is {CompanionActorState.CurrentSchemaVersion}).");
            }

            // CRITICAL: reload preserves stamina/water/items and never auto-resumes stale task
            state.PersistedTaskId = null;
            return state;
        }
    }

    public void Save(CompanionActorState state)
    {
        ArgumentNullException.ThrowIfNull(state);
        lock (_ioLock)
        {
            var filePath = GetFilePath(state.CompanionId);
            state.LastSavedUtc = DateTime.UtcNow.ToString("o");
            var json = JsonSerializer.Serialize(state, JsonOptions);
            File.WriteAllText(filePath, json);
        }
    }
}
