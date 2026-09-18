namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// Repository interface for versioned actor state persistence.
/// </summary>
public interface IActorStateRepository
{
    /// <summary>
    /// Loads the persisted state if present, or initializes default state exactly once.
    /// </summary>
    CompanionActorState LoadOrInitializeDefaults(string companionId = "default-companion");

    /// <summary>
    /// Saves the current actor state.
    /// </summary>
    void Save(CompanionActorState state);

    /// <summary>
    /// Checks if a persisted state exists for the companion.
    /// </summary>
    bool StateExists(string companionId = "default-companion");
}
