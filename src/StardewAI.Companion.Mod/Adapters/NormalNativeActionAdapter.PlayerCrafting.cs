using HarmonyLib;
using StardewValley;
using StardewValley.Menus;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Adapters;

public sealed partial class NormalNativeActionAdapter
{
    private NativeActionStepResult CraftViaPlayerMenu(IFarmerActor actor, NativeActionRequest request)
    {
        var player = actor.GameFarmer;
        if (!ActorGuards.IsPlayerBound(actor) || player is null || !player.IsLocalPlayer)
            return NativeActionStepResult.Precondition("A bound local native player is required.", "player-binding-invalid");
        if (Game1.eventUp || Game1.activeClickableMenu is not null || Game1.fadeToBlack
            || Game1.newDay || player.UsingTool || player.isEating)
            return NativeActionStepResult.Precondition("Native UI or another action owns the player.", "native-ui-active");
        string name = request.ItemId ?? "";
        if (!player.craftingRecipes.ContainsKey(name) || !CraftingRecipe.craftingRecipes.ContainsKey(name))
            return NativeActionStepResult.Precondition("The native player has not learned this recipe.", "recipe-not-learned");
        var recipe = new CraftingRecipe(name, false);
        if (!recipe.doesFarmerHaveIngredientsInInventory())
            return NativeActionStepResult.Precondition("Native recipe ingredient check failed.", "missing-materials");
        var preview = recipe.createItem();
        if (preview is null || preview.Stack <= 0)
            return NativeActionStepResult.Failed("Native recipe has no output.");
        if (!CanFitAfterNativeCraft(player, recipe, preview))
            return NativeActionStepResult.Precondition("The crafted output cannot fit after native ingredient consumption. Choose an inventory action first.", "inventory-full");

        // A standalone native page, with no material containers, binds consumption
        // to this exact Game1.player inventory. Invoke only its one-recipe click:
        // receiveLeftClick's physical Shift state can otherwise craft five units.
        var page = new CraftingPage(Game1.uiViewport.Width / 2 - 400,
            Game1.uiViewport.Height / 2 - 300, 800, 600, false, true, null);
        var pages = AccessTools.Field(typeof(CraftingPage), "pagesOfCraftingRecipes")?.GetValue(page)
            as List<Dictionary<ClickableTextureComponent, CraftingRecipe>>;
        var click = AccessTools.Method(typeof(CraftingPage), "clickCraftingRecipe");
        if (pages is null || click is null)
            return NativeActionStepResult.Precondition("Native crafting page contract is unavailable.", "native-crafting-unavailable");
        int recipePage = pages.FindIndex(rows => rows.Any(row => row.Value.name == name));
        if (recipePage < 0)
            return NativeActionStepResult.Precondition("The learned recipe is absent from the native crafting page.", "recipe-unavailable");
        var icon = pages[recipePage].First(row => row.Value.name == name).Key;
        if (icon.hoverText == "ghosted")
            return NativeActionStepResult.Precondition("Native crafting page marks this recipe unavailable.", "recipe-unavailable");
        int craftedBefore = player.craftingRecipes[name];
        var before = SnapshotInventoryTotals(player);
        Game1.activeClickableMenu = page;
        try
        {
            for (int i = 0; i < recipePage; i++) page.receiveScrollWheelAction(-1);
            if (!ActorGuards.IsPlayerBound(actor) || !recipe.doesFarmerHaveIngredientsInInventory())
                return NativeActionStepResult.Precondition("Native player or recipe ingredients changed before the click.", "native-crafting-changed");
            click.Invoke(page, new object[] { icon, true });
            // The native click owns ingredient depletion, output, recipe stats,
            // quest notifications and achievements. Transfer its actual cursor
            // item using the same native inventory API as the gamepad path.
            if (page.heldItem is { } held && player.couldInventoryAcceptThisItem(held)
                && player.addItemToInventoryBool(held, false))
                page.heldItem = null;
            int gained = MeasureGainedStack(before, SnapshotInventoryTotals(player), preview.QualifiedItemId);
            int recorded = player.craftingRecipes[name] - craftedBefore;
            if (page.heldItem is not null || recorded != recipe.numberProducedPerCraft || gained != recorded)
                return NativeActionStepResult.Failed($"Native crafting ran; recorded output={recorded}, backpack delta={gained}, cursor held={page.heldItem is not null}. Reobserve; do not replay to retrieve output.");
            return NativeActionStepResult.Succeeded("crafted", itemId: preview.QualifiedItemId, itemCount: gained);
        }
        finally
        {
            // Never discard or close a crafted item still on the native cursor.
            if (ReferenceEquals(Game1.activeClickableMenu, page) && page.readyToClose()) page.exitThisMenu();
        }
    }

    private static bool CanFitAfterNativeCraft(Farmer player, CraftingRecipe recipe, Item output)
    {
        // Read-only capacity forecast in native consumeIngredients' reverse-slot
        // order. This mutates no material, cursor, output or recipe statistic.
        int slots = Math.Min(player.MaxItems, player.Items.Count);
        var remaining = player.Items.Select(item => item?.Stack ?? 0).ToArray();
        foreach (var ingredient in recipe.recipeList)
        {
            int needed = ingredient.Value;
            for (int i = remaining.Length - 1; i >= 0 && needed > 0; i--)
            {
                if (remaining[i] <= 0 || !CraftingRecipe.ItemMatchesForCrafting(player.Items[i], ingredient.Key)) continue;
                int used = Math.Min(remaining[i], needed);
                remaining[i] -= used;
                needed -= used;
            }
            if (needed > 0) return false;
        }
        int capacity = 0;
        for (int i = 0; i < slots; i++)
        {
            var item = player.Items[i];
            if (remaining[i] == 0) capacity += output.maximumStackSize();
            else if (item is not null && item.canStackWith(output)) capacity += Math.Max(0, item.maximumStackSize() - remaining[i]);
        }
        return capacity >= output.Stack;
    }
}
