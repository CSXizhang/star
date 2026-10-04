using System.Reflection.Emit;
using HarmonyLib;
using StardewValley;
using StardewValley.Menus;

namespace StardewAI.Companion.Mod.Menus;

/// <summary>Only companion-owned conversation menus allow the world to keep running.</summary>
public static class CompanionMenuClock
{
    public static IClickableMenu? OwnedQuestion { get; set; }
    public static bool IsConversation(IClickableMenu? menu) => menu != null &&
        (menu is CompanionCommandMenu or CompanionLifeMenu or CompanionSpeechInputMenu or CompanionNpcDialogueBox or CompanionConversationMenu or CompanionDashboardMenu
         || ReferenceEquals(menu, OwnedQuestion));
    public static bool HasBlockingMenu => Game1.activeClickableMenu != null && !IsConversation(Game1.activeClickableMenu);

    public static void Install() => new Harmony("StardewAI.Companion.ConversationClock").Patch(
        AccessTools.Method(typeof(Game1), nameof(Game1.shouldTimePass)),
        transpiler: new HarmonyMethod(typeof(CompanionMenuClock), nameof(Transpile)));

    private static IClickableMenu? ClockMenu() => IsConversation(Game1.activeClickableMenu) ? null : Game1.activeClickableMenu;
    private static bool ClockCanMove(Farmer farmer) => farmer.CanMove || IsConversation(Game1.activeClickableMenu);

    private static IEnumerable<CodeInstruction> Transpile(IEnumerable<CodeInstruction> instructions)
    {
        var menuGetter = AccessTools.PropertyGetter(typeof(Game1), nameof(Game1.activeClickableMenu));
        var moveGetter = AccessTools.PropertyGetter(typeof(Farmer), nameof(Farmer.CanMove));
        foreach (var instruction in instructions)
        {
            if (instruction.Calls(menuGetter))
                instruction.operand = AccessTools.Method(typeof(CompanionMenuClock), nameof(ClockMenu));
            else if (instruction.Calls(moveGetter))
            {
                instruction.opcode = OpCodes.Call;
                instruction.operand = AccessTools.Method(typeof(CompanionMenuClock), nameof(ClockCanMove));
            }
            yield return instruction;
        }
    }
}
