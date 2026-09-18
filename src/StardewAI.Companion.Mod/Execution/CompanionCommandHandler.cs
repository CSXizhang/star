using System.Text;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>
/// Supported command types for in-game text command input.
/// </summary>
public enum InGameCommandType
{
    Water,
    Pause,
    Resume,
    Cancel,
    Status
}

/// <summary>
/// Strongly-typed representation of a parsed in-game companion command.
/// </summary>
public sealed record ParsedInGameCommand(
    InGameCommandType Type,
    int X = 0,
    int Y = 0,
    int Radius = 0
);

/// <summary>
/// Helper for parsing console commands and in-game text commands, discovering unwatered dirt targets in radius,
/// and formatting status lines and detailed status outputs.
/// Pure logic class suitable for full unit testing without game dependencies.
/// </summary>
public static class CompanionCommandHandler
{
    public const int MaxRadius = 2;

    /// <summary>
    /// Parses a single raw text command entered by the player in the in-game text input menu.
    /// Supports English and Chinese command verbs and aliases:
    ///   water <x> <y> [radius 0-2] / 浇水 <x> <y> [radius]
    ///   pause / 暂停
    ///   resume / 继续
    ///   cancel / 取消
    ///   status / 状态
    /// </summary>
    public static bool TryParseInGameCommand(
        string? input,
        out ParsedInGameCommand? command,
        out string? errorMessage)
    {
        command = null;

        if (string.IsNullOrWhiteSpace(input))
        {
            errorMessage = "Command cannot be empty. Available: water/浇水, pause/暂停, resume/继续, cancel/取消, status/状态";
            return false;
        }

        string[] tokens = input.Trim().Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        if (tokens.Length == 0)
        {
            errorMessage = "Command cannot be empty. Available: water/浇水, pause/暂停, resume/继续, cancel/取消, status/状态";
            return false;
        }

        string verb = tokens[0].ToLowerInvariant();

        switch (verb)
        {
            case "water":
            case "浇水":
                if (tokens.Length < 3)
                {
                    errorMessage = "Usage: water <x> <y> [radius (0-2)] (or 浇水 <x> <y> [radius])";
                    return false;
                }

                if (!int.TryParse(tokens[1], out int x) || x < 0)
                {
                    errorMessage = "Invalid x coordinate: must be a non-negative integer.";
                    return false;
                }

                if (!int.TryParse(tokens[2], out int y) || y < 0)
                {
                    errorMessage = "Invalid y coordinate: must be a non-negative integer.";
                    return false;
                }

                int radius = 0;
                if (tokens.Length >= 4)
                {
                    if (!int.TryParse(tokens[3], out radius) || radius < 0 || radius > MaxRadius)
                    {
                        errorMessage = $"Invalid radius '{tokens[3]}': radius must be an integer between 0 and {MaxRadius} (default 0).";
                        return false;
                    }
                }

                command = new ParsedInGameCommand(InGameCommandType.Water, x, y, radius);
                errorMessage = null;
                return true;

            case "pause":
            case "暂停":
                command = new ParsedInGameCommand(InGameCommandType.Pause);
                errorMessage = null;
                return true;

            case "resume":
            case "继续":
                command = new ParsedInGameCommand(InGameCommandType.Resume);
                errorMessage = null;
                return true;

            case "cancel":
            case "取消":
                command = new ParsedInGameCommand(InGameCommandType.Cancel);
                errorMessage = null;
                return true;

            case "status":
            case "状态":
                command = new ParsedInGameCommand(InGameCommandType.Status);
                errorMessage = null;
                return true;

            default:
                errorMessage = $"Unknown command '{tokens[0]}'. Available: water/浇水, pause/暂停, resume/继续, cancel/取消, status/状态.";
                return false;
        }
    }

    /// <summary>
    /// Parses arguments for the ai_water command: &lt;x&gt; &lt;y&gt; [radius].
    /// </summary>
    public static bool ParseWaterCommand(
        string[]? args,
        out int x,
        out int y,
        out int radius,
        out string? errorMessage)
    {
        x = 0;
        y = 0;
        radius = 0;

        if (args == null || args.Length < 2)
        {
            errorMessage = "Usage: ai_water <x> <y> [radius (0-2, default 0)]";
            return false;
        }

        if (!int.TryParse(args[0], out x) || x < 0)
        {
            errorMessage = "Invalid x coordinate: must be a non-negative integer.";
            return false;
        }

        if (!int.TryParse(args[1], out y) || y < 0)
        {
            errorMessage = "Invalid y coordinate: must be a non-negative integer.";
            return false;
        }

        if (args.Length >= 3)
        {
            if (!int.TryParse(args[2], out radius) || radius < 0 || radius > MaxRadius)
            {
                errorMessage = $"Invalid radius '{args[2]}': radius must be an integer between 0 and {MaxRadius} (default 0).";
                return false;
            }
        }

        errorMessage = null;
        return true;
    }

    /// <summary>
    /// Finds all tilled unwatered tiles centered at (centerX, centerY) within the specified radius.
    /// Filters out untilled ground, obstacles, and already-watered tiles.
    /// </summary>
    public static List<TileCoordinate> FindTilledUnwateredTiles(
        IWorldObserver observer,
        string locationName,
        int centerX,
        int centerY,
        int radius)
    {
        ArgumentNullException.ThrowIfNull(observer);
        if (string.IsNullOrEmpty(locationName))
        {
            throw new ArgumentException("Location name cannot be null or empty.", nameof(locationName));
        }

        radius = Math.Clamp(radius, 0, MaxRadius);
        var matchingTiles = new List<TileCoordinate>();

        for (int dy = -radius; dy <= radius; dy++)
        {
            for (int dx = -radius; dx <= radius; dx++)
            {
                int tx = centerX + dx;
                int ty = centerY + dy;
                if (tx < 0 || ty < 0) continue;

                var tile = new TileCoordinate(tx, ty);
                var dirt = observer.GetDirtState(locationName, tile);
                if (dirt.IsTilled && dirt.HasCrop && !dirt.IsWatered)
                {
                    matchingTiles.Add(tile);
                }
            }
        }

        return matchingTiles;
    }

    /// <summary>
    /// Formats a single-line status text for in-game HUD rendering (e.g. "AI: watering 1/3", "AI: paused", "AI: idle").
    /// </summary>
    public static string FormatStatusLine(
        bool isExecuting,
        bool isPaused,
        int currentTargetIndex,
        int totalTargets)
    {
        if (!isExecuting)
            return "AI: idle";

        if (isPaused)
            return "AI: paused";

        if (totalTargets > 0)
        {
            int currentNumber = Math.Clamp(currentTargetIndex + 1, 1, totalTargets);
            return $"AI: watering {currentNumber}/{totalTargets}";
        }

        return "AI: watering";
    }

    /// <summary>
    /// Formats comprehensive multi-line companion status for console display.
    /// </summary>
    public static string FormatStatusDetails(
        IFarmerActor actor,
        WaterZoneStateMachine? stateMachine)
    {
        ArgumentNullException.ThrowIfNull(actor);

        var sb = new StringBuilder();
        sb.AppendLine("[Companion Status]");
        sb.AppendLine($"  Position:  {actor.LocationName} ({actor.Tile.X}, {actor.Tile.Y}), Facing: {actor.Facing}");
        sb.AppendLine($"  Resources: Stamina: {actor.Stamina:F1}/{actor.MaxStamina:F1}, Water: {actor.WaterLeft}/{actor.MaxWater}");

        bool isExecuting = stateMachine?.IsExecuting == true;
        bool isPaused = stateMachine?.IsPaused == true;
        string taskId = actor.ActiveTaskId ?? stateMachine?.CurrentRequest?.TaskId ?? "none";

        if (isExecuting)
        {
            int total = stateMachine?.TotalTargets ?? 0;
            int watered = stateMachine?.WateredCount ?? 0;
            int skipped = stateMachine?.SkippedCount ?? 0;
            int failed = stateMachine?.FailedCount ?? 0;
            sb.AppendLine($"  Task:      {taskId} ({stateMachine?.CurrentState})");
            sb.AppendLine($"  Progress:  {watered}/{total} watered ({skipped} skipped, {failed} failed)");
            sb.AppendLine($"  Paused:    {isPaused}");
        }
        else
        {
            sb.AppendLine("  Task:      none (idle)");
            sb.AppendLine("  Progress:  idle");
            sb.AppendLine("  Paused:    False");
        }

        return sb.ToString().TrimEnd();
    }
}

