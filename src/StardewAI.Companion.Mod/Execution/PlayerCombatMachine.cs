using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Tools;
using StardewValley.Monsters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Transport;
namespace StardewAI.Companion.Mod.Execution;

/// A single native weapon swing, never direct monster damage or fake drops.
public sealed class PlayerCombatMachine : ISkillExecutionMachine
{
    private readonly PlayerActor actor;private readonly Func<bool> owns;private readonly Func<int> revision;
    private SkillExecutePayload? request;private DateTime deadline;private Dictionary<Monster,int> health=new();
    public PlayerCombatMachine(PlayerActor actor,Func<bool> owns,Func<int> revision){this.actor=actor;this.owns=owns;this.revision=revision;}
    public event Action<SkillResultPayload>? Completed;
    public bool IsExecuting=>request!=null;public bool IsPaused{get;private set;}public int TotalTargets=>1;public int CurrentTargetIndex=>0;public string? ActiveTaskId=>request?.TaskId;
    public bool Start(SkillExecutePayload payload,out SkillResultPayload? reject)
    {
        reject=null;var p=actor.GameFarmer??throw new InvalidOperationException("Player binding lost.");
        string? error=!p.Items.OfType<MeleeWeapon>().Any(w=>!w.isScythe())?"No genuine melee weapon is owned; obtain one through native mine story/shop first.":payload.Parameters.Facing is not int f||f<0||f>3?"Explicit facing 0..3 required.":null;
        if(error!=null){reject=new(payload.CommandId,payload.TaskId,"rejected",0,0,1,revision(),new(),Error:new("COMBAT_PRECONDITION",error,null,false),SkillId:"attack");return false;}
        health=p.currentLocation.characters.OfType<Monster>().Where(m=>Vector2.Distance(m.Position,p.Position)<=192).ToDictionary(m=>m,m=>m.Health);
        request=payload;IsPaused=false;deadline=DateTime.UtcNow.AddSeconds(8);p.faceDirection(payload.Parameters.Facing!.Value);actor.SetActiveTask(payload.TaskId);actor.BeginUsingTool("Weapon");return true;
    }
    public void Update(GameTime? time,long tickCount)
    {
        if(request==null||IsPaused)return;
        if(!actor.BindingValid||!owns()){RequestCancel("Player authority ended.");return;}
        if(DateTime.UtcNow>deadline){Finish("failed","COMBAT_ANIMATION_TIMEOUT","Native weapon animation did not settle.");return;}
        if(actor.UpdateToolAnimation(time,tickCount)!=ToolAnimationPhase.Completed)return;
        var effects=health.Where(x=>x.Key.Health<x.Value).Select(x=>new Dictionary<string,object>{["kind"]="native-weapon-damage-observed",["monster"]=x.Key.Name,["healthBefore"]=x.Value,["healthAfter"]=Math.Max(0,x.Key.Health)}).ToList();
        Finish("succeeded",null,effects.Count>0?"Native swing damaged observed monster(s).":"Native swing completed without confirmed damage.",effects);
    }
    private void Finish(string status,string? code,string message,List<Dictionary<string,object>>? effects=null){var r=request;if(r==null)return;request=null;actor.SetActiveTask(null);Completed?.Invoke(new(r.CommandId,r.TaskId,status,status=="succeeded"?1:0,0,status=="failed"?1:0,revision(),effects??new(),Error:code==null?null:new(code,message,null,false),SkillId:"attack",Details:new Dictionary<string,object>{["message"]=message}));}
    public void RequestCancel(string reason){Finish("cancelled","CANCELLED",reason);}
    public void RequestPause(){IsPaused=true;}public void Resume(){IsPaused=false;}
}
