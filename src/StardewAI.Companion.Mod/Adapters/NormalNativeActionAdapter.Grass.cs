using Microsoft.Xna.Framework;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Adapters;

public sealed partial class NormalNativeActionAdapter
{
    private NativeActionStepResult CutGrass(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var farmer=actor.GameFarmer;
        var location=ResolveLocation(request.LocationId);
        if(farmer is null || location is null || !ReferenceEquals(farmer.currentLocation,location))
            return NativeActionStepResult.Precondition("Companion is not on the requested map.","wrong-map");
        if(!actor.Tile.IsAdjacentTo(target.Tile) && actor.Tile!=target.Tile)
            return NativeActionStepResult.Precondition("Grass cutting requires an adjacent tile.","not-adjacent");
        var scythe=farmer.Items.OfType<MeleeWeapon>().FirstOrDefault(t=>t.isScythe());
        if(scythe is null) return NativeActionStepResult.Precondition("Companion needs its own scythe.","missing-tool:Scythe",true);
        var tile=new Vector2(target.Tile.X,target.Tile.Y);
        if(!location.terrainFeatures.TryGetValue(tile,out var feature))
            return NativeActionStepResult.Precondition("The requested tile contains no grass or dead crop.","no-grass");
        if(feature is HoeDirt liveDirt && liveDirt.crop is not null && !liveDirt.crop.dead.Value)
            return NativeActionStepResult.Precondition("Living crops cannot be cleared by this action.","live-crop-protected");
        var grass=feature as Grass;
        var dirt=feature as HoeDirt;
        if(grass is null && !IsDeadCrop(dirt))
            return NativeActionStepResult.Precondition("The requested tile contains no grass or dead crop.","no-grass");
        return InvokeIsolated(actor,"cut-grass",()=>
        {
            int before=grass?.numberOfWeeds.Value ?? 0;
            // Base Tool.DoFunction binds lastUser. The native grass callback controls
            // cutting, random hay yield and StoreHayInAnySilo; no hay is manufactured.
            scythe.DoFunction(location,target.Tile.X*64+32,target.Tile.Y*64+32,0,farmer);
            if(!ReferenceEquals(scythe.getLastFarmerToUse(),farmer))
                return NativeActionStepResult.Failed("Scythe user did not bind to companion.");
            if(dirt is not null)
            {
                // Recheck before the native callback: only dead crops are authorized.
                // Native HoeDirt.performToolAction's scythe branch destroys the dead
                // crop itself and returns false (the tilled soil must stay in place).
                if(!IsDeadCrop(dirt))
                    return NativeActionStepResult.Precondition("The crop is no longer dead.","live-crop-protected");
                dirt.performToolAction(scythe,0,tile);
                return dirt.crop is null ? NativeActionStepResult.Succeeded("cleared-dead-crop")
                    : NativeActionStepResult.Failed("Native scythe action did not clear the dead crop.");
            }
            if(grass is null) return NativeActionStepResult.Failed("Grass target changed.");
            bool remove=grass.performToolAction(scythe,0,tile);
            if(remove) location.terrainFeatures.Remove(tile);
            if(remove) return NativeActionStepResult.Succeeded("cut-grass");
            // Dense grass needs multiple native swings. A reduced density is
            // progress, not a cleared tile: keep the same target and show each
            // swing through the existing multi-tick action/animation lifecycle.
            return grass.numberOfWeeds.Value<before || grass.grassType.Value == Grass.blueGrass
                ? NativeActionStepResult.Continue("cutting-grass")
                : NativeActionStepResult.Failed("Native scythe action did not reduce the grass.");
        });
    }

    private static bool IsDeadCrop(HoeDirt? dirt) => dirt?.crop?.dead.Value == true;
}
