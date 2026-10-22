namespace StardewAI.Companion.Mod.Menus;

/// <summary>Cheap invalidation checks; unchanged frames never wrap or measure history text.</summary>
public sealed class CompanionDashboardRefreshState
{
    private Snapshot? _previous;

    public bool Changed(LifeMenuUiState state, CompanionTaskPanelState task, int width, int height, int tab, int scroll)
    {
        var current = new Snapshot(width, height, tab, scroll, tab == 2 ? 0 : state.ConversationRevision,
            tab == 2 && state.HasProfileState, tab == 2 ? state.ProfileRevision : 0, tab == 2 ? state.PendingProfileSetRequestId : null,
            tab == 1 && state.WorkPaused, tab == 1 && CompanionCommandMenu.AutonomyPaused, tab == 2 ? CompanionCommandMenu.AutonomyMode : "",
            tab == 2 && CompanionCommandMenu.ModeChangePending,
            tab == 1 ? task.Stage : "", tab == 1 ? task.Current : "", tab == 1 ? task.Goal : null, tab == 1 ? state.WorkGoal : null,
            tab == 1 ? task.ConnectionProblem : null, tab == 1 ? task.WaitReason : null, tab == 1 ? task.Progress : null,
            tab == 1 ? task.LastResult : null, tab == 1 ? task.NextStep : "", tab == 1 ? task.OtherGoals : Array.Empty<string>(),
            tab == 2 ? task.UsageTodayText : null, tab == 2 ? task.UsageSaveTodayText : null, tab == 2 ? task.UsageSaveTotalText : null);
        bool changed = _previous != current;
        _previous = current;
        return changed;
    }

    private readonly record struct Snapshot(int Width, int Height, int Tab, int Scroll, int ConversationRevision,
        bool HasProfileState, int ProfileRevision, string? PendingProfileRequest,
        bool Paused, bool AutonomyPaused, string AutonomyMode, bool ModeChangePending, string Stage, string Current,
        string? Goal, string? WorkGoal, string? ConnectionProblem, string? WaitReason,
        string? Progress, string? LastResult, string NextStep, IReadOnlyList<string> OtherGoals,
        string? UsageToday, string? UsageSaveToday, string? UsageSaveTotal);
}
