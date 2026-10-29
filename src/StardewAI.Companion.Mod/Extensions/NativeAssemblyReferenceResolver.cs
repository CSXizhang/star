using System.Reflection;
namespace StardewAI.Companion.Mod.Extensions;

/// <summary>SMAPI may rewrite/load Mods from bytes. Locate compile references by
/// full identity within the configured Mods directory, without loading DLLs.</summary>
internal sealed class NativeAssemblyReferenceResolver
{
    private readonly string? modsRoot;
    private readonly string? hostAssemblyPath;
    private readonly Dictionary<string,string> cache = new(StringComparer.OrdinalIgnoreCase);
    private readonly HashSet<string> attempted = new(StringComparer.OrdinalIgnoreCase);
    public NativeAssemblyReferenceResolver(string? modsRoot, string? hostAssemblyPath)
    { this.modsRoot=modsRoot; this.hostAssemblyPath=hostAssemblyPath; }

    public (string[] References,string[] Missing) Resolve(IEnumerable<Assembly> assemblies)
    {
        string framework=Path.GetDirectoryName(typeof(object).Assembly.Location)!;
        var paths=new Dictionary<string,string>(StringComparer.OrdinalIgnoreCase);
        var missing=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var assembly in assemblies)
        {
            string? identity=assembly.GetName().FullName;
            if (identity == null) continue;
            string path=assembly.Location;
            if (File.Exists(path))
            {
                if (!string.Equals(Path.GetDirectoryName(path),framework,StringComparison.OrdinalIgnoreCase)) paths[identity]=path;
            }
            else if (cache.TryGetValue(identity,out string? cached) && File.Exists(cached)) paths[identity]=cached;
            else missing.Add(identity);
        }
        if (missing.Count>0)
        {
            TryMatch(hostAssemblyPath,missing,paths);
            var pending=missing.Where(id=>!attempted.Contains(id)).ToHashSet(StringComparer.OrdinalIgnoreCase);
            if (pending.Count>0 && modsRoot != null && Directory.Exists(modsRoot))
            {
                foreach(string path in Dlls(modsRoot))
                {
                    TryMatch(path,pending,paths);
                    if (pending.Count==0) break;
                }
            }
            foreach(var pair in paths) { cache[pair.Key]=pair.Value; missing.Remove(pair.Key); }
            foreach(string identity in missing) attempted.Add(identity);
        }
        return (paths.Values.Distinct(StringComparer.OrdinalIgnoreCase).ToArray(),missing.OrderBy(x=>x,StringComparer.Ordinal).ToArray());
    }
    private static void TryMatch(string? path,HashSet<string> missing,Dictionary<string,string> paths)
    {
        if (path==null || !File.Exists(path)) return;
        try {
            string? identity=AssemblyName.GetAssemblyName(path).FullName;
            if (identity!=null && missing.Remove(identity)) paths[identity]=Path.GetFullPath(path);
        } catch (Exception ex) when (ex is BadImageFormatException or FileLoadException or IOException or UnauthorizedAccessException) { }
    }
    private static IEnumerable<string> Dlls(string root)
    {
        var directories=new Stack<string>(); directories.Push(Path.GetFullPath(root));
        while(directories.Count>0)
        {
            string directory=directories.Pop();
            string[] files, children;
            try { files=Directory.GetFiles(directory,"*.dll"); children=Directory.GetDirectories(directory); }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { continue; }
            foreach(string file in files) if ((File.GetAttributes(file)&FileAttributes.ReparsePoint)==0) yield return file;
            foreach(string child in children)
            {
                string name=Path.GetFileName(child);
                if (name.Equals("data",StringComparison.OrdinalIgnoreCase) || name.Equals(".local",StringComparison.OrdinalIgnoreCase)
                    || name.Equals("toolchain",StringComparison.OrdinalIgnoreCase) || name.Equals("sdk",StringComparison.OrdinalIgnoreCase)
                    || name.Equals("packs",StringComparison.OrdinalIgnoreCase) || name.Equals("shared",StringComparison.OrdinalIgnoreCase)
                    || name.Equals(".git",StringComparison.OrdinalIgnoreCase) || (File.GetAttributes(child)&FileAttributes.ReparsePoint)!=0) continue;
                directories.Push(child);
            }
        }
    }
}
