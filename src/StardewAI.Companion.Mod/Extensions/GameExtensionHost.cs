using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;
using System.Text.Json.Nodes;
using StardewModdingAPI;
using StardewValley;
namespace StardewAI.Companion.Mod.Extensions;

/// <summary>Versioned adapter host. Assemblies share native SDK identity, never reload SMAPI Mods.</summary>
public sealed class GameExtensionHost : IDisposable
{
    internal sealed class AdapterContext : AssemblyLoadContext
    {
        public AdapterContext() : base(isCollectible: true) { }
        protected override Assembly? Load(AssemblyName name) => SharedAssemblies().FirstOrDefault(a => string.Equals(a.GetName().FullName, name.FullName, StringComparison.OrdinalIgnoreCase));
    }
    private static IEnumerable<Assembly> SharedAssemblies() => AppDomain.CurrentDomain.GetAssemblies()
        .Where(a => !a.IsDynamic && AssemblyLoadContext.GetLoadContext(a) is not AdapterContext)
        .GroupBy(a => a.GetName().FullName, StringComparer.OrdinalIgnoreCase).Select(group => group.First());
    private sealed record Loaded(ExtensionManifest Manifest, IGameExtension Adapter, AdapterContext Context, string Package);
    private readonly string root;
    private readonly IModRegistry registry;
    private readonly NativeAssemblyReferenceResolver references;
    private readonly Dictionary<string, Loaded> loaded = new(StringComparer.Ordinal);
    private readonly Dictionary<string,string> persisted = new(StringComparer.Ordinal);
    private readonly int threadId = Environment.CurrentManagedThreadId;
    private static readonly JsonSerializerOptions Json = new() { PropertyNameCaseInsensitive = true, PropertyNamingPolicy = JsonNamingPolicy.CamelCase, WriteIndented = true };
    public List<string> CleanupErrors { get; } = new();
    public List<string> RestoreErrors { get; } = new();
    public GameExtensionHost(string packageRoot, IModRegistry registry, string? modsRoot = null, string? hostAssemblyPath = null)
    {
        root = Path.GetFullPath(packageRoot); this.registry = registry;
        references = new(modsRoot, hostAssemblyPath);
        Directory.CreateDirectory(root);
        string catalog = Path.Combine(root, "active.json");
        if (File.Exists(catalog))
        {
            try { foreach (string package in JsonSerializer.Deserialize<string[]>(File.ReadAllText(catalog), Json) ?? Array.Empty<string>())
                try { var restored = Load(package, persist: false); persisted[restored.Id] = package; } catch (Exception ex) { RestoreErrors.Add($"{package}: {ex.Message}"); } }
            catch (Exception ex) { RestoreErrors.Add(ex.Message); }
        }
    }
    private void AssertThread() { if (Environment.CurrentManagedThreadId != threadId) throw new InvalidOperationException("Extensions require the game thread."); }
    public ExtensionManifest[] List() => loaded.Values.Select(x => x.Manifest).ToArray();
    public ExtensionManifest Load(string package, bool persist = true)
    {
        AssertThread();
        string directory = ResolveContained(root, package);
        var manifest = JsonSerializer.Deserialize<ExtensionManifest>(File.ReadAllText(Path.Combine(directory, "manifest.json")), Json)
            ?? throw new InvalidDataException("Missing extension manifest.");
        if (string.IsNullOrWhiteSpace(manifest.Id) || string.IsNullOrWhiteSpace(manifest.Version) || manifest.Kind is not ("query" or "action") || manifest.InputSchema == null || manifest.ContractVersion != 1)
            throw new InvalidDataException("Manifest requires id, version, query/action kind and inputSchema.");
        foreach (var dependency in manifest.Dependencies ?? Array.Empty<ExtensionDependency>())
            if (registry.Get(dependency.Id)?.Manifest.Version.ToString() != dependency.Version)
                throw new InvalidOperationException($"Dependency {dependency.Id} requires version {dependency.Version}.");
        var context = new AdapterContext();
        Loaded? candidate = null;
        try
        {
            string dll = ResolveContained(directory, manifest.Assembly);
            using var stream = File.OpenRead(dll);
            string hash = Convert.ToHexString(System.Security.Cryptography.SHA256.Create().ComputeHash(stream)).ToLowerInvariant();
            if (!string.Equals(hash, manifest.Sha256, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Extension assembly hash does not match manifest.");
            if (loaded.TryGetValue(manifest.Id, out var existing) && existing.Manifest.Version == manifest.Version && existing.Manifest.Sha256 == manifest.Sha256)
            {
                if (JsonSerializer.Serialize(existing.Manifest, Json) != JsonSerializer.Serialize(manifest, Json)) throw new InvalidDataException("An immutable extension manifest changed without a version change.");
                if (persist) Persist(manifest.Id, package);
                context.Unload();
                return existing.Manifest;
            }
            stream.Position = 0;
            var assembly = context.LoadFromStream(stream);
            var type = assembly.GetType(manifest.EntryType, true)!;
            if (!typeof(IGameExtension).IsAssignableFrom(type) || type.IsAbstract) throw new InvalidDataException("Entry type must implement IGameExtension.");
            candidate = new(manifest, (IGameExtension)Activator.CreateInstance(type)!, context, package);
            loaded.TryGetValue(manifest.Id, out var previous);
            if (persist) Persist(manifest.Id, package);
            loaded[manifest.Id] = candidate;
            if (previous != null) Release(previous);
            return manifest;
        }
        catch { if (candidate != null) Release(candidate); else context.Unload(); throw; }
    }
    private void Persist(string id, string package)
    {
        var paths = persisted.Where(x => x.Key != id).Select(x => x.Value).Append(package).ToArray();
        string temp = Path.Combine(root, "active.json.tmp");
        File.WriteAllText(temp, JsonSerializer.Serialize(paths, Json));
        File.Move(temp, Path.Combine(root, "active.json"), true);
        persisted[id] = package;
    }
    public IExtensionExecution Begin(string id, string? version, string kind, ExtensionContext context, JsonObject arguments)
    {
        AssertThread();
        if (!loaded.TryGetValue(id, out var item)) throw new InvalidOperationException("Extension is not loaded.");
        if (version != null && item.Manifest.Version != version) throw new InvalidOperationException("Extension version changed; inspect the current catalog.");
        if (item.Manifest.Kind != kind) throw new InvalidOperationException("Extension invocation kind does not match its manifest.");
        ExtensionInputValidation.Validate(item.Manifest.InputSchema, arguments);
        context.CancellationToken.ThrowIfCancellationRequested();
        return item.Adapter.Begin(context, arguments) ?? throw new InvalidOperationException("Extension returned no execution.");
    }
    public static string ResolveContained(string parent, string relative)
    {
        if (string.IsNullOrWhiteSpace(relative) || Path.IsPathRooted(relative)) throw new InvalidDataException("Package paths must be relative.");
        string prefix = Path.GetFullPath(parent).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        string path = Path.GetFullPath(Path.Combine(parent, relative));
        if (!path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Package path leaves its root.");
        for (string? p = path; p != null && p.StartsWith(prefix, StringComparison.OrdinalIgnoreCase); p = Path.GetDirectoryName(p))
            if ((File.Exists(p) || Directory.Exists(p)) && (File.GetAttributes(p) & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("Linked package paths are unsupported.");
        return path;
    }
    public object Discover(string? modId, string? typeName, string? member = null, int offset = 0, int limit = 20)
    {
        AssertThread();
        if (offset < 0 || limit < 1 || limit > 40) throw new ArgumentException("Discovery offset must be nonnegative and limit must be 1..40.");
        var selectedMods = registry.GetAll().Where(m => modId == null || m.Manifest.UniqueID.Equals(modId, StringComparison.OrdinalIgnoreCase)).OrderBy(m=>m.Manifest.UniqueID).ToArray();
        var mods = (modId == null ? selectedMods.Skip(offset).Take(limit) : selectedMods).Select(m => {
            object? api = null; string? error = null; try { api = registry.GetApi(m.Manifest.UniqueID); } catch (Exception ex) { error = ex.Message; }
            return new { id = m.Manifest.UniqueID, version = m.Manifest.Version.ToString(), apiType = api?.GetType().FullName, error,
                api = api == null || modId == null ? null : DescribeType(api.GetType(), member, offset, limit) };
        }).ToArray();
        var assemblies = SharedAssemblies().ToArray();
        var candidates = assemblies
            .SelectMany(SafeTypes).Where(t => (t.IsPublic || t.IsNestedPublic) && (typeName == null ? t == typeof(Farmer) || t == typeof(GameLocation) || t == typeof(StardewValley.Object) : (t.FullName ?? t.Name).Contains(typeName, StringComparison.OrdinalIgnoreCase)))
            .OrderBy(t => t.FullName).ToArray();
        var exact = candidates.Where(t => t.FullName?.Equals(typeName,StringComparison.OrdinalIgnoreCase) == true).ToArray();
        if (exact.Length == 0) exact = candidates.Where(t => t.Name.Equals(typeName,StringComparison.OrdinalIgnoreCase)).ToArray();
        // Ambiguous short names must be refined; never silently select a different native type.
        var types = exact.Length == 1 ? new[] { DescribeType(exact[0],member,offset,limit) } : Array.Empty<object>();
        var compileReferences = references.Resolve(assemblies);
        return new { mods, modsTotal=selectedMods.Length, modsHasMore=modId == null && offset+mods.Length < selectedMods.Length,
            nativeTypes = types, candidateTypes = types.Length == 0 ? candidates.Skip(offset).Take(limit).Select(t=>t.FullName).ToArray() : Array.Empty<string>(),
            candidatesTotal=candidates.Length, candidatesHasMore=types.Length==0 && offset+limit<candidates.Length, offset, limit,
            authoring = new { targetFramework = "net6.0", contractVersion = 1,
            hostAssembly = compileReferences.References.FirstOrDefault(path => string.Equals(AssemblyName.GetAssemblyName(path).FullName,typeof(IGameExtension).Assembly.GetName().FullName,StringComparison.OrdinalIgnoreCase)),
            example = "using System.Text.Json.Nodes; using Microsoft.Xna.Framework; using StardewAI.Companion.Mod.Extensions; public sealed class Adapter : IGameExtension { public IExtensionExecution Begin(ExtensionContext c, JsonObject a) => new Run(c); sealed class Run : IExtensionExecution { readonly ExtensionContext c; public Run(ExtensionContext c) { this.c=c; } public ExtensionOutcome? Tick(GameTime time) { c.CancellationToken.ThrowIfCancellationRequested(); return new ExtensionOutcome(true, new JsonObject { [\"location\"] = c.Actor.LocationName }); } public void Cancel() {} public void Dispose() {} } } For actions, perform one bounded native step each Tick and return null until observed completion; Cancel/Dispose detach events and stop owned movement.",
            references = compileReferences.References, missingReferences = compileReferences.Missing,
            contract = "IGameExtension.Begin(ExtensionContext, JsonObject) returns IExtensionExecution. Tick(GameTime) returns null while running or ExtensionOutcome(bool Succeeded, JsonObject Evidence, string? Error=null). Context: Actor (bound IFarmerActor, GameFarmer), Observer (IWorldObserver), ModRegistry, CancellationToken. Access the game only on this main-thread lifecycle; query adapters must not mutate the world. Use native interactions and factual evidence. SDK supplies framework references. Discovery pages declared public members; inspect baseType/interfaces explicitly for inherited APIs. Refine nativeType with an exact full name; use member filter and offset/limit to read every matching member."
        } };
    }
    private static object DescribeType(Type type, string? filter, int offset, int limit)
    {
        var members = type.GetMembers(BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.DeclaredOnly)
            .Where(m => m is MethodInfo or PropertyInfo or FieldInfo or ConstructorInfo)
            .Where(m=>filter == null || m.Name.Contains(filter,StringComparison.OrdinalIgnoreCase))
            .OrderBy(m=>m.ToString(),StringComparer.Ordinal).ToArray();
        var page=members.Skip(offset).Take(limit).ToArray();
        return new { name=type.FullName, baseType=type.BaseType?.FullName, interfaces=type.GetInterfaces().Select(t=>t.FullName).ToArray(),
            members=page.Select(m=>m.ToString() ?? m.Name).ToArray(), documentation=Documentation(type,page), total=members.Length,offset,limit,hasMore=offset+page.Length<members.Length };
    }
    private static string[] Documentation(Type type, MemberInfo[] members)
    {
        string xml = Path.ChangeExtension(type.Assembly.Location,".xml");
        if (!File.Exists(xml)) return Array.Empty<string>();
        try {
            var names=members.Select(m=>type.FullName+"."+m.Name).ToHashSet(StringComparer.Ordinal);
            var doc=System.Xml.Linq.XDocument.Load(xml);
            return doc.Descendants("member").Where(e => {
                    string id=(string?)e.Attribute("name") ?? "";
                    string name=id.Length>2 ? id[2..].Split('(')[0] : "";
                    return names.Contains(name);
                }).Select(e => ((string?)e.Attribute("name")) + ": " + string.Join(" ",e.Value.Split((char[]?)null,StringSplitOptions.RemoveEmptyEntries))).ToArray();
        } catch { return Array.Empty<string>(); }
    }
    private static IEnumerable<Type> SafeTypes(Assembly a) { try { return a.GetTypes(); } catch (ReflectionTypeLoadException ex) { return ex.Types.OfType<Type>(); } }
    private void Release(Loaded entry)
    {
        try { (entry.Adapter as IDisposable)?.Dispose(); }
        catch (Exception ex) { CleanupErrors.Add($"{entry.Manifest.Id}/{entry.Manifest.Version}: {ex.Message}"); }
        finally { entry.Context.Unload(); }
    }
    public void Dispose() { AssertThread(); foreach (var entry in loaded.Values) Release(entry); loaded.Clear(); }
}
