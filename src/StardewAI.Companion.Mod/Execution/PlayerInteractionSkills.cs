using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Characters;
using StardewValley.Monsters;
using StardewValley.Menus;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Transport;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Execution;

public sealed partial class CompanionMechanicsCoordinator
{
    private PlayerFishingMachine? _fishingMachine;
    private PlayerCombatMachine? _combatMachine;

    private object InspectPlayerActivities()
    {
        if (_actor is not PlayerActor p || !p.BindingValid)
            throw new InvalidOperationException("Native player binding required.");
        var farmer = p.GameFarmer ?? throw new InvalidOperationException("Player binding lost.");
        var loc = farmer.currentLocation;
        var point = farmer.TilePoint;
        bool festival = ActorGuards.FestivalFreeRoam;
        IEnumerable<NPC> visibleNpcs = festival ? Game1.CurrentEvent!.actors : loc.characters;
        var actions = new List<object>();
        var fishing = new List<object>();
        var groundItems = new List<object>();
        var fixtures = _observer.ScanInteractables(loc.NameOrUniqueName).Select(f =>
        {
            var row = new Dictionary<string, object>(f);
            if (f.GetValueOrDefault("tile") is Dictionary<string, object> tile
                && tile.GetValueOrDefault("x") is int x && tile.GetValueOrDefault("y") is int y)
            {
                int distance = Math.Abs(x-point.X)+Math.Abs(y-point.Y);
                row["distanceTiles"] = distance;
                row["canInteractNow"] = distance <= 1 && (!Game1.eventUp || festival) && Game1.activeClickableMenu is null;
                row["operation"] = f.GetValueOrDefault("type") switch { "parcel" => "open_chest", "bed" => "sleep", _ => "interact_tile" };
            }
            return row;
        }).ToList();
        if (loc is Farm && _shippingAdapter is not null)
        {
            var footprint = _shippingAdapter.GetShippingBinFootprint(loc.NameOrUniqueName);
            if (footprint.Count > 0)
            {
                var occupied = footprint.ToHashSet();
                var approaches = footprint.SelectMany(tile => new[] {
                    new TileCoordinate(tile.X - 1, tile.Y), new TileCoordinate(tile.X + 1, tile.Y),
                    new TileCoordinate(tile.X, tile.Y - 1), new TileCoordinate(tile.X, tile.Y + 1) })
                    .Distinct().Where(tile => !occupied.Contains(tile)
                        && _observer.IsTilePassable(loc.NameOrUniqueName, tile)).ToArray();
                fixtures.Add(new Dictionary<string, object> {
                    ["type"] = "shipping-bin", ["operation"] = "ship_items",
                    ["locationId"] = loc.NameOrUniqueName,
                    ["footprint"] = footprint.Select(tile => new {x=tile.X,y=tile.Y}).ToArray(),
                    ["passableApproachTiles"] = approaches.Select(tile => new {x=tile.X,y=tile.Y}).ToArray(),
                    ["canInteractNow"] = !Game1.eventUp && Game1.activeClickableMenu is null
                        && footprint.Any(tile => p.Tile.IsAdjacentTo(tile)),
                    ["approachRouteKnown"] = false,
                    ["provenance"] = "current-native-shipping-bin-footprint-and-passability",
                    ["note"] = "Approach tiles are passable observations, not proven routes. Query a selected approach tile if needed; ship_items still requires explicit model-selected items." });
            }
        }
        foreach (var debris in loc.debris)
        {
            if (debris is null || !NativeAnimalHarvestInventoryScope.IsOrdinaryDebris(debris)) continue;
            foreach (var chunk in debris.Chunks)
            {
                var position = chunk.position.Value;
                groundItems.Add(new { tile = new { x = (int)(position.X / 64), y = (int)(position.Y / 64) },
                    itemId = debris.item?.QualifiedItemId ?? debris.itemId.Value,
                    kind = "native-loose-debris", observedStack = debris.item?.Stack ?? 1 });
            }
        }
        foreach (var pair in loc.objects.Pairs)
        {
            var item = pair.Value;
            if (!NormalNativeActionAdapter.IsPickupGroundObject(item)) continue;
            groundItems.Add(new { tile = new { x = (int)pair.Key.X, y = (int)pair.Key.Y },
                itemId = item.QualifiedItemId, kind = "native-grabbable-object", observedStack = item.Stack });
        }
        // Public current-location map interactions, not future random outcomes.
        bool wholeFarm = loc is Farm;
        for (int x = wholeFarm ? 0 : Math.Max(0, point.X-12); x <= (wholeFarm ? loc.Map.Layers[0].LayerWidth-1 : Math.Min(loc.Map.Layers[0].LayerWidth-1,point.X+12)); x++)
        for (int y = wholeFarm ? 0 : Math.Max(0, point.Y-12); y <= (wholeFarm ? loc.Map.Layers[0].LayerHeight-1 : Math.Min(loc.Map.Layers[0].LayerHeight-1,point.Y+12)); y++)
        {
            string? action = loc.doesTileHaveProperty(x,y,"Action","Buildings");
            string? touch = loc.doesTileHaveProperty(x,y,"TouchAction","Back");
            if (!string.IsNullOrWhiteSpace(action) || !string.IsNullOrWhiteSpace(touch))
                actions.Add(new { tile=new {x,y},action,touchAction=touch,
                    distanceTiles=Math.Abs(x-point.X)+Math.Abs(y-point.Y),
                    canInteractNow=!string.IsNullOrWhiteSpace(action) && Math.Abs(x-point.X)+Math.Abs(y-point.Y)<=1
                        && (!Game1.eventUp || festival) && Game1.activeClickableMenu is null });
            if (loc.isWaterTile(x,y) && fishing.Count < 24)
            {
                foreach (var d in new[]{(dx:0,dy:1,face:0),(dx:-1,dy:0,face:1),(dx:0,dy:-1,face:2),(dx:1,dy:0,face:3)})
                {
                    int sx=x+d.dx,sy=y+d.dy;
                    if (sx<0||sy<0||sx>=loc.Map.Layers[0].LayerWidth||sy>=loc.Map.Layers[0].LayerHeight||loc.isWaterTile(sx,sy)) continue;
                    if (_observer.IsTilePassable(loc.NameOrUniqueName,new TileCoordinate(sx,sy)))
                    { fishing.Add(new {standTile=new {x=sx,y=sy},facing=d.face,waterTile=new{x,y}}); break; }
                }
            }
        }
        object? menuChoices = Menus.CompanionMenuClock.IsConversation(Game1.activeClickableMenu) ? null : Game1.activeClickableMenu switch {
            DialogueBox {isQuestion:true} question => (question.responses ?? Array.Empty<Response>()).Select((response,i) => new {index=i,label=response.responseText,operation="native_answer"}).ToArray(),
            MineElevatorMenu elevator => elevator.elevators.Select((c,i)=>new {index=i,label=c.name}).ToArray(),
            ItemGrabMenu grab => grab.ItemsToGrabMenu.actualInventory.Select((item,i)=>new {index=i,itemId=item?.QualifiedItemId,name=item?.DisplayName,stack=item?.Stack}).ToArray(),
            CraftingPage crafting when crafting.heldItem is not null => crafting.inventory.actualInventory.Select((item,i) => new {index=i,itemId=item?.QualifiedItemId,stack=item?.Stack,cursorItemId=crafting.heldItem.QualifiedItemId,cursorStack=crafting.heldItem.Stack,acceptsCursor=item is null || item.canStackWith(crafting.heldItem) && item.maximumStackSize()-item.Stack >= crafting.heldItem.Stack}).ToArray(),
            ConfirmationDialog => new[] { new {index=0,label="Confirm"}, new {index=1,label="Cancel"} },
            NamingMenu naming => new[] { new {index=0,label="Confirm explicit name",input=DescribeNativeNamingInput(naming)} },
            _ => null
        };
        var festivalExits = new List<object>();
        if (festival)
        {
            int width = loc.Map.Layers[0].LayerWidth, height = loc.Map.Layers[0].LayerHeight;
            void AddBoundary(int x, int y, int outsideX, int outsideY)
            {
                if (!_observer.IsTilePassable(loc.NameOrUniqueName, new TileCoordinate(x,y))) return;
                festivalExits.Add(new { standTile=new{x,y}, tile=new{x=outsideX,y=outsideY}, operation="interact_tile",
                    effect="opens-native-leave-festival-confirmation; requires-explicit-answer" });
            }
            for (int x=0; x<width; x++) { AddBoundary(x,0,x,-1); AddBoundary(x,height-1,x,height); }
            for (int y=0; y<height; y++) { AddBoundary(0,y,-1,y); AddBoundary(width-1,y,width,y); }
        }
        return new {
            eventIsFestival=Game1.CurrentEvent?.isFestival == true, eventAllowsMovement=festival,
            festivalName=festival ? Game1.CurrentEvent!.FestivalName : null, festivalExits,
            festivalNote=festival ? "Native festival free-roam: same-map movement and interactions are available. Festival clock does not advance to tomorrow by waiting. Native boundary exits open a question; no leave/participate choice is automatic." : null,
            locationId=loc.NameOrUniqueName,worldRevision=_observer.WorldRevision,playerTile=new{x=point.X,y=point.Y},
            scope="current-location-public-observation",interactionRadius=wholeFarm ? (int?)null : 12,fishingRadius=wholeFarm ? (int?)null : 12,
            farmSpatialScope=wholeFarm ? "whole-farm" : null,fixtures,
            // Public current-map transitions are navigation geometry, not hidden
            // inventory, future events, or an inferred unlock.
            exits=loc.warps.Where(w=>!w.npcOnly.Value).Select(w=> {
                var standing=FarmSpaceObserver.FindStandingTile(_observer,w.TargetName,new TileCoordinate(w.TargetX,w.TargetY));
                return new {tile=new{x=w.X,y=w.Y},targetLocation=w.TargetName,
                    nativeLandingTile=new{x=w.TargetX,y=w.TargetY},
                    targetTile=standing.HasValue ? new{x=standing.Value.X,y=standing.Value.Y} : null,
                    standingTileAvailable=standing.HasValue,scope="current-map-native-warp",
                    evidence="Checked nearby standing tile, not an unlock; use navigate_to destination, not source trigger."};
            }).ToArray(),
            npcs=visibleNpcs.Where(n=>n is not Monster).Select(n=>new {
                name=n.Name,displayName=n.displayName,tile=new{x=n.TilePoint.X,y=n.TilePoint.Y},
                isConversationCharacter=n is not Horse,
                distanceTiles=Math.Abs(n.TilePoint.X-point.X)+Math.Abs(n.TilePoint.Y-point.Y),
                canInitiateConversation=n is not Horse && Math.Abs(n.TilePoint.X-point.X)+Math.Abs(n.TilePoint.Y-point.Y)<=1
                    && (!Game1.eventUp || festival) && Game1.activeClickableMenu is null,
                approachRequired=Math.Abs(n.TilePoint.X-point.X)+Math.Abs(n.TilePoint.Y-point.Y)>1
            }).ToArray(),
            monsters=loc.characters.OfType<Monster>().Select(n=>new {name=n.Name,tile=new{x=n.TilePoint.X,y=n.TilePoint.Y},health=n.Health}).ToArray(),
            interactions=actions.Take(48).ToArray(),interactionsTruncated=actions.Count>48,
            groundPickupObservationStatus="observed",groundPickupCount=groundItems.Count,
            groundPickupItems=groundItems.Take(48).ToArray(),groundPickupItemsTruncated=groundItems.Count>48,
            groundPickupScope="current-location-supported-native-pickups",
            fishingSpots=fishing.ToArray(),fishingSpotsTruncated=fishing.Count>=24,
            nativeMenu=Menus.CompanionMenuClock.IsConversation(Game1.activeClickableMenu) ? "CompanionConversation" : Game1.activeClickableMenu?.GetType().Name,menuChoices,nativeUi=CaptureNativeUi(),
            hasFishingRod=farmer.Items.Any(i=>i is FishingRod),hasWeapon=farmer.Items.OfType<MeleeWeapon>().Any(w=>!w.isScythe()),
            note="Pickup candidates are currently present native loose items or grabbable objects, not cleared terrain. Native proximity may already collect drops: inspect inventoryDelta before scheduling another pickup. NPCs and map actions are live public locations; routes still require native collision. Fish selection/catch and mine contents are unknown until actual native play. No unlock is inferred from a geometric route."
        };
    }

    private bool TryAcceptPlayerInteraction(SkillExecutePayload payload,EnvelopeDto envelope,out SkillResultPayload? reject)
    {
        reject=null;
        if (_actor is not PlayerActor actor || !actor.BindingValid)
        { reject=BuildRejection(payload,"PLAYER_ONLY","This interaction binds only to the controlled native player.",false);return false; }
        if (!TryCheckIdle(out var busy))
        { reject=BuildRejection(payload,"ACTOR_BUSY",busy!,true);return false; }
        var p=actor.GameFarmer ?? throw new InvalidOperationException("Player binding lost.");var loc=p.currentLocation;
        if (payload.Parameters.LocationId != loc.NameOrUniqueName)
        { reject=BuildRejection(payload,"LOCATION_MISMATCH","Navigate to the explicitly observed interaction location first.",false);return false; }
        try
        {
            actor.Halt();
            object effect;
            SkillResultError? interactionError = null;
            if(payload.SkillId=="native-menu-select")
            {
                int index=payload.Parameters.ChoiceIndex??throw new ArgumentException("Explicit observed menu choiceIndex required.");
                if (Game1.activeClickableMenu is NamingMenu naming)
                {
                    if (index != 0) throw new ArgumentException("Only the observed naming confirmation at index 0 is supported.");
                    string text = payload.Parameters.MenuText ?? throw new ArgumentException("Explicit menuText is required for NamingMenu confirmation.");
                    if (string.IsNullOrWhiteSpace(text) || text.Length > 64 || text.Any(char.IsControl))
                        throw new ArgumentException("Naming text must contain 1..64 characters without control characters.");
                    if (text.Length < naming.minLength || naming.doneNaming is null)
                        throw new InvalidOperationException("Native naming input or callback is not ready; no confirmation sent.");
                    // Native TextBox enforces its own filtering and visual width.
                    // A changed input must be observed before confirming it.
                    naming.textBox.Text = text;
                    string actualText = naming.textBox.Text;
                    bool exactInput = actualText == text;
                    if (exactInput)
                    {
                        var button = naming.doneNamingButton;
                        naming.receiveLeftClick(button.bounds.Center.X, button.bounds.Center.Y);
                    }
                    else
                        interactionError = new("NAME_TEXT_CHANGED", "The native text box changed the requested name through its own input rules. The changed text is observed; no confirmation was sent.", null, false);
                    effect = new {kind="native-naming-input",index,requestedText=text,actualText,
                        confirmationInputSent=exactInput,menuClosed=!ReferenceEquals(Game1.activeClickableMenu,naming),
                        worldOutcomeConfirmed=false};
                }
                else if (payload.Parameters.MenuText is not null)
                    throw new ArgumentException("menuText is accepted only by the currently observed native NamingMenu.");
                else if(Game1.activeClickableMenu is MineElevatorMenu elevator)
                {
                    if(index<0||index>=elevator.elevators.Count)throw new ArgumentException("No observed elevator choice at that index.");
                    var c=elevator.elevators[index];elevator.receiveLeftClick(c.bounds.Center.X,c.bounds.Center.Y);
                    effect=new {kind="native-menu-choice",menu="MineElevatorMenu",index,label=c.name};
                }
                else if(Game1.activeClickableMenu is ItemGrabMenu grab && !grab.shippingBin)
                {
                    if(grab.heldItem!=null)throw new InvalidOperationException("An item is already held; do not select another source item.");
                    if(index<0||index>=grab.ItemsToGrabMenu.actualInventory.Count||index>=grab.ItemsToGrabMenu.inventory.Count||grab.ItemsToGrabMenu.actualInventory[index]==null)throw new ArgumentException("Observed source slot is empty or unavailable.");
                    var selected=grab.ItemsToGrabMenu.actualInventory[index];
                    if(!p.couldInventoryAcceptThisItem(selected))throw new InvalidOperationException("Native player inventory cannot accept this item; no selection made.");
                    var source=grab.ItemsToGrabMenu.inventory[index];
                    grab.receiveLeftClick(source.bounds.Center.X,source.bounds.Center.Y);
                    if(ReferenceEquals(Game1.activeClickableMenu,grab)&&grab.heldItem!=null)
                    {
                        // Deposit only this selected item through native clicks.
                        // Skip full stacks and continue if a partial stack leaves
                        // a remainder on the cursor; never swap unrelated items.
                        int slots=Math.Min(grab.inventory.actualInventory.Count,grab.inventory.inventory.Count);
                        for(int destination=0;destination<slots && ReferenceEquals(Game1.activeClickableMenu,grab) && grab.heldItem!=null;destination++)
                        {
                            var item=grab.inventory.actualInventory[destination];
                            if(item!=null && (!item.canStackWith(grab.heldItem) || item.Stack>=item.maximumStackSize()))continue;
                            var slot=grab.inventory.inventory[destination];
                            grab.receiveLeftClick(slot.bounds.Center.X,slot.bounds.Center.Y);
                        }
                    }
                    effect=new {kind="native-menu-item-selection",index,itemId=selected.QualifiedItemId,stillHeld=grab.heldItem!=null};
                }
                else if (Game1.activeClickableMenu is CraftingPage crafting && crafting.heldItem is { } held)
                {
                    if (index < 0 || index >= crafting.inventory.actualInventory.Count || index >= crafting.inventory.inventory.Count)
                        throw new ArgumentException("No observed crafting cursor destination at this index.");
                    var item = crafting.inventory.actualInventory[index];
                    if (item is not null && (!item.canStackWith(held) || item.maximumStackSize() - item.Stack < held.Stack))
                        throw new InvalidOperationException("The selected slot cannot hold the crafted cursor item without swapping inventory.");
                    var destination = crafting.inventory.inventory[index];
                    crafting.receiveLeftClick(destination.bounds.Center.X, destination.bounds.Center.Y);
                    effect = new {kind="native-crafting-cursor-selection",index,itemId=held.QualifiedItemId,stillHeld=crafting.heldItem is not null};
                }
                else throw new InvalidOperationException("Only observed native item collection, crafting cursor destinations, existing mine elevator choices and explicit native naming input are supported. Story/permanent questions use explicit native_answer authorization.");
            }
            else if (payload.SkillId=="talk-npc")
            {
                IEnumerable<NPC> candidates = ActorGuards.FestivalFreeRoam ? Game1.CurrentEvent!.actors : loc.characters;
                var npc=candidates.FirstOrDefault(n=>n.Name==payload.Parameters.NpcName && n is not Monster && n is not Horse);
                if(npc==null)throw new InvalidOperationException("NPC not currently present in this location; observe again rather than assume a schedule.");
                if(Math.Abs(p.TilePoint.X-npc.TilePoint.X)+Math.Abs(p.TilePoint.Y-npc.TilePoint.Y)>1)
                {reject=BuildRejection(payload,"NOT_ADJACENT","Approach an observed adjacent NPC tile first.",true);return false;}
                bool handled=ActorGuards.FestivalFreeRoam
                    ? Game1.CurrentEvent!.checkAction(new xTile.Dimensions.Location(npc.TilePoint.X,npc.TilePoint.Y),Game1.viewport,p)
                    : npc.checkAction(p,loc);
                if(!handled)throw new InvalidOperationException("Native NPC action declined; no conversation or friendship change inferred.");
                var nativeUi = CaptureNativeUi();
                string? actualSpeaker = nativeUi?.DialogueSpeaker;
                if (actualSpeaker is not null && actualSpeaker != npc.Name)
                    interactionError = new("NPC_SPEAKER_MISMATCH",
                        $"Requested {npc.Name}, but the actual native dialogue speaker is {actualSpeaker}. The observed dialogue is preserved; the requested conversation is not confirmed.", null, false);
                // Festival checkAction may select another overlapping actor. A
                // requested NPC is not evidence of the actual dialogue speaker.
                effect=new {kind="native-npc-action",requestedNpcName=npc.Name,npcName=actualSpeaker,
                    dialogueSpeaker=actualSpeaker,requestedNpcMatchedDialogue=actualSpeaker is null ? (bool?)null : actualSpeaker == npc.Name,
                    menu=Game1.activeClickableMenu?.GetType().Name,dialogueUp=Game1.dialogueUp};
            }
            else if(payload.SkillId=="interact-tile")
            {
                var tile=payload.Parameters.Tile??throw new ArgumentException("Explicit tile required.");
                if(Math.Abs(p.TilePoint.X-tile.X)+Math.Abs(p.TilePoint.Y-tile.Y)>1)
                {reject=BuildRejection(payload,"NOT_ADJACENT","Approach the public interaction tile first.",true);return false;}
                bool outside = tile.X<0 || tile.Y<0 || tile.X>=loc.Map.Layers[0].LayerWidth || tile.Y>=loc.Map.Layers[0].LayerHeight;
                bool handled=ActorGuards.FestivalFreeRoam
                    ? outside ? Game1.CurrentEvent!.TryStartEndFestivalDialogue(p)
                        : Game1.CurrentEvent!.checkAction(new xTile.Dimensions.Location(tile.X,tile.Y),Game1.viewport,p)
                    : loc.checkAction(new xTile.Dimensions.Location(tile.X,tile.Y),Game1.viewport,p);
                if(!handled)throw new InvalidOperationException("Native map action declined. Touch actions require actual walking onto the tile, not a forced warp.");
                effect=new{kind="native-map-action",tile,menu=Game1.activeClickableMenu?.GetType().Name,eventUp=Game1.eventUp};
            }
            else if(payload.SkillId=="fish")
            {
                if(_fishingMachine==null){_fishingMachine=new(actor,()=>PlayerAuthority?.AgentOwns==true,()=>_observer.WorldRevision);_fishingMachine.Completed+=CompleteExecution;}
                _currentExecuteEnvelope=envelope;_currentExecuteCommandId=payload.CommandId;
                return _fishingMachine.Start(payload,out reject);
            }
            else if(payload.SkillId=="attack")
            {
                if(_combatMachine==null){_combatMachine=new(actor,()=>PlayerAuthority?.AgentOwns==true,()=>_observer.WorldRevision);_combatMachine.Completed+=CompleteExecution;}
                _currentExecuteEnvelope=envelope;_currentExecuteCommandId=payload.CommandId;
                return _combatMachine.Start(payload,out reject);
            }
            else throw new ArgumentException("Unsupported player interaction.");
            var result=new SkillResultPayload(payload.CommandId,payload.TaskId,interactionError is null ? "succeeded" : "partially-succeeded",interactionError is null ? 1 : 0,0,0,_observer.WorldRevision,new(),Error:interactionError,SkillId:payload.SkillId,Details:new Dictionary<string,object>{["nativeInteraction"]=effect,["nativeUi"]=CaptureNativeUi()!});
            _lastSkillResultPayload=result;
            if(_transportServer!=null)_=_transportServer.SendSkillResultAsync(result,envelope.MessageId,envelope.IdempotencyKey);
            OnExecutionCompleted?.Invoke();return true;
        }
        catch(Exception ex){reject=BuildRejection(payload,"NATIVE_INTERACTION_UNAVAILABLE",ex.Message,false);return false;}
    }
}
