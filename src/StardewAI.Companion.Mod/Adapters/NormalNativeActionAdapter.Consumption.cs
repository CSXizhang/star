using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.CompilerServices;
using HarmonyLib;
using StardewValley;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Adapters;

public sealed partial class NormalNativeActionAdapter
{
    private NativeActionStepResult EatFood(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var farmer = actor.GameFarmer;
        if (farmer is null || !string.Equals(actor.LocationName,request.LocationId,StringComparison.OrdinalIgnoreCase))
            return NativeActionStepResult.Precondition("Companion is not on the requested map.","wrong-map");
        var food = farmer.Items.OfType<StardewValley.Object>().FirstOrDefault(i=>SameItem(i.QualifiedItemId,request.ItemId)&&i.Stack>0);
        if (food is null) return NativeActionStepResult.Precondition("Companion has no requested food.","missing-item");
        if (food.Edibility < 0 || food.QualifiedItemId == "(O)434" || food.staminaRecoveredOnConsumption() <= 0)
            return NativeActionStepResult.Precondition("Only ordinary food with positive energy recovery is supported.","unsupported-food");
        if (farmer.hasBuff("6")) return NativeActionStepResult.Precondition("Native nausea prevents eating.","nauseated");
        if (farmer.Stamina >= farmer.MaxStamina && farmer.health >= farmer.maxHealth)
            return NativeActionStepResult.Precondition("Companion is already fully recovered; no food consumed.","already-recovered");
        return InvokeIsolated(actor,"eat-food",()=>
        {
            var previousEat = farmer.itemToEat;
            var previousGrabbed = farmer.mostRecentlyGrabbedItem;
            float before = farmer.Stamina;
            int health = farmer.health;
            try
            {
                // doneEating is the game's consumption completion primitive. It does not consume
                // the inventory unit: the player interaction caller does that separately.
                var one = (StardewValley.Object)food.getOne();
                farmer.itemToEat = one;
                farmer.mostRecentlyGrabbedItem = one;
                using (new NativeFoodConsumptionScope(farmer,one)) farmer.doneEating();
                if (farmer.Stamina <= before && farmer.health <= health)
                    return NativeActionStepResult.Failed("Native consumption produced no recovery; food retained.");
                food.Stack--;
                if (food.Stack <= 0) farmer.Items[farmer.Items.IndexOf(food)] = null;
                return NativeActionStepResult.Succeeded("ate-food",itemId:request.ItemId,itemCount:1);
            }
            finally { farmer.itemToEat=previousEat; farmer.mostRecentlyGrabbedItem=previousGrabbed; }
        });
    }
}

// Permit only the exact detached farmer for this single doneEating call. Native formulas,
// buffs, health and exhaustion effects stay native; Game1.player is never swapped.
internal sealed class NativeFoodConsumptionScope : IDisposable
{
    [ThreadStatic] private static Farmer? _receiver;
    [ThreadStatic] private static StardewValley.Object? _food;
    private static bool _patched;
    public NativeFoodConsumptionScope(Farmer receiver, StardewValley.Object food)
    {
        if (_receiver is not null || ReferenceEquals(receiver,Game1.player) || !ReferenceEquals(receiver.itemToEat,food))
            throw new InvalidOperationException("Invalid consumption context.");
        if (!_patched)
        {
            new Harmony("StardewAI.Companion.NativeFood").Patch(AccessTools.Method(typeof(Farmer),nameof(Farmer.doneEating)),
                transpiler:new HarmonyMethod(typeof(NativeFoodConsumptionScope),nameof(Transpile)));
            _patched=true;
        }
        _receiver=receiver; _food=food;
    }
    private static bool IsReceiver(Farmer farmer) => farmer.IsLocalPlayer || ReferenceEquals(farmer,_receiver)
        && _food is not null && ReferenceEquals(farmer.itemToEat,_food) && !ReferenceEquals(farmer,Game1.player);
    private static IEnumerable<CodeInstruction> Transpile(IEnumerable<CodeInstruction> instructions)
    {
        var code=instructions.ToList();
        var matches=code.Where(i=>i.Calls(AccessTools.PropertyGetter(typeof(Farmer),nameof(Farmer.IsLocalPlayer)))).ToList();
        if(matches.Count!=1) throw new InvalidOperationException("Unsupported native food ownership gates.");
        matches[0].opcode=OpCodes.Call; matches[0].operand=AccessTools.Method(typeof(NativeFoodConsumptionScope),nameof(IsReceiver));
        return code;
    }
    public void Dispose() { _receiver=null; _food=null; }
}

/// <summary>Reverse-copy only the native overnight resource block; no quests, mail, farmhouse or world updates.</summary>
internal static class NativeOvernightRecovery
{
    private static bool _patched;
    public static void Apply(Farmer farmer, int bedtime)
    {
        if (ReferenceEquals(farmer,Game1.player)) throw new InvalidOperationException("Expected the detached companion.");
        if (!_patched)
        {
            new Harmony("StardewAI.Companion.NativeOvernight").CreateReversePatcher(
                AccessTools.Method(typeof(Farmer),nameof(Farmer.dayupdate)),
                new HarmonyMethod(typeof(NativeOvernightRecovery),nameof(Recover))).Patch();
            _patched=true;
        }
        farmer.timeWentToBed.Value=bedtime;
        Recover(farmer,bedtime);
        farmer.ClearBuffs();
        farmer.timeWentToBed.Value=0;
    }
    [MethodImpl(MethodImplOptions.NoInlining)]
    private static void Recover(Farmer farmer, int bedtime)
    {
        // Harmony discovers this local transpiler on the reverse-patch stand-in.
        static IEnumerable<CodeInstruction> Transpiler(IEnumerable<CodeInstruction> instructions)
        {
            var code=instructions.ToList();
            var getter=AccessTools.PropertyGetter(typeof(Farmer),nameof(Farmer.Stamina));
            int first=code.FindIndex(i=>i.Calls(getter))-1;
            int last=code.FindIndex(first+1,i=>i.opcode==OpCodes.Stfld && Equals(i.operand,AccessTools.Field(typeof(Farmer),nameof(Farmer.health))));
            if(first<0 || last<=first || code[first].opcode!=OpCodes.Ldarg_0)
                throw new InvalidOperationException("Unsupported native overnight resource block.");
            var slice=code.GetRange(first,last-first+1);
            // Every branch must stay in the isolated native block.
            var labels=slice.SelectMany(i=>i.labels).ToHashSet();
            if(slice.Any(i=>i.operand is Label target && !labels.Contains(target)))
                throw new InvalidOperationException("Native overnight resource block has an external branch.");
            slice.Add(new CodeInstruction(OpCodes.Ret));
            return slice;
        }
        _=Transpiler(Array.Empty<CodeInstruction>());
        throw new NotSupportedException("Native overnight recovery was not patched.");
    }
}
