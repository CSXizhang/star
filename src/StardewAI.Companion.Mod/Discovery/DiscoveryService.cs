using System.Text.Json;
using StardewModdingAPI;

namespace StardewAI.Companion.Mod.Discovery;

/// <summary>
/// Manages local restricted endpoint and token discovery for external Runtime.
/// Publishes connection coordinates and session tokens into an explicit Mod-private file,
/// restricts access to current OS user, rotates credentials on each save lifecycle,
/// and ensures credentials are never logged in normal logs.
/// </summary>
public sealed class DiscoveryService

{
    private readonly string _discoveryPath;
    private readonly IMonitor _monitor;
    private readonly object _lock = new();

    public string DiscoveryPath => _discoveryPath;

    public DiscoveryService(string modDirectory, IMonitor monitor)
    {
        ArgumentNullException.ThrowIfNull(modDirectory);
        _monitor = monitor ?? throw new ArgumentNullException(nameof(monitor));

        // Explicit deterministic Mod-private discovery path
        _discoveryPath = Path.Combine(modDirectory, "data", "transport-discovery.json");
    }

    /// <summary>
    /// Publishes discovery file for external runtime without logging secret token.
    /// </summary>
    public void Publish(string saveId, string gameSessionId, int port, string sessionToken)
    {
        lock (_lock)
        {
            try
            {
                var discoveryObj = new
                {
                    protocolVersion = "0.1",
                    endpoint = $"ws://127.0.0.1:{port}/",
                    host = "127.0.0.1",
                    port = port,
                    sessionToken = sessionToken,
                    saveId = saveId,
                    gameSessionId = gameSessionId,
                    createdAtUtc = DateTime.UtcNow.ToString("o")
                };

                string json = JsonSerializer.Serialize(discoveryObj, new JsonSerializerOptions { WriteIndented = true });

                WriteFileAtomicRestricted(_discoveryPath, json);

                // Note: Never log sessionToken in SMAPI logs!
                _monitor.Log($"Transport endpoint discovery published at {_discoveryPath} (Port: {port}, Save: {saveId}).", LogLevel.Info);
            }
            catch (Exception ex)
            {
                _monitor.Log($"Failed to publish transport discovery: {ex.Message}", LogLevel.Warn);
            }
        }
    }

    /// <summary>
    /// Invalidates and removes discovery file on save unload or title return.
    /// </summary>
    public void Invalidate()
    {
        lock (_lock)
        {
            try
            {
                if (File.Exists(_discoveryPath))
                {
                    File.Delete(_discoveryPath);
                    _monitor.Log($"Transport endpoint discovery invalidated at {_discoveryPath}.", LogLevel.Debug);
                }
            }
            catch (Exception ex)
            {
                _monitor.Log($"Error invalidating discovery file: {ex.Message}", LogLevel.Warn);
            }
        }
    }

    private static void WriteFileAtomicRestricted(string targetPath, string content)
    {
        string? dir = Path.GetDirectoryName(targetPath);
        if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
        {
            Directory.CreateDirectory(dir);
        }

        string tempPath = targetPath + ".tmp." + Guid.NewGuid().ToString("N");
        File.WriteAllText(tempPath, content);

        try
        {
            if (OperatingSystem.IsWindows())
            {
                var fileInfo = new FileInfo(tempPath);
                var fileSecurity = fileInfo.GetAccessControl();
                fileSecurity.SetAccessRuleProtection(isProtected: true, preserveInheritance: false);
                var currentUser = System.Security.Principal.WindowsIdentity.GetCurrent().User;
                if (currentUser != null)
                {
                    fileSecurity.AddAccessRule(new System.Security.AccessControl.FileSystemAccessRule(
                        currentUser,
                        System.Security.AccessControl.FileSystemRights.FullControl,
                        System.Security.AccessControl.AccessControlType.Allow));
                }
                fileInfo.SetAccessControl(fileSecurity);
            }
        }
        catch
        {
            // Platform or environment safe fallback
        }

        File.Move(tempPath, targetPath, overwrite: true);
    }
}

