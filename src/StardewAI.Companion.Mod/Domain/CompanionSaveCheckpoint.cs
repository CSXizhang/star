using System.Text.Json;

namespace StardewAI.Companion.Mod.Domain;

/// <summary>Stored inside the real game save, so abandoning a day abandons its conversation and work facts.</summary>
public sealed class CompanionSaveCheckpoint
{
    public string SaveId { get; set; } = "";
    public List<CompanionConversationEntry> Conversation { get; set; } = new();
    public CompanionActorState? Actor { get; set; }
    public Dictionary<string, string?> RuntimePartitions { get; set; } = new();

    public static readonly string[] RuntimeFiles =
    {
        "work-state.json", "autonomy-state.json", "companion-profile.json",
        "companion-memory.json", "companion-care.json", "companion-milestones.json",
    };

    public static Dictionary<string, string?> CaptureRuntime(string modDirectory, string saveId)
    {
        var result = new Dictionary<string, string?>();
        foreach (string name in RuntimeFiles)
        {
            string path = Path.Combine(modDirectory, "data", name);
            if (!File.Exists(path)) { result[name] = null; continue; }
            using var document = JsonDocument.Parse(File.ReadAllText(path));
            result[name] = document.RootElement.TryGetProperty(saveId, out var record) ? record.GetRawText() : null;
        }
        return result;
    }

    public static IEnumerable<CompanionConversationEntry> LegacyHistoryBeforeDay(
        IEnumerable<CompanionConversationEntry> entries, string loadedDay) => entries.Where(entry =>
            DayNumber(entry.GameDate) is { } date && DayNumber(loadedDay) is { } today && date < today);

    private static int? DayNumber(string date)
    {
        string[] parts = date.Split(':');
        if (parts.Length != 3 || !int.TryParse(parts[0], out int year) || year < 1 ||
            !int.TryParse(parts[2], out int day) || day is < 1 or > 28) return null;
        int season = Array.IndexOf(new[] { "spring", "summer", "fall", "winter" }, parts[1].ToLowerInvariant());
        return season < 0 ? null : (year - 1) * 112 + season * 28 + day;
    }

    public static void PublishLoad(string modDirectory, string saveId, string gameSessionId,
        string gameDate, CompanionSaveCheckpoint? checkpoint, IEnumerable<CompanionConversationEntry> priorConversation,
        string? conversationArchive)
    {
        string path = Path.Combine(modDirectory, "data", "game-load.json");
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        string temporary = path + ".tmp";
        var request = new { saveId, gameSessionId, gameDate,
            runtimePartitions = checkpoint?.RuntimePartitions,
            reloadSummary = BuildReloadSummary(priorConversation, gameDate, conversationArchive) };
        File.WriteAllText(temporary, JsonSerializer.Serialize(request));
        File.Move(temporary, path, overwrite: true);
    }

    public static object BuildReloadSummary(IEnumerable<CompanionConversationEntry> entries, string loadedDay, string? archive)
    {
        var history = entries.ToList();
        // Durable structured intent is supplied by the runtime's live stores.
        // Quotations are not a substitute for summarization; the original remains
        // available in full, including alternatives and the player's reasons.
        return new
        {
            loadedGameDate = loadedDay,
            preservedPlayerMessageCount = history.Count(e => e.IsPlayer),
            preservedMessageCount = history.Count,
            historyTool = "read_reload_history",
            fullConversationArchive = archive,
            priorExecutionResultsAreCurrentFacts = false,
            previousDecisionAnswersApply = true,
        };
    }

    public static List<CompanionConversationEntry> RestoreWithReloadHistory(
        IEnumerable<CompanionConversationEntry> committed, IEnumerable<CompanionConversationEntry> previous)
    {
        var saved = committed.ToList();
        var recent = previous.ToList();
        var latest = recent.ToDictionary(e => e.Id, StringComparer.Ordinal);
        var savedIds = saved.Select(e => e.Id).ToHashSet(StringComparer.Ordinal);
        var restored = saved.Select(e => latest.TryGetValue(e.Id, out var update) ? update : e).ToList();
        restored.AddRange(recent.Where(e => !savedIds.Contains(e.Id)).Select(e => e with
        { RolledBack = true, Unread = false }));
        return CompanionConversationStore.Retain(restored);
    }
}
