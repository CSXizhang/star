using System.Text.Json.Nodes;
using StardewAI.Companion.Mod.Extensions;
using StardewAI.Companion.Mod.Transport;
using StardewAI.Companion.Mod.Domain;
using StardewModdingAPI;
namespace StardewAI.Companion.Mod.Execution;
public sealed partial class CompanionMechanicsCoordinator
{
    private GameExtensionHost? extensionHost;
    private IModRegistry? extensionRegistry;
    private ExtensionExecutionMachine? extensionMachine;
    public void ShutdownExtensions() { extensionMachine?.RequestCancel("Game exited."); extensionHost?.Dispose(); extensionHost = null; }
    public void ConfigureExtensions(string packagesRoot, IModRegistry registry, string? modDirectory = null)
    {
        extensionMachine?.RequestCancel("Extension host replaced."); extensionHost?.Dispose();
        extensionRegistry = registry; extensionHost = new(packagesRoot, registry,
            modDirectory == null ? null : ConfiguredModsPath(modDirectory), modDirectory == null ? null : Path.Combine(modDirectory,"StardewAI.Companion.Mod.dll"));
        extensionMachine = new(_actor, () => ConnectionActionsAllowed?.Invoke() != false
            && PresetBusinessAllowed?.Invoke() != false && (_actor is not PlayerActor player || player.BindingValid && PlayerAuthority?.AgentOwns == true));
    }
    private static string? ConfiguredModsPath(string modDirectory)
    {
        // SMAPI 4.2 exposes the actual --mods-path only as an internal property.
        // Read that one configuration value; never inspect or execute Mod internals.
        try {
            if (typeof(Constants).GetProperty("ModsPath",System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic)?.GetValue(null) is string path && Directory.Exists(path)) return path;
        } catch { }
        return Path.GetDirectoryName(modDirectory);
    }
    private object LoadExtensionReceipt(WaterZoneParameters parameters) {
        var m = extensionHost!.Load(parameters.ExtensionPackage ?? throw new ArgumentException("extensionPackage required."), parameters.ExtensionPersist);
        return new { id=m.Id, version=m.Version, sha256=m.Sha256, loaded=true, persisted=parameters.ExtensionPersist, verified=false, manifest=m };
    }
    private bool TryAcceptExtension(SkillExecutePayload payload, EnvelopeDto envelope, out SkillResultPayload? reject)
    {
        reject = null;
        if (extensionHost == null) { reject = BuildRejection(payload,"EXTENSION_HOST_UNAVAILABLE","Native extension host is unavailable.",false); return false; }
        var p = payload.Parameters;
        try
        {
            // All extension commands share the receipt slot; preserve its active owner.
            if (!TryCheckIdle(out var busy)) { reject = BuildRejection(payload,"ACTOR_BUSY",busy!,true); return false; }
            if (_cancelledTaskIds.Contains(payload.CommandId) || _cancelledTaskIds.Contains(payload.TaskId)) throw new InvalidOperationException("Command was cancelled before execution.");
            _currentExecuteEnvelope = envelope; _currentExecuteCommandId = payload.CommandId;
            if (payload.SkillId == "invoke-extension")
            {
                string id = p.ExtensionId ?? throw new ArgumentException("extensionId required.");
                string kind = p.ExtensionKind ?? throw new ArgumentException("extensionKind required.");
                extensionMachine!.Start(payload.TaskId, token => extensionHost.Begin(id,p.ExtensionVersion,kind,new(_actor,_observer,extensionRegistry!,token),p.ExtensionArguments ?? new()),
                    (state, outcome) => CompleteExecution(new(payload.CommandId,payload.TaskId,state,outcome.Succeeded?1:0,0,state=="failed"?1:0,_observer.WorldRevision,new(),
                        Error:outcome.Error == null ? null : new("EXTENSION_EXECUTION_FAILED",outcome.Error),SkillId:payload.SkillId,
                        Details:new() { ["id"]=id, ["version"]=extensionHost.List().Single(x=>x.Id==id).Version,
                            ["sha256"]=extensionHost.List().Single(x=>x.Id==id).Sha256, ["succeeded"]=outcome.Succeeded,
                            ["saveId"]=envelope.SaveId ?? "", ["gameSessionId"]=envelope.GameSessionId ?? "", ["worldRevision"]=_observer.WorldRevision,
                            ["evidence"]=outcome.Evidence, ["actorId"]=_actor.CompanionId, ["location"]=_actor.LocationName, ["inventory"]=_actor.GetInventorySnapshot() })));
                return true;
            }
            object data = payload.SkillId switch {
                "discover-extension-api" => extensionHost.Discover(p.ExtensionModId,p.ExtensionType,p.ExtensionMember,p.ExtensionOffset,p.ExtensionLimit),
                "list-extensions" => new { extensions=extensionHost.List(),restoreErrors=extensionHost.RestoreErrors,cleanupErrors=extensionHost.CleanupErrors },
                "load-extension" => LoadExtensionReceipt(p),
                _ => throw new ArgumentException("Unknown extension command.")
            };
            CompleteExecution(new(payload.CommandId,payload.TaskId,"succeeded",1,0,0,_observer.WorldRevision,new(),SkillId:payload.SkillId,Details:new() { ["extension"]=data }));
            return true;
        }
        catch (Exception ex) { reject=BuildRejection(payload,"EXTENSION_UNAVAILABLE",ex.Message,false); return false; }
    }
}
