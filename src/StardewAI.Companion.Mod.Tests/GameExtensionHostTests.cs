using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Xna.Framework;
using StardewAI.Companion.Mod.Extensions;
using StardewAI.Companion.Mod.Domain;
using Xunit;
namespace StardewAI.Companion.Mod.Tests;
public class GameExtensionHostTests
{
    [Fact]
    public void ByteLoadedHostAndModApiRecoverCompileReferencesWithoutUsingGeneratedPackages()
    {
        string root=Path.Combine(Path.GetTempPath(),"star-byte-api-"+Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try {
            string hostPath=Path.Combine(root,"StardewAI.Companion.Mod.dll");
            File.Copy(typeof(IGameExtension).Assembly.Location,hostPath);
            var hostAssembly=System.Reflection.Assembly.Load(File.ReadAllBytes(hostPath));
            Assert.Empty(hostAssembly.Location);
            string modFolder=Path.Combine(root,"ExampleMod"); Directory.CreateDirectory(modFolder);
            string name="ByteLoadedApi"+Guid.NewGuid().ToString("N");
            string apiPath=Path.Combine(modFolder,name+".dll");
            WriteApi(name,apiPath);
            var apiAssembly=System.Reflection.Assembly.Load(File.ReadAllBytes(apiPath));
            Assert.Empty(apiAssembly.Location);
            string excludedFolder=Path.Combine(root,"Companion","data","abilities","packages","draft"); Directory.CreateDirectory(excludedFolder);
            string excludedName="GeneratedOnly"+Guid.NewGuid().ToString("N");
            string excludedPath=Path.Combine(excludedFolder,excludedName+".dll"); WriteApi(excludedName,excludedPath);
            var excludedAssembly=System.Reflection.Assembly.Load(File.ReadAllBytes(excludedPath));
            var resolver=new NativeAssemblyReferenceResolver(root,hostPath);
            var result=resolver.Resolve(new[] { hostAssembly,apiAssembly,excludedAssembly,typeof(object).Assembly });
            Assert.Contains(hostPath,result.References); Assert.Contains(apiPath,result.References);
            Assert.DoesNotContain(excludedPath,result.References); Assert.DoesNotContain(typeof(object).Assembly.Location,result.References);
            Assert.Equal(excludedAssembly.FullName,Assert.Single(result.Missing));
            Assert.Equal(result.References,resolver.Resolve(new[] {hostAssembly,apiAssembly}).References);
        } finally { Directory.Delete(root,true); }
        static void WriteApi(string name,string path)
        {
            using var dll=Mono.Cecil.AssemblyDefinition.CreateAssembly(new Mono.Cecil.AssemblyNameDefinition(name,new Version(1,0,0,0)),name,Mono.Cecil.ModuleKind.Dll);
            dll.MainModule.Types.Add(new Mono.Cecil.TypeDefinition("Example","Dto",Mono.Cecil.TypeAttributes.Public|Mono.Cecil.TypeAttributes.Class,dll.MainModule.ImportReference(typeof(object))));
            dll.Write(path);
        }
    }
    [Fact]
    public void IsolatedModApiTypesAreDiscoveredAndSharedWithExactAssemblyIdentity()
    {
        string root=Path.Combine(Path.GetTempPath(),"star-isolated-api-"+Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        Exercise(root);
        GC.Collect(); GC.WaitForPendingFinalizers(); GC.Collect();
        Directory.Delete(root,true);
        [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
        static void Exercise(string root)
        {
        var modContext=new System.Runtime.Loader.AssemblyLoadContext("independent-mod",isCollectible:true);
        var adapterContext=new GameExtensionHost.AdapterContext();
        try {
            string name="IsolatedModApi"+Guid.NewGuid().ToString("N");
            string path=Path.Combine(root,name+".dll");
            using (var dll=Mono.Cecil.AssemblyDefinition.CreateAssembly(new Mono.Cecil.AssemblyNameDefinition(name,new Version(1,2,3,4)),name,Mono.Cecil.ModuleKind.Dll)) {
                var dto=new Mono.Cecil.TypeDefinition("IsolatedMod","PublicDto",Mono.Cecil.TypeAttributes.Public|Mono.Cecil.TypeAttributes.Class,dll.MainModule.ImportReference(typeof(object)));
                dto.Fields.Add(new Mono.Cecil.FieldDefinition("Message",Mono.Cecil.FieldAttributes.Public,dll.MainModule.ImportReference(typeof(string))));
                dll.MainModule.Types.Add(dto); dll.Write(path);
            }
            var assembly=modContext.LoadFromAssemblyPath(path);
            using var host=new GameExtensionHost(Path.Combine(root,"packages"),System.Reflection.DispatchProxy.Create<StardewModdingAPI.IModRegistry,EmptyRegistry>());
            var discovery=JsonSerializer.SerializeToNode(host.Discover(null,"IsolatedMod.PublicDto"))!;
            Assert.Equal("IsolatedMod.PublicDto",Assert.Single(discovery["nativeTypes"]!.AsArray())!["name"]!.GetValue<string>());
            Assert.Contains(discovery["authoring"]!["references"]!.AsArray(),node=>node!.GetValue<string>()==path);
            Assert.Same(assembly,adapterContext.LoadFromAssemblyName(assembly.GetName()));
            var wrongVersion=new GameExtensionHost.AdapterContext();
            try {
                var requested=assembly.GetName(); requested.Version=new Version(9,0,0,0);
                Assert.Throws<System.IO.FileNotFoundException>(()=>wrongVersion.LoadFromAssemblyName(requested));
            } finally { wrongVersion.Unload(); }
        } finally { adapterContext.Unload(); modContext.Unload(); }
        }
    }
    [Fact]
    public void FurnitureDiscoveryFitsTransportAndEveryDeclaredMemberRemainsReachable()
    {
        string root=Path.Combine(Path.GetTempPath(),"star-discovery-"+Guid.NewGuid().ToString("N"));
        try {
            var registry=System.Reflection.DispatchProxy.Create<StardewModdingAPI.IModRegistry,EmptyRegistry>();
            using var host=new GameExtensionHost(root,registry);
            string authoring=JsonSerializer.Serialize(host.Discover(null,null));
            Assert.True(System.Text.Encoding.UTF8.GetByteCount(authoring)<60000);
            Assert.Empty(JsonNode.Parse(authoring)!["nativeTypes"]!.AsArray());
            var vector=JsonSerializer.SerializeToNode(host.Discover(null,"Microsoft.Xna.Framework.Vector2"))!;
            Assert.Equal("Microsoft.Xna.Framework.Vector2",Assert.Single(vector["nativeTypes"]!.AsArray())!["name"]!.GetValue<string>());
            var members=new List<string>(); int offset=0; bool more;
            do {
                string json=JsonSerializer.Serialize(host.Discover(null,"Furniture",offset:offset));
                Assert.True(System.Text.Encoding.UTF8.GetByteCount(json)<60000,"Discovery must fit the 64KB transport envelope.");
                var result=JsonNode.Parse(json)!;
                var type=Assert.Single(result["nativeTypes"]!.AsArray());
                Assert.Equal("StardewValley.Objects.Furniture",type!["name"]!.GetValue<string>());
                members.AddRange(type["members"]!.AsArray().Select(x=>x!.GetValue<string>()));
                more=type["hasMore"]!.GetValue<bool>(); offset+=20;
            } while(more);
            var expected=typeof(StardewValley.Objects.Furniture).GetMembers(System.Reflection.BindingFlags.Instance|System.Reflection.BindingFlags.Static|System.Reflection.BindingFlags.Public|System.Reflection.BindingFlags.DeclaredOnly)
                .Where(m=>m is System.Reflection.MethodInfo or System.Reflection.PropertyInfo or System.Reflection.FieldInfo or System.Reflection.ConstructorInfo).Select(m=>m.ToString()).OrderBy(x=>x,StringComparer.Ordinal).ToArray();
            Assert.Equal(expected,members);
            var filtered=JsonSerializer.SerializeToNode(host.Discover(null,"Furniture","rotate"))!;
            Assert.Contains(filtered["nativeTypes"]![0]!["members"]!.AsArray(),x=>x!.GetValue<string>().Contains("rotate",StringComparison.OrdinalIgnoreCase));
            var fuzzy=JsonSerializer.SerializeToNode(host.Discover(null,"Furnit"))!;
            Assert.Empty(fuzzy["nativeTypes"]!.AsArray()); Assert.NotEmpty(fuzzy["candidateTypes"]!.AsArray());
            var references=filtered["authoring"]!["references"]!.AsArray().Select(x=>x!.GetValue<string>()).ToArray();
            Assert.DoesNotContain(typeof(object).Assembly.Location,references);
            Assert.Contains(typeof(StardewValley.Objects.Furniture).Assembly.Location,references);
            Assert.Contains(typeof(IGameExtension).Assembly.Location,references);
        } finally { Directory.Delete(root,true); }
    }
    public class EmptyRegistry : System.Reflection.DispatchProxy
    {
        protected override object? Invoke(System.Reflection.MethodInfo? method,object?[]? args) => method?.Name == "GetAll" ? Array.Empty<StardewModdingAPI.IModInfo>() : null;
    }
    [Fact]
    public void FailedReplacementAndUnpersistedTrialPreserveApprovedCatalog()
    {
        string root = Path.Combine(Path.GetTempPath(), "star-extension-" + Guid.NewGuid().ToString("N"));
        try {
            Package(root,"v1",typeof(WorkingExtension).FullName!);
            Package(root,"v2",typeof(ThrowingExtension).FullName!);
            using (var host = new GameExtensionHost(root,null!)) {
                host.Load("v1");
                host.Load("v1"); // Keeping an already-loaded immutable version must not re-run its constructor.
                Assert.ThrowsAny<Exception>(() => host.Load("v2"));
                Assert.Equal("v1",Assert.Single(host.List()).Version);
                Package(root,"v3",typeof(WorkingExtension).FullName!);
                host.Load("v3",persist:false);
                Assert.Equal("v3",Assert.Single(host.List()).Version);
            }
            using var restored = new GameExtensionHost(root,null!);
            Assert.Equal("v1",Assert.Single(restored.List()).Version);
        } finally { Directory.Delete(root,true); }
    }
    [Fact]
    public void ReplacementAndShutdownDisposeGeneratedAdaptersEvenWhenCleanupThrows()
    {
        string root=Path.Combine(Path.GetTempPath(),"star-dispose-extension-"+Guid.NewGuid().ToString("N"));
        try {
            Package(root,"v1",typeof(DisposalExtension).FullName!);
            Package(root,"v2",typeof(DisposalExtension).FullName!);
            var host=new GameExtensionHost(root,null!);
            host.Load("v1",false); host.Load("v2",false);
            Assert.Single(host.CleanupErrors);
            Assert.Equal("v2",Assert.Single(host.List()).Version);
            host.Dispose(); Assert.Equal(2,host.CleanupErrors.Count); Assert.Empty(host.List());
        } finally { Directory.Delete(root,true); }
    }
    [Fact]
    public void HashMismatchAndKindMismatchPreventExecution()
    {
        string root = Path.Combine(Path.GetTempPath(),"star-extension-"+Guid.NewGuid().ToString("N"));
        try {
            Package(root,"v1",typeof(WorkingExtension).FullName!);
            using var host = new GameExtensionHost(root,null!); host.Load("v1");
            Assert.Throws<InvalidOperationException>(()=>host.Begin("test","v1","action",null!,new()));
            File.AppendAllText(Path.Combine(root,"v1","adapter.dll"),"tamper");
            Assert.Throws<InvalidDataException>(()=>host.Load("v1"));
            Assert.Single(host.List());
            Assert.Throws<InvalidDataException>(()=>host.Load("../escape"));
        } finally { Directory.Delete(root,true); }
    }
    [Fact]
    public void CancellationReleasesActorAndDisposesEvenWhenExtensionCleanupThrows()
    {
        var actor = new MechanicsActor("test"); var execution = new BadCleanup();
        var machine = new ExtensionExecutionMachine(actor,()=>true); string? state=null;
        machine.Start("task",token=> { token.Register(()=>throw new InvalidOperationException("callback failure")); return execution; },(s,_)=>state=s);
        machine.RequestPause(); machine.RequestCancel("player takeover"); machine.Update(new GameTime(),1);
        Assert.False(machine.IsExecuting); Assert.Null(actor.ActiveTaskId); Assert.True(execution.Disposed);
        Assert.Equal("failed",state); Assert.Equal(0,execution.Ticks);
    }
    [Fact]
    public void MalformedArgumentsAreRejectedBeforeGeneratedCode()
    {
        var schema=JsonNode.Parse("{\"type\":\"object\",\"required\":[\"count\"],\"additionalProperties\":false,\"properties\":{\"count\":{\"type\":\"integer\",\"minimum\":1}}}")!.AsObject();
        Assert.Throws<ArgumentException>(()=>ExtensionInputValidation.Validate(schema,new JsonObject()));
        Assert.Throws<ArgumentException>(()=>ExtensionInputValidation.Validate(schema,new JsonObject { ["count"]=-1 }));
        Assert.Throws<ArgumentException>(()=>ExtensionInputValidation.Validate(schema,new JsonObject { ["count"]=1,["actor"]="other" }));
    }
    private static void Package(string root,string version,string entry)
    {
        string dir=Path.Combine(root,version); Directory.CreateDirectory(dir);
        string dll=typeof(WorkingExtension).Assembly.Location; File.Copy(dll,Path.Combine(dir,"adapter.dll"),true);
        string hash=Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(File.ReadAllBytes(dll))).ToLowerInvariant();
        File.WriteAllText(Path.Combine(dir,"manifest.json"),JsonSerializer.Serialize(new ExtensionManifest("test",version,"test","query",entry,"adapter.dll",new JsonObject { ["type"]="object" },"skill.md",Array.Empty<ExtensionDependency>(),hash)));
    }
    public sealed class WorkingExtension : IGameExtension { private static int instances; public WorkingExtension() { if (++instances > 1) throw new InvalidOperationException("constructor repeated"); } public IExtensionExecution Begin(ExtensionContext c,JsonObject a)=>new BadCleanup(); }
    public sealed class DisposalExtension : IGameExtension, IDisposable { public IExtensionExecution Begin(ExtensionContext c, JsonObject a)=>new BadCleanup(); public void Dispose()=>throw new InvalidOperationException("disposed"); }
    public sealed class ThrowingExtension : IGameExtension { public ThrowingExtension()=>throw new InvalidOperationException("bad constructor"); public IExtensionExecution Begin(ExtensionContext c,JsonObject a)=>throw new Exception(); }
    public sealed class BadCleanup : IExtensionExecution { public bool Disposed; public int Ticks; public ExtensionOutcome? Tick(GameTime t) { Ticks++; return null; } public void Cancel()=>throw new Exception("cancel"); public void Dispose() { Disposed=true; throw new Exception("dispose"); } }
}
