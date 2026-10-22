using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Menus;
using StardewValley.Objects;
using HarmonyLib;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.PlayerMode;
using StardewAI.Companion.Mod.Observation;
using StardewAI.Companion.Mod.Transport;
namespace StardewAI.Companion.Mod.Execution;

// Opening is an action, never a side effect of a read-only inventory observation.
public sealed class PlayerChestObservationMachine : ISkillExecutionMachine {
    private readonly PlayerActor actor;
    private readonly PlayerKnownObserver observer;
    private Chest? chest;
    private SkillExecutePayload? request;
    private DateTime deadline;
    private bool invoked;
    private Dictionary<string,int>? inventoryBefore;
    public event Action<SkillResultPayload>? Completed;
    public PlayerChestObservationMachine(PlayerActor actor, PlayerKnownObserver observer) { this.actor=actor; this.observer=observer; }
    public bool IsExecuting => request != null;
    public bool IsPaused { get; private set; }
    public int TotalTargets => 1;
    public int CurrentTargetIndex => 0;
    public string? ActiveTaskId => request?.TaskId;
    public void Start(SkillExecutePayload payload) { request=payload; invoked=false; inventoryBefore=null; deadline=DateTime.UtcNow.AddSeconds(10); actor.SetActiveTask(payload.TaskId); }
    public void Update(GameTime? time,long tickCount) {
        if (request == null || IsPaused) return;
        var tile=request.Parameters.ChestTile;
        if (!actor.BindingValid || tile == null) { Finish("failed","CHEST_NOT_REACHABLE","Opening requires a valid bound player and an observed chest tile."); return; }
        if (request.Parameters.LocationId != actor.LocationName) { Finish("failed","LOCATION_MISMATCH",$"Player is on {actor.LocationName} at {actor.Tile}; requested chest is on {request.Parameters.LocationId} at ({tile.X},{tile.Y}). Coordinates belong to their observed map; do not reuse them on another map."); return; }
        if (!invoked) {
            if (Game1.activeClickableMenu != null) { Finish("failed","MENU_BUSY","An existing native menu is open."); return; }
            if (!Game1.currentLocation.objects.TryGetValue(new Vector2(tile.X,tile.Y),out var obj) || obj is not Chest found || found.fridge.Value || !found.playerChest.Value && !found.giftbox.Value || found.SpecialChestType != Chest.SpecialChestTypes.None) { Finish("failed","NO_NORMAL_CHEST",$"No normal player chest or giftbox on {actor.LocationName} at ({tile.X},{tile.Y}); verify the observed object's locationId, not just its coordinates. Moving closer or clearing another tile cannot create a chest here."); return; }
            if (!actor.Tile.IsAdjacentTo(new(tile.X,tile.Y))) { Finish("failed","CHEST_NOT_REACHABLE",$"Observed chest on {actor.LocationName} at ({tile.X},{tile.Y}); player is at {actor.Tile}. Navigate to an adjacent walkable tile before opening."); return; }
            chest=found; invoked=true; actor.Halt();
            if (chest.giftbox.Value) {
                inventoryBefore=InventoryTotals();
                // Gift parcels have their own native branch before the OS
                // right-click gate. It dumps native contents and removes the
                // parcel; no ItemGrabMenu is created for this interaction.
                if (!chest.checkForAction(actor.GameFarmer!, false)) {
                    Finish("failed","GIFT_INTERACTION_REJECTED","Native gift interaction rejected."); return;
                }
            } else {
            // checkForAction gates player chests on OS right-click. Dispatch its
            // IL-verified native lock callback as the structured action instead.
            var open=AccessTools.DeclaredMethod(typeof(Chest),"<checkForAction>b__60_0");
            if (open == null) { Finish("failed","NATIVE_API_UNAVAILABLE","Native player chest interaction callback is unavailable."); return; }
            var pending=request;
            chest.GetMutex().RequestLock(() => {
                if (request != pending || !actor.BindingValid) { chest.GetMutex().ReleaseLock(); return; }
                open.Invoke(chest,null);
            },() => { if (request == pending) Finish("failed","CHEST_LOCKED","Native chest mutex could not be acquired."); });
            }
        }
        if (chest?.giftbox.Value == true && !Game1.currentLocation.objects.Values.Any(o => ReferenceEquals(o,chest))) {
            if (actor.GameFarmer!.freezePause > 0) return; // let the native parcel animation finish
            observer.BumpRevision(); Finish("succeeded",null,null); return;
        }
        if (Game1.activeClickableMenu is ItemGrabMenu menu && ReferenceEquals(menu.context,chest)) {
            observer.NoteChestOpened(actor.LocationName,tile.X,tile.Y);
            // Native cleanup releases the chest mutex; only close the menu opened by this action.
            menu.exitThisMenu(); Finish("succeeded",null,null); return;
        }
        if (DateTime.UtcNow >= deadline) Finish("failed","CHEST_OPEN_TIMEOUT","Native chest menu did not open within ten seconds.");
    }
    public void RequestCancel(string reason) { if (request != null) { if (Game1.activeClickableMenu is ItemGrabMenu menu && ReferenceEquals(menu.context,chest)) menu.exitThisMenu(); Finish("cancelled","CANCELLED",reason); } }
    public void RequestPause() => IsPaused=true;
    public void Resume() => IsPaused=false;
    private Dictionary<string,int> InventoryTotals() => actor.GameFarmer!.Items.Where(i => i != null)
        .GroupBy(i => i.QualifiedItemId).ToDictionary(g => g.Key,g => g.Sum(i => i.Stack));
    private void Finish(string state,string? code,string? message) {
        var saved=request!; request=null; IsPaused=false; actor.SetActiveTask(null); observer.BumpRevision();
        var effects=new List<Dictionary<string,object>>();
        if(state=="succeeded" && inventoryBefore != null) {
            foreach(var item in InventoryTotals()) {
                int gained=item.Value-inventoryBefore.GetValueOrDefault(item.Key);
                if(gained>0) effects.Add(new() {{"state","picked-up"},{"itemId",item.Key},{"stack",gained},{"via","native-gift-inventory-observation"},{"location",actor.LocationName}});
            }
        }
        Completed?.Invoke(new(saved.CommandId,saved.TaskId,state,state=="succeeded"?1:0,0,state=="failed"?1:0,observer.WorldRevision,effects,Error:code==null?null:new(code,message!,null,false),SkillId:"open-chest"));
    }
}
