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

    public static void Install()
    {
        var harmony = new Harmony("StardewAI.Companion.ConversationClock");
        harmony.Patch(AccessTools.Method(typeof(Game1), nameof(Game1.shouldTimePass)),
            transpiler: new HarmonyMethod(typeof(CompanionMenuClock), nameof(Transpile)));
        harmony.Patch(AccessTools.Method(typeof(Game1), "_update"),
            transpiler: new HarmonyMethod(typeof(CompanionMenuClock), nameof(TranspileClockDispatch)));
    }

    private static IClickableMenu? ClockMenu() => IsConversation(Game1.activeClickableMenu) ? null : Game1.activeClickableMenu;
    private static bool ClockCanMove(Farmer farmer) => farmer.CanMove || IsConversation(Game1.activeClickableMenu);

    private static IEnumerable<CodeInstruction> TranspileClockDispatch(IEnumerable<CodeInstruction> instructions)
    {
        var code = instructions.ToList();
        var menuGetter = AccessTools.PropertyGetter(typeof(Game1), nameof(Game1.activeClickableMenu));
        var multiplayerGetter = AccessTools.PropertyGetter(typeof(Game1), nameof(Game1.IsMultiplayer));
        var minigameGetter = AccessTools.PropertyGetter(typeof(Game1), nameof(Game1.currentMinigame));
        var matches = 0;
        for (var i = 2; i + 2 < code.Count; i++)
        {
            // Only the menu gate immediately following the multiplayer check
            // and preceding the minigame check guards UpdateGameClock. Keep
            // actual menu update/input dispatch and all other pause gates intact.
            if (code[i].Calls(menuGetter) && code[i - 2].Calls(multiplayerGetter)
                && code[i - 1].opcode.FlowControl == FlowControl.Cond_Branch
                && code[i + 1].opcode.FlowControl == FlowControl.Cond_Branch
                && code[i + 2].Calls(minigameGetter))
            {
                code[i].operand = AccessTools.Method(typeof(CompanionMenuClock), nameof(ClockMenu));
                matches++;
            }
        }
        if (matches != 1)
            throw new InvalidOperationException($"Expected one conversation clock dispatch gate, found {matches}.");
        return code;
    }

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
