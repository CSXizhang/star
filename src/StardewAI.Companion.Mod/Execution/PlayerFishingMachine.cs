using System.Reflection;
using System.Reflection.Emit;
using HarmonyLib;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod.Execution;

/// One ordinary native cast. The controller supplies held use-tool intentions
/// at the native input boundary; native rod/bar physics, random fish, difficulty,
/// stamina, animations, reward and inventory settlement remain untouched.
public sealed class PlayerFishingMachine : ISkillExecutionMachine
{
    private readonly PlayerActor actor;
    private readonly Func<bool> owns;
    private readonly Func<int> revision;
    private SkillExecutePayload? request;
    private FishingRod? rod;
    private DateTime deadline;
    private Dictionary<string,int> before=new();
    private bool started;
    private static PlayerFishingMachine? inputOwner;
    private static bool installed;
    public event Action<SkillResultPayload>? Completed;
    public bool IsExecuting=>request!=null;
    public bool IsPaused{get;private set;}
    public int TotalTargets=>1;
    public int CurrentTargetIndex=>0;
    public string? ActiveTaskId=>request?.TaskId;
    public PlayerFishingMachine(PlayerActor actor,Func<bool> owns,Func<int> revision)
    {
        this.actor=actor;this.owns=owns;this.revision=revision;
        if(!installed)
        {
            var h=new Harmony("StardewAI.NativeFishingInput");
            foreach(var method in new[]{AccessTools.Method(typeof(FishingRod),nameof(FishingRod.tickUpdate)),AccessTools.Method(typeof(BobberBar),nameof(BobberBar.update))})
                h.Patch(method,transpiler:new HarmonyMethod(typeof(PlayerFishingMachine),nameof(InputBoundary)));
            installed=true;
        }
    }
    public bool Start(SkillExecutePayload payload,out SkillResultPayload? reject)
    {
        reject=null;var p=actor.GameFarmer ?? throw new InvalidOperationException("Player binding lost.");
        rod=p.Items.OfType<FishingRod>().FirstOrDefault();
        string? error=rod==null?"No native FishingRod in this player's inventory; obtain it through Willy/event/shop first.":
            p.Stamina<=2?"Insufficient stamina for a native cast.":
            payload.Parameters.Facing is not int f||f<0||f>3?"Explicit facing 0=up,1=right,2=down,3=left required.":null;
        if(error!=null){reject=new(payload.CommandId,payload.TaskId,"rejected",0,0,1,revision(),new(),Error:new("FISHING_PRECONDITION",error,null,false),SkillId:"fish");return false;}
        p.faceDirection(payload.Parameters.Facing!.Value);
        request=payload;started=false;IsPaused=false;deadline=DateTime.UtcNow.AddSeconds(150);
        before=Totals(p);actor.Halt();actor.SetActiveTask(payload.TaskId);inputOwner=this;
        p.CurrentToolIndex=p.Items.IndexOf(rod);
        var offset=p.FacingDirection switch {0=>new Point(0,-1),1=>new Point(1,0),2=>new Point(0,1),_=>new Point(-1,0)};
        p.lastClick=new Vector2((p.TilePoint.X+offset.X)*64+32,(p.TilePoint.Y+offset.Y)*64+32);
        p.BeginUsingTool();return true;
    }
    private static Dictionary<string,int> Totals(Farmer p)=>p.Items.Where(i=>i!=null).GroupBy(i=>i.QualifiedItemId).ToDictionary(g=>g.Key,g=>g.Sum(i=>i.Stack));
    private bool OwnInput=>request!=null&&!IsPaused&&actor.BindingValid&&owns()&&ReferenceEquals(actor.GameFarmer?.CurrentTool,rod);
    private bool Held
    {
        get
        {
            if(!OwnInput||rod==null)return false;
            if(Game1.activeClickableMenu is BobberBar bar)
            {
                // Visible minigame position/velocity only; never alter catch
                // progress or fish position. Physics still determines success.
                float center=bar.bobberBarPos+bar.bobberBarHeight/2f;
                return center+bar.bobberBarSpeed*6f>bar.bobberPosition+6f;
            }
            if(rod.isTimingCast)return rod.castingPower<0.72f;
            return rod.isNibbling||rod.fishCaught;
        }
    }
    private static ButtonState NativeLeft(ref MouseState state)=>inputOwner?.OwnInput==true?(inputOwner.Held?ButtonState.Pressed:ButtonState.Released):state.LeftButton;
    private static bool NativeClick(bool useToolOnly)=>inputOwner?.OwnInput==true?inputOwner.Held:Game1.didPlayerJustClickAtAll(useToolOnly);
    private static IEnumerable<CodeInstruction> InputBoundary(IEnumerable<CodeInstruction> instructions)
    {
        var left=AccessTools.PropertyGetter(typeof(MouseState),nameof(MouseState.LeftButton));
        var click=AccessTools.Method(typeof(Game1),nameof(Game1.didPlayerJustClickAtAll));
        foreach(var i in instructions)
        {
            if(i.Calls(left)){i.opcode=OpCodes.Call;i.operand=AccessTools.Method(typeof(PlayerFishingMachine),nameof(NativeLeft));}
            else if(i.Calls(click)){i.opcode=OpCodes.Call;i.operand=AccessTools.Method(typeof(PlayerFishingMachine),nameof(NativeClick));}
            yield return i;
        }
    }
    public void Update(GameTime? time,long tickCount)
    {
        if(request==null||IsPaused)return;
        if(!actor.BindingValid||!owns()){RequestCancel("Player authority or binding ended.");return;}
        if(DateTime.UtcNow>deadline){AbortNative();Finish("failed","FISHING_TIMEOUT","Native cast exceeded its bounded window; no catch inferred.");return;}
        var p=actor.GameFarmer ?? throw new InvalidOperationException("Player binding lost.");
        if(Game1.activeClickableMenu is ItemGrabMenu && started)
        {
            var caught=Totals(p).Where(x=>x.Value>before.GetValueOrDefault(x.Key)).Select(x=>new Dictionary<string,object>{["kind"]="native-fishing-inventory-gain",["itemId"]=x.Key,["count"]=x.Value-before.GetValueOrDefault(x.Key)}).ToList();
            Finish("succeeded",null,"Native cast reached item collection menu; inspect its actual choices and use native_menu_select, not a fabricated treasure transfer.",caught);return;
        }
        if(p.UsingTool||rod?.inUse()==true||Game1.activeClickableMenu is BobberBar){started=true;return;}
        if(!started)return;
        if(Game1.activeClickableMenu!=null)return;
        var gained=Totals(p).Where(x=>x.Value>before.GetValueOrDefault(x.Key)).Select(x=>new Dictionary<string,object>{["kind"]="native-fishing-inventory-gain",["itemId"]=x.Key,["count"]=x.Value-before.GetValueOrDefault(x.Key)}).ToList();
        Finish("succeeded",null,gained.Count>0?"Native cast settled in player inventory.":"Native cast ended without inventory gain; no fish success claimed.",gained);
    }
    private void AbortNative(){if(Game1.activeClickableMenu is BobberBar b)b.emergencyShutDown();else rod?.doneFishing(actor.GameFarmer,true);}
    private void Finish(string status,string? code,string message,List<Dictionary<string,object>>? gains=null)
    {
        var r=request;if(r==null)return;request=null;inputOwner=null;actor.SetActiveTask(null);
        Completed?.Invoke(new(r.CommandId,r.TaskId,status,status=="succeeded"?1:0,0,status=="failed"?1:0,revision(),gains??new(),Error:code==null?null:new(code,message,null,false),SkillId:"fish",Details:new Dictionary<string,object>{["message"]=message,["nativeSettlement"]=true}));
    }
    public void RequestCancel(string reason){AbortNative();Finish("cancelled","CANCELLED",reason);}
    public void RequestPause(){IsPaused=true;}
    public void Resume(){IsPaused=false;}
}
