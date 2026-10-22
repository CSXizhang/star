using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace StardewAI.Companion.Mod.Domain;

public sealed record CompanionConversationEntry(string Id, string Speaker, string Text, string GameDate, bool IsPlayer, bool Unread = false,
    string? DecisionId = null, string? DecisionStatus = null, string? ReplyToDecisionId = null, bool RolledBack = false);
public sealed record CompanionConversationArchive(string SaveId, List<CompanionConversationEntry> Entries);

/// <summary>Visible conversation only, isolated by save and written on receipt rather than game-day save.</summary>
public sealed class CompanionConversationStore
{
    public const int MaxEntries = 200;
    private readonly string _directory;
    public CompanionConversationStore(string directory) => _directory = directory;
    public static List<CompanionConversationEntry> Retain(IEnumerable<CompanionConversationEntry> entries)
    {
        var all = entries.ToList();
        int keepFrom = Math.Max(0, all.Count - MaxEntries);
        return all.Where((e, i) => i >= keepFrom || e.DecisionStatus == "pending").ToList();
    }
    private string PathFor(string saveId) => Path.Combine(_directory,
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(saveId))) + ".json");

    public IReadOnlyList<CompanionConversationEntry> Load(string saveId)
    {
        string path = PathFor(saveId);
        if (!File.Exists(path)) return Array.Empty<CompanionConversationEntry>();
        var archive = JsonSerializer.Deserialize<CompanionConversationArchive>(File.ReadAllText(path));
        return archive?.SaveId == saveId ? Retain(archive.Entries) : Array.Empty<CompanionConversationEntry>();
    }

    public void Save(string saveId, IEnumerable<CompanionConversationEntry> entries)
    {
        Directory.CreateDirectory(_directory);
        var current = entries.ToList();
        var updates = current.ToDictionary(e => e.Id, StringComparer.Ordinal);
        var history = LoadHistory(saveId).Select(e => updates.Remove(e.Id, out var updated) ? updated : e).ToList();
        history.AddRange(current.Where(e => updates.ContainsKey(e.Id)));
        Write(PathFor(saveId) + ".history.json", new CompanionConversationArchive(saveId, history));
        Write(PathFor(saveId), new CompanionConversationArchive(saveId, Retain(current)));
    }

    public IReadOnlyList<CompanionConversationEntry> LoadHistory(string saveId)
    {
        string path = PathFor(saveId) + ".history.json";
        if (!File.Exists(path)) return Load(saveId);
        var archive = JsonSerializer.Deserialize<CompanionConversationArchive>(File.ReadAllText(path));
        return archive?.SaveId == saveId ? archive.Entries : Array.Empty<CompanionConversationEntry>();
    }

    private static void Write(string path, CompanionConversationArchive archive)
    {
        string temporary = path + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(archive));
        File.Move(temporary, path, overwrite: true);
    }

    public string ArchiveReload(string saveId, IEnumerable<CompanionConversationEntry> entries)
    {
        string directory = Path.Combine(_directory, "reloads");
        Directory.CreateDirectory(directory);
        string path = Path.Combine(directory, Path.GetFileNameWithoutExtension(PathFor(saveId)) + "-" + Guid.NewGuid().ToString("N") + ".json");
        File.WriteAllText(path, JsonSerializer.Serialize(new CompanionConversationArchive(saveId, entries.ToList())));
        return path;
    }
}
