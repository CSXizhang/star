using StardewModdingAPI;
using StardewValley;

namespace StardewAI.Companion.Mod.PlayerMode;

/// <summary>
/// Per-save record of what the native player legitimately knows in player mode:
/// maps they have actually visited and chests they have actually opened. This is
/// the knowledge substrate for <see cref="PlayerKnownObserver"/>; integration
/// wires Player.Warped to <see cref="NoteLocationVisited"/>, Display.MenuChanged
/// (ItemGrabMenu with Chest context) and successful player-mode chest skills to
/// <see cref="NoteChestObserved"/>, and persists via <see cref="SaveIfDirty"/>.
///
/// Threading: this type is only safe to call from the game thread (like the rest
/// of the observation surface). It performs no locking; SMAPI events fire on the
/// game thread and save events are raised synchronously on it.
///
/// Seeding choice: when a save has no stored knowledge yet (first player-mode
/// load of an existing or new save), the tracker seeds "Farm", "FarmHouse" and
/// the player's current location. An existing save's owner legitimately knows
/// their own farm and home, and knowing the map they are standing on is
/// unavoidable. The seed is marked dirty so the next save persists it and later
/// loads do not re-derive it.
/// </summary>
public sealed class PlayerKnowledgeTracker
{
    private const string SaveKey = "player-knowledge-v1";

    /// <summary>Location names (NameOrUniqueName) the player has actually visited.</summary>
    public HashSet<string> VisitedLocations { get; } = new(StringComparer.OrdinalIgnoreCase);

    /// <summary>Chest keys (<see cref="ChestKey"/> format) the player has actually opened.</summary>
    public HashSet<string> ObservedChests { get; } = new(StringComparer.OrdinalIgnoreCase);

    /// <summary>True when a Note* method changed state since the last <see cref="Save"/>.</summary>
    public bool Dirty { get; private set; }

    public void Load(IDataHelper data)
    {
        Dto? dto = null;
        try
        {
            dto = data.ReadSaveData<Dto>(SaveKey);
        }
        catch
        {
            // Corrupt auxiliary data must never break a save load; fall through to the seed.
        }

        VisitedLocations.Clear();
        ObservedChests.Clear();

        if (dto is { Version: 1 })
        {
            if (dto.VisitedLocations != null)
            {
                foreach (string name in dto.VisitedLocations)
                    if (!string.IsNullOrWhiteSpace(name)) VisitedLocations.Add(name);
            }
            if (dto.ObservedChests != null)
            {
                foreach (string key in dto.ObservedChests)
                    if (!string.IsNullOrWhiteSpace(key)) ObservedChests.Add(key);
            }
            Dirty = false;
            return;
        }

        // No prior knowledge stored: seed the maps an existing save's owner legitimately knows.
        // Note* marks the tracker dirty so the seed is persisted by the next SaveIfDirty.
        NoteLocationVisited("Farm");
        NoteLocationVisited("FarmHouse");
        if (!string.IsNullOrWhiteSpace(Game1.currentLocation?.NameOrUniqueName))
            NoteLocationVisited(Game1.currentLocation.NameOrUniqueName);
    }

    public void Save(IDataHelper data)
    {
        var dto = new Dto
        {
            Version = 1,
            VisitedLocations = VisitedLocations.OrderBy(n => n, StringComparer.Ordinal).ToList(),
            ObservedChests = ObservedChests.OrderBy(k => k, StringComparer.Ordinal).ToList(),
        };
        data.WriteSaveData(SaveKey, dto);
        Dirty = false;
    }

    public void SaveIfDirty(IDataHelper data)
    {
        if (Dirty) Save(data);
    }

    public void NoteLocationVisited(string locationName)
    {
        if (string.IsNullOrWhiteSpace(locationName)) return;
        if (VisitedLocations.Add(locationName)) Dirty = true;
    }

    public bool HasVisited(string? locationName) =>
        !string.IsNullOrWhiteSpace(locationName) && VisitedLocations.Contains(locationName);

    public void NoteChestObserved(string locationName, int x, int y)
    {
        if (string.IsNullOrWhiteSpace(locationName)) return;
        if (ObservedChests.Add(ChestKey(locationName, x, y))) Dirty = true;
    }

    public bool HasObservedChest(string locationName, int x, int y) =>
        !string.IsNullOrWhiteSpace(locationName) && ObservedChests.Contains(ChestKey(locationName, x, y));

    public static string ChestKey(string locationName, int x, int y) => $"{locationName}:{x}:{y}";

    private sealed class Dto
    {
        public int Version { get; set; } = 1;
        public List<string>? VisitedLocations { get; set; }
        public List<string>? ObservedChests { get; set; }
    }
}
