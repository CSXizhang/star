using HarmonyLib;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

/// <summary>
/// One model-selected food through the real player's eatHeldObject lifecycle.
/// Native inventory depletion, animation, buffs and recovery remain native.
/// A read-only completion hook confirms doneEating for this exact player/food;
/// a depleted stack alone is not evidence that recovery finished.
/// </summary>
public sealed class PlayerEatingMachine : ISkillExecutionMachine
{
    private readonly PlayerActor _actor;
    private readonly Func<bool> _owns;
    private readonly Func<int> _revision;
    private SkillExecutePayload? _request;
    private StardewValley.Object? _food;
    private string _itemId = "";
    private int _quality;
    private int _slot;
    private int _quantityBefore;
    private int _quantityAfterStart;
    private int _freeSlotsBefore;
    private float _staminaBefore;
    private int _healthBefore;
    private DateTime _deadline;
    private bool _completionObserved;
    private string? _cancelReason;
    private static PlayerEatingMachine? _completionOwner;
    private static bool _installed;

    public PlayerEatingMachine(PlayerActor actor, Func<bool> owns, Func<int> revision)
    {
        _actor = actor;
        _owns = owns;
        _revision = revision;
        if (!_installed)
        {
            new Harmony("StardewAI.NativePlayerEatingReceipt").Patch(
                AccessTools.Method(typeof(Farmer), nameof(Farmer.doneEating)),
                postfix: new HarmonyMethod(typeof(PlayerEatingMachine), nameof(ObserveDoneEating)));
            _installed = true;
        }
    }

    public event Action<SkillResultPayload>? Completed;
    public bool IsExecuting => _request is not null;
    public bool IsPaused { get; private set; }
    public int TotalTargets => 1;
    public int CurrentTargetIndex => 0;
    public string? ActiveTaskId => _request?.TaskId;

    private static void ObserveDoneEating(Farmer __instance)
    {
        var owner = _completionOwner;
        if (owner?._request is not null && owner._actor.BindingValid
            && ReferenceEquals(__instance, owner._actor.GameFarmer)
            && ReferenceEquals(__instance.itemToEat, owner._food)
            && __instance.IsLocalPlayer && !__instance.isEating)
            owner._completionObserved = true;
    }

    public bool Start(SkillExecutePayload payload, out SkillResultPayload? reject)
    {
        reject = null;
        var farmer = _actor.GameFarmer;
        string? error = !_actor.BindingValid || !_owns() || farmer is null
            ? "A valid controlled native player is required."
            : IsExecuting || _completionOwner?.IsExecuting == true || farmer.isEating || farmer.UsingTool
                ? "Native player is already performing an action."
                : !string.Equals(payload.Parameters.LocationId, _actor.LocationName, StringComparison.OrdinalIgnoreCase)
                    ? "Navigate to the requested food consumption map first."
                    : string.IsNullOrWhiteSpace(payload.Parameters.ItemId) || (payload.Parameters.ItemCount ?? 1) != 1
                        ? "Eating requires an observed itemId and exactly one inventory unit."
                        : Game1.eventUp || Game1.activeClickableMenu is not null || Game1.fadeToBlack
                            || Game1.newDay || Game1.showingEndOfNightStuff || !farmer.CanMove
                            ? "Native UI, a transition or another player action currently owns the player."
                            : null;
        if (error is null)
        {
            // Item identity alone selects the first matching backpack stack,
            // as the existing primitive does. Its exact quality/slot are reported.
            _food = farmer!.Items.OfType<StardewValley.Object>().FirstOrDefault(item => item.Stack > 0
                && (item.QualifiedItemId == payload.Parameters.ItemId || item.ItemId == payload.Parameters.ItemId));
            error = _food is null ? "The requested food is no longer in the native player backpack."
                : _food.Edibility == -300 || _food.QualifiedItemId == "(O)434"
                    ? "Select ordinary edible food; non-food and permanent Stardrop effects are outside this action."
                    : null;
        }
        if (error is not null)
        {
            reject = new(payload.CommandId, payload.TaskId, "rejected", 0, 0, 0, _revision(), new(),
                Error: new("FOOD_PRECONDITION", error, null, false), SkillId: "eat-food");
            return false;
        }

        _itemId = _food!.QualifiedItemId;
        _quality = _food.Quality;
        _slot = farmer!.Items.IndexOf(_food);
        _quantityBefore = Quantity(farmer);
        _quantityAfterStart = _quantityBefore;
        _freeSlotsBefore = _actor.FreeInventorySlots;
        _staminaBefore = farmer.Stamina;
        _healthBefore = farmer.health;
        _completionObserved = false;
        _cancelReason = null;
        IsPaused = false;
        _deadline = DateTime.UtcNow.AddSeconds(15);
        _request = payload;
        _completionOwner = this;
        _actor.Halt();
        _actor.SetActiveTask(payload.TaskId);
        try
        {
            // eatHeldObject can otherwise temporarily substitute the last grabbed
            // item into the equipped slot. Equip the actual selected stack and
            // bind that same reference, just as the native food confirmation does.
            farmer.CurrentToolIndex = _slot;
            farmer.mostRecentlyGrabbedItem = _food;
            farmer.eatHeldObject();
            _quantityAfterStart = Quantity(farmer);
            if (!farmer.isEating)
                Finish("failed", "FOOD_NATIVE_DECLINED", "Native food/drink rules declined consumption; inspect the actual inventory receipt.");
        }
        catch (Exception ex)
        {
            _quantityAfterStart = Quantity(farmer);
            Finish("partially-succeeded", "FOOD_SETTLEMENT_UNCONFIRMED", $"Native consumption did not settle: {ex.Message}");
        }
        // Once the native call is issued, even an immediate failure is a terminal
        // receipt through Completed, not a second dispatch or an inventory rollback.
        return true;
    }

    private int Quantity(Farmer farmer) => farmer.Items.Where(item => item is not null
        && item.QualifiedItemId == _itemId && item.Quality == _quality).Sum(item => item.Stack);

    public void Update(GameTime? time, long tickCount)
    {
        if (_request is null) return;
        if (!_actor.BindingValid)
        {
            Finish("partially-succeeded", "FOOD_SETTLEMENT_UNCONFIRMED", "Player binding changed after native consumption started; no substitute player was inspected.");
            return;
        }
        if (!_owns()) _cancelReason ??= "Player authority ended after native consumption started.";
        var farmer = _actor.GameFarmer!;
        // Pausing/cancelling cannot undo a committed native food unit. Continue
        // observation only until its native completion, without issuing an action.
        if (_completionObserved && !farmer.isEating && farmer.CanMove)
        {
            if (_quantityBefore - Quantity(farmer) == 1)
                Finish("succeeded", null, "Native food animation and doneEating completed; exactly one selected food unit was consumed.");
            else
                Finish("partially-succeeded", "FOOD_QUANTITY_UNCONFIRMED", "Native doneEating completed, but an exact one-unit inventory change was not verified.");
            return;
        }
        if (DateTime.UtcNow > _deadline)
            Finish("partially-succeeded", "FOOD_SETTLEMENT_UNCONFIRMED", "Native consumption completion was not observed within its bounded window; do not infer recovery or replay this action.");
    }

    private void Finish(string status, string? code, string message)
    {
        var request = _request;
        if (request is null) return;
        bool bound = _actor.BindingValid;
        var farmer = bound ? _actor.GameFarmer : null;
        int quantityAfter = farmer is null ? _quantityAfterStart : Quantity(farmer);
        int consumed = _quantityBefore - quantityAfter;
        bool settled = status == "succeeded";
        var effects = new List<Dictionary<string, object>>();
        if (consumed > 0)
            effects.Add(new() { ["kind"] = "native-player-food-consumption", ["state"] = settled ? "ate-food" : "consumed-unconfirmed",
                ["itemId"] = _itemId, ["quality"] = _quality, ["stack"] = consumed, ["inventorySlot"] = _slot });
        var consumption = new Dictionary<string, object>
        {
            ["itemId"] = _itemId, ["quality"] = _quality, ["inventorySlot"] = _slot,
            ["quantityBefore"] = _quantityBefore, ["quantityAfter"] = quantityAfter,
            ["doneEatingObserved"] = _completionObserved, ["settled"] = settled,
            ["freeSlotsBefore"] = _freeSlotsBefore, ["staminaBefore"] = _staminaBefore, ["healthBefore"] = _healthBefore,
        };
        if (farmer is not null)
        {
            consumption["freeSlotsAfter"] = _actor.FreeInventorySlots;
            consumption["staminaAfter"] = farmer.Stamina;
            consumption["healthAfter"] = farmer.health;
        }
        if (_cancelReason is not null) consumption["cancelRequested"] = _cancelReason;
        _request = null;
        if (ReferenceEquals(_completionOwner, this)) _completionOwner = null;
        _actor.SetActiveTask(null);
        IsPaused = false;
        Completed?.Invoke(new(request.CommandId, request.TaskId, status, settled ? 1 : 0, 0,
            status == "failed" ? 1 : 0, _revision(), effects,
            Error: code is null ? null : new(code, message, null, false), SkillId: "eat-food",
            Details: new Dictionary<string, object> { ["message"] = message, ["nativeConsumption"] = consumption }));
    }

    public void RequestCancel(string reason) => _cancelReason ??= reason;
    public void RequestPause() => IsPaused = true;
    public void Resume() => IsPaused = false;
}
