namespace StardewAI.Companion.Mod.Navigation;

/// <summary>
/// Result of a same-map pathfinding calculation.
/// </summary>
public sealed class PathResult
{
    public bool Success { get; }
    public IReadOnlyList<PathStep> Steps { get; }
    public string? ErrorMessage { get; }
    public bool IsStuck { get; }

    private PathResult(bool success, IReadOnlyList<PathStep> steps, string? errorMessage, bool isStuck)
    {
        Success = success;
        Steps = steps;
        ErrorMessage = errorMessage;
        IsStuck = isStuck;
    }

    public static PathResult Completed(IReadOnlyList<PathStep> steps) =>
        new(true, steps, null, false);

    public static PathResult Failed(string reason, bool isStuck = false) =>
        new(false, Array.Empty<PathStep>(), reason, isStuck);
}
