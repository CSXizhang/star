using System.Text;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>First-meeting decisions, separate from the native menu and transport.</summary>
public static class CompanionFirstMeeting
{
    public static bool NeedsMeeting(LifeMenuUiState state) =>
        state.HasProfileState && !state.IsOnboarded && !state.IsSkipped;

    // An explicitly saved legacy profile already has a chosen name and personality.
    public static bool NeedsName(LifeMenuUiState state) => NeedsMeeting(state) && state.ProfileRevision == 0;

    public static string? NormalizeName(string value)
    {
        string name = value.Trim();
        int length = name.EnumerateRunes().Count();
        return length is >= 1 and <= 12 ? name : null;
    }

    public static LifeProfilePatchDto Patch(string? name, string choice) => new(
        Onboarded: choice == "later" ? null : true,
        Skipped: choice == "later",
        CompanionName: name,
        CareFrequency: choice == "chat" ? "chatty" : null);

    public static string Preference(string choice) => choice switch
    {
        "help" => "初次见面时，玩家希望我多帮忙：用现有资源照料农场；本次先做一件能做的农活，不采购。以后按当轮意愿、暂停和资源保护来安排。",
        "chat" => "初次见面时，玩家希望我多聊聊，分享农场近况；这不代表授权派工或采购。",
        _ => "初次见面时，玩家希望先相处看看，暂不增加主动工作；之后以玩家当轮意愿为准。",
    };
}
