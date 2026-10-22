using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;
using StardewAI.Companion.Mod.Transport;
using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>
/// Native UI continuations (skills "native-advance" / "native-answer") and the
/// native-UI observation that feeds the snapshot's world.native section and the
/// business-skill UI guard. Every action goes through the game's own menu/event
/// APIs (skipEvent, DialogueBox click/advance paths, answerDialogueAction, ESC /
/// exitThisMenu) — never OS input, teleporting, or forced menu/event teardown.
/// </summary>
public sealed partial class CompanionMechanicsCoordinator
{
    private const int MaxDialogueTextLength = 200;

    /// <summary>Live protective-hold state, wired by ModEntry to the decision pause controller.</summary>
    public Func<ProgramHoldSnapshot?>? ProgramHoldProvider { get; set; }
    public Func<string?>? ClockPauseOwnerProvider { get; set; }

    private static Dictionary<string, object> DescribeNativeNamingInput(NamingMenu menu) => new()
    {
        ["currentText"] = menu.textBox.Text,
        ["title"] = typeof(NamingMenu).GetField("title", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Public)?.GetValue(menu) as string ?? "",
        ["minLength"] = menu.minLength,
        ["operation"] = "native_menu_select",
        ["choiceIndex"] = 0,
        ["textRequired"] = true,
        ["note"] = "Provide explicit text, then native confirmation. Native filtering and width limits remain active; closing this menu does not confirm naming."
    };

    /// <summary>Captures the native UI/world state for the snapshot. Never throws.</summary>
    private NativeUiSnapshot? CaptureNativeUi()
    {
        try
        {
            var menu = Game1.activeClickableMenu;
            bool companionConversation = Menus.CompanionMenuClock.IsConversation(menu);
            var dialogue = companionConversation ? null : menu as DialogueBox;
            string? speaker = dialogue?.characterDialogue?.speaker?.Name;
            string? text = dialogue?.getCurrentString() ?? (menu is ConfirmationDialog ? typeof(ConfirmationDialog).GetField("message", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)?.GetValue(menu) as string : null);
            if (text != null && text.Length > MaxDialogueTextLength)
                text = text.Substring(0, MaxDialogueTextLength);
            bool isQuestion = dialogue?.isQuestion == true;
            string[]? responses = isQuestion && dialogue?.responses is { } choices
                ? choices.Select(r => r.responseText).ToArray()
                : menu is ConfirmationDialog ? new[] { "Confirm", "Cancel" } : null;
            return new NativeUiSnapshot(
                EventUp: Game1.eventUp,
                EventSkippable: Game1.CurrentEvent?.skippable == true,
                EventIsFestival: Game1.CurrentEvent?.isFestival == true,
                EventAllowsMovement: ActorGuards.FestivalFreeRoam,
                DialogueUp: !companionConversation && (Game1.dialogueUp || dialogue != null),
                DialogueSpeaker: speaker,
                DialogueText: text,
                IsQuestionDialogue: isQuestion || menu is ConfirmationDialog,
                QuestionResponses: responses,
                ActiveMenu: companionConversation ? "CompanionConversation" : menu?.GetType().Name,
                PlayerCanMove: Game1.player?.CanMove == true,
                IsTimePaused: Game1.isTimePaused,
                EndOfNight: Game1.showingEndOfNightStuff || Game1.newDay,
                ProgramHold: ProgramHoldProvider?.Invoke(),
                ClockPauseOwner: ClockPauseOwnerProvider?.Invoke(),
                PlayerActionSupport: ActorGuards.IsPlayerBound(_actor)
                    ? new[] { "refill_watering_can", "clear_debris:includes-native-mine-stones", "cut_grass", "chop_tree:ordinary-wild-trees-only", "apply_fertilizer", "pickup_items", "eat_food:one-selected-native-backpack-unit", "craft_items:native-learned-recipe-click", "place_items:native-inventory-placement", "observe_player_activities", "talk_npc", "interact_tile", "fish:requires-owned-rod", "attack:requires-owned-weapon", "native_menu_select:observed-item-or-elevator-or-crafting-cursor-or-explicit-naming-text" }
                    : null,
                NamingInput: menu is NamingMenu naming ? DescribeNativeNamingInput(naming) : null);
        }
        catch
        {
            return null;
        }
    }

    /// <summary>
    /// Execution-time guard: business skills must not run while native UI owns the
    /// screen. Returns the rejection code/message describing the blocking native
    /// state and the skill that resolves it (native-advance / native-answer).
    /// </summary>
    private static bool TryDescribeNativeUiBlock(out string code, out string message, bool allowFestival = false)
    {
        code = null!;
        message = null!;
        try
        {
            if (Game1.activeClickableMenu is NamingMenu)
            {
                code = "NAME_INPUT_REQUIRED";
                message = "A native NamingMenu requires explicit text and native confirmation through native-menu-select. Observe namingInput; native-advance cannot complete or dismiss this transaction.";
                return true;
            }
            if (!Menus.CompanionMenuClock.IsConversation(Game1.activeClickableMenu)
                && Game1.activeClickableMenu is DialogueBox { isQuestion: true } nativeQuestion)
            {
                code = "CHOICE_REQUIRED";
                string[] responses = nativeQuestion.responses?.Select(r => r.responseText).ToArray() ?? Array.Empty<string>();
                message = $"A native question dialogue is open; answer it with native-answer using an explicit observed choice (responses: {string.Join(" | ", responses)}).";
                return true;
            }
            if (Game1.eventUp && !(allowFestival && ActorGuards.FestivalFreeRoam))
            {
                code = "EVENT_ACTIVE";
                bool skippable = Game1.CurrentEvent?.skippable == true;
                message = skippable
                    ? "A native cutscene/event is playing. Wait for it to finish, or advance it with native-advance (allowSkip=true) to skip it legitimately."
                    : "A native cutscene/event is playing and cannot be skipped; wait for it to finish before dispatching business skills.";
                return true;
            }
            if (!Menus.CompanionMenuClock.IsConversation(Game1.activeClickableMenu)
                && (Game1.dialogueUp || Game1.activeClickableMenu is DialogueBox))
            {
                code = "DIALOGUE_UP";
                message = "A native dialogue is open; advance it with native-advance before dispatching business skills.";
                return true;
            }
            if (Menus.CompanionMenuClock.HasBlockingMenu && Game1.activeClickableMenu is { } menu)
            {
                code = "MENU_OPEN";
                message = $"A native menu ({menu.GetType().Name}) is open; dismiss it with native-advance (where supported) before dispatching business skills.";
                return true;
            }
        }
        catch
        {
            // Headless/pre-load: fail open rather than blocking skills on a broken read.
        }
        return false;
    }

    private Dictionary<string, object> NativeUiDetails()
    {
        var details = new Dictionary<string, object>();
        var native = CaptureNativeUi();
        if (native != null) details["native"] = native;
        return details;
    }

    /// <summary>Handles skill "native-advance" (allowSkip): skip event / advance dialogue / dismiss menu.</summary>
    private bool TryAcceptNativeAdvance(SkillExecutePayload payload, EnvelopeDto envelope, out SkillResultPayload? rejectResult)
    {
        rejectResult = null;
        try
        {
            if (Game1.eventUp && Game1.activeClickableMenu is null)
            {
                if (ActorGuards.FestivalFreeRoam)
                {
                    rejectResult = BuildRejection(payload, "FESTIVAL_FREE_ROAM",
                        "The festival is in native free-roam. Observe player activities for NPCs, map actions and boundary exits. Waiting does not finish a festival; no choice has been made.", false);
                    return false;
                }
                var currentEvent = Game1.CurrentEvent;
                bool skippable = currentEvent?.skippable == true;
                if (skippable && payload.Parameters.AllowSkip)
                {
                    _actor.Halt();
                    currentEvent!.skipEvent();
                    return CompleteNativeUiSkill(payload, envelope, "skipped-event", null);
                }
                string hint = skippable
                    ? " The event is skippable: use native-advance with allowSkip=true to skip it legitimately."
                    : " This event cannot be skipped; wait for it to finish.";
                rejectResult = BuildRejection(payload, "EVENT_ACTIVE", $"A native cutscene/event is playing.{hint}", retryable: true);
                return false;
            }

            var menu = Game1.activeClickableMenu;
            if (Menus.CompanionMenuClock.IsConversation(menu))
            {
                rejectResult = BuildRejection(payload, "PLAYER_CONVERSATION_OPEN",
                    "The player is using companion chat. Keep it open; it does not block companion farm work and requires no native-advance.", false);
                return false;
            }
            if (menu is NamingMenu naming)
            {
                rejectResult = BuildRejection(payload, "NAME_INPUT_REQUIRED",
                    "Naming requires explicit text and native confirmation through native-menu-select. Closing NamingMenu does not complete naming; no close input was sent.", false,
                    extraDetails: new Dictionary<string, object> { ["namingInput"] = DescribeNativeNamingInput(naming) });
                return false;
            }
            if (menu is ShippingMenu or LevelUpMenu or SaveGameMenu)
            {
                rejectResult = BuildRejection(payload, "MENU_OWNED_BY_SLEEP_FLOW",
                    $"The native {menu.GetType().Name} belongs to the overnight sleep flow and is owned by the sleep skill; do not advance it here.", retryable: false);
                return false;
            }

            if (menu is ConfirmationDialog)
            {
                rejectResult = BuildRejection(payload, "CHOICE_REQUIRED",
                    "A native confirmation is open. Use native-answer with explicit choiceIndex 0 (confirm) or 1 (cancel).", false);
                return false;
            }
            if (menu is DialogueBox { isQuestion: true } question)
            {
                string[] responses = question.responses?.Select(r => r.responseText).ToArray() ?? Array.Empty<string>();
                rejectResult = BuildRejection(payload, "CHOICE_REQUIRED",
                    $"A native question dialogue is open; it requires an explicit choice via native-answer (responses: {string.Join(" | ", responses)}).", retryable: false,
                    extraDetails: new Dictionary<string, object> { ["questionResponses"] = responses });
                return false;
            }

            if (menu is DialogueBox box)
            {
                _actor.Halt();
                box.receiveLeftClick(box.x + box.width / 2, box.y + box.height / 2, true);
                string? newText = (Game1.activeClickableMenu as DialogueBox)?.getCurrentString();
                if (newText != null && newText.Length > MaxDialogueTextLength)
                    newText = newText.Substring(0, MaxDialogueTextLength);
                return CompleteNativeUiSkill(payload, envelope, "advanced-dialogue",
                    new Dictionary<string, object> { ["dialogueText"] = newText ?? "" });
            }

            if (menu != null)
            {
                if (!menu.readyToClose())
                {
                    rejectResult = BuildRejection(payload, "MENU_NOT_READY_TO_CLOSE",
                        "The native menu is not ready to close; inspect its current state and finish the pending interaction first.", retryable: true);
                    return false;
                }
                _actor.Halt();
                string menuType = menu.GetType().Name;
                // Native dismissal semantics: ESC first (the player's own close path),
                // then exitThisMenu only if the menu survived its ESC handler.
                menu.receiveKeyPress(Keys.Escape);
                if (ReferenceEquals(Game1.activeClickableMenu, menu) && menu.readyToClose()) menu.exitThisMenu();
                if (ReferenceEquals(Game1.activeClickableMenu, menu))
                {
                    rejectResult = BuildRejection(payload, "MENU_NOT_READY_TO_CLOSE",
                        "The native menu remained open after its close input; no dismissal is confirmed.", retryable: true);
                    return false;
                }
                return CompleteNativeUiSkill(payload, envelope, "dismissed-menu",
                    new Dictionary<string, object> { ["menuType"] = menuType });
            }

            rejectResult = BuildRejection(payload, "NOTHING_TO_ADVANCE",
                "No native event, dialogue or menu is active; there is nothing to advance.", retryable: false);
            return false;
        }
        catch (Exception ex)
        {
            rejectResult = BuildRejection(payload, "NATIVE_ADVANCE_FAILED", $"native-advance failed: {ex.Message}", retryable: true);
            return false;
        }
    }

    /// <summary>Handles skill "native-answer" (choiceIndex): answer a question dialogue natively.</summary>
    private bool TryAcceptNativeAnswer(SkillExecutePayload payload, EnvelopeDto envelope, out SkillResultPayload? rejectResult)
    {
        rejectResult = null;
        if (Menus.CompanionMenuClock.IsConversation(Game1.activeClickableMenu))
        {
            rejectResult = BuildRejection(payload, "PLAYER_CONVERSATION_OPEN",
                "Companion chat choices belong to the player. Continue companion work without answering or closing their conversation.", false);
            return false;
        }
        if (payload.Parameters.ChoiceIndex is not { } choiceIndex)
        {
            rejectResult = BuildRejection(payload, "INVALID_PARAMETERS",
                "parameters.choiceIndex is a required explicit parameter for native-answer; the mod never chooses on its own.", retryable: false);
            return false;
        }

        try
        {
            if (Game1.activeClickableMenu is ConfirmationDialog confirmation)
            {
                if (choiceIndex is < 0 or > 1)
                { rejectResult = BuildRejection(payload, "CHOICE_OUT_OF_RANGE", "Use observed confirmation choice 0 or 1.", false); return false; }
                var button = choiceIndex == 0 ? confirmation.okButton : confirmation.cancelButton;
                _actor.Halt();
                confirmation.receiveLeftClick(button.bounds.Center.X, button.bounds.Center.Y);
                if (ReferenceEquals(Game1.activeClickableMenu, confirmation))
                { rejectResult = BuildRejection(payload, "MENU_NOT_READY", "Native confirmation did not close; wait for its input delay then observe again.", true); return false; }
                return CompleteNativeUiSkill(payload, envelope, "answered-confirmation",
                    new Dictionary<string, object> { ["choiceIndex"] = choiceIndex });
            }
            if (Game1.activeClickableMenu is not DialogueBox { isQuestion: true } question)
            {
                rejectResult = BuildRejection(payload, "NOT_A_QUESTION",
                    "No native question dialogue is open; native-answer only answers explicit question dialogues.", retryable: false);
                return false;
            }

            var responses = question.responses ?? Array.Empty<Response>();
            if (choiceIndex < 0 || choiceIndex >= responses.Length)
            {
                string[] labels = responses.Select(r => r.responseText).ToArray();
                rejectResult = BuildRejection(payload, "CHOICE_OUT_OF_RANGE",
                    $"choiceIndex {choiceIndex} is out of range for the {responses.Length} response(s) currently offered: {string.Join(" | ", labels)}.", retryable: false,
                    extraDetails: new Dictionary<string, object> { ["questionResponses"] = labels });
                return false;
            }

            var choice = responses[choiceIndex];
            string questionKey = Game1.currentLocation?.lastQuestionKey?.Split(' ')[0] ?? "";
            if (Game1.eventUp || question.characterDialogue != null)
            {
                _actor.Halt();
                question.selectedResponse = choiceIndex;
                question.receiveLeftClick(question.x + question.width / 2, question.y + question.height / 2, true);
                // A first click can merely finish typing; never report an answer
                // until the native handler starts transitioning or replaces UI.
                if (ReferenceEquals(Game1.activeClickableMenu, question) && !question.transitioning)
                { rejectResult = BuildRejection(payload, "QUESTION_NOT_READY", "Question text/input is still settling; the answer is not confirmed. Observe again before retrying.", true); return false; }
                return CompleteNativeUiSkill(payload, envelope, "answered", new Dictionary<string, object>
                { ["choiceIndex"] = choiceIndex, ["answerText"] = choice.responseText });
            }
            if (string.IsNullOrEmpty(questionKey))
            {
                rejectResult = BuildRejection(payload, "QUESTION_KEY_UNKNOWN",
                    "The native question key could not be resolved; answer this dialogue manually.", retryable: false);
                return false;
            }

            // A native Sleep_Yes answer is an overnight action, not an immediate
            // UI acknowledgement. Keep this exact command live through native save
            // and morning so it emits the same verified sleep receipt as sleep.
            if (questionKey == "Sleep" && choice.responseKey == "Yes" && ActorGuards.IsPlayerBound(_actor))
            {
                if (!TryCheckIdle(out var busy)) { rejectResult = BuildRejection(payload, "ACTOR_BUSY", busy!, true); return false; }
                _sleepMachine ??= new PlayerSleepStateMachine(_actor, _observer, new NavigationStateMachine(_actor, _observer, _navigator, _mapGraph, null, _log), log: _log);
                if (!_sleepCompletionBound) { _sleepMachine.OnCompleted += r => CompleteExecution(r.ToTransportPayload() with { SkillId = _sleepSkillId }); _sleepCompletionBound = true; }
                _sleepSkillId = payload.SkillId;
                _currentExecuteEnvelope = envelope;
                _currentExecuteCommandId = payload.CommandId;
                var started = _sleepMachine.Start(new PlayerSleepRequest(payload.CommandId, payload.TaskId, envelope.IdempotencyKey, payload.ExpectedWorldRevision, payload.Budgets.MaxGameMinutes, payload.CancelPolicy), out var error, answerExistingQuestion: true);
                rejectResult = error is null ? null : error.ToTransportPayload() with { SkillId = payload.SkillId };
                return started;
            }
            _actor.Halt();
            bool accepted = Game1.currentLocation!.answerDialogueAction($"{questionKey}_{choice.responseKey}", null);
            if (!accepted)
            {
                rejectResult = BuildRejection(payload, "ANSWER_REJECTED",
                    $"The native dialogue handler rejected '{questionKey}_{choice.responseKey}'.", retryable: false);
                return false;
            }
            // The normal DialogueBox response path also cleans up its question; only
            // close that exact menu, never a newly opened overnight screen.
            if (ReferenceEquals(Game1.activeClickableMenu, question))
                question.closeDialogue();

            return CompleteNativeUiSkill(payload, envelope, "answered", new Dictionary<string, object>
            {
                ["choiceIndex"] = choiceIndex,
                ["answerText"] = choice.responseText,
            });
        }
        catch (Exception ex)
        {
            rejectResult = BuildRejection(payload, "NATIVE_ANSWER_FAILED", $"native-answer failed: {ex.Message}", retryable: true);
            return false;
        }
    }

    private bool CompleteNativeUiSkill(SkillExecutePayload payload, EnvelopeDto envelope, string action, Dictionary<string, object>? extraDetails)
    {
        var details = NativeUiDetails();
        details["action"] = action;
        if (extraDetails != null)
            foreach (var pair in extraDetails)
                details[pair.Key] = pair.Value ?? "";

        var result = new SkillResultPayload(payload.CommandId, payload.TaskId, "succeeded", 1, 0, 0,
            _observer.WorldRevision, new List<Dictionary<string, object>>(),
            new SkillResultResources(0, 0, 0), SkillId: payload.SkillId, Details: details);
        _lastSkillResultPayload = result;
        _currentExecuteEnvelope = envelope;
        _currentExecuteCommandId = payload.CommandId;
        CompleteExecution(result);
        Log($"{payload.SkillId}: {action} (commandId={payload.CommandId}).");
        return true;
    }
}
