using System.Net.Http;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace StardewAI.Companion.Mod.Updates;

public sealed record ReleaseUpdate(string Version, string Notes, string Url, string Sha256, long Size)
{
    public const string Repository = "CSXizhang/star";
    public static ReleaseUpdate? Parse(string json, string installed)
    {
        using var doc = JsonDocument.Parse(json);
        var release = doc.RootElement;
        if (release.GetProperty("draft").GetBoolean() || release.GetProperty("prerelease").GetBoolean()) return null;
        string version = release.GetProperty("tag_name").GetString()!.TrimStart('v');
        if (!Regex.IsMatch(version, @"^\d+\.\d+\.\d+$") ||
            !System.Version.TryParse(installed, out var current) || System.Version.Parse(version) <= current) return null;
        string name = $"StardewAI.Companion.Mod-{version}-windows-x64.zip";
        foreach (var asset in release.GetProperty("assets").EnumerateArray())
        {
            if (asset.GetProperty("name").GetString() != name) continue;
            string url = asset.GetProperty("browser_download_url").GetString()!;
            if (!url.StartsWith($"https://github.com/{Repository}/releases/download/", StringComparison.Ordinal))
                throw new InvalidDataException("更新下载地址不属于本项目。 ");
            string digest = asset.TryGetProperty("digest", out var hash) ? hash.GetString() ?? "" : "";
            if (!Regex.IsMatch(digest, "^sha256:[a-fA-F0-9]{64}$"))
                throw new InvalidDataException("新版安装包缺少校验值，请稍后重试。 ");
            long size = asset.GetProperty("size").GetInt64();
            if (size <= 0 || size > 1024L * 1024 * 1024) throw new InvalidDataException("安装包大小异常。 ");
            string notes = release.TryGetProperty("body", out var body) ? body.GetString() ?? "" : "";
            notes = Regex.Replace(notes, @"[#*`\r]", "").Trim();
            if (notes.Length > 160) notes = notes[..160] + "…";
            return new(version, notes, url, digest[7..], size);
        }
        return null;
    }

    private static HttpClient Client(TimeSpan timeout)
    {
        var client = new HttpClient { Timeout = timeout };
        client.DefaultRequestHeaders.UserAgent.ParseAdd("StardewAICompanion-Updater/1.0");
        client.DefaultRequestHeaders.Accept.ParseAdd("application/vnd.github+json");
        return client;
    }

    public static async Task<ReleaseUpdate?> Check(string installed)
    {
        using var client = Client(TimeSpan.FromSeconds(15));
        return Parse(await client.GetStringAsync($"https://api.github.com/repos/{Repository}/releases/latest"), installed);
    }

    public async Task<string> Download(string directory)
    {
        Directory.CreateDirectory(directory);
        string partial = Path.Combine(directory, "package.partial");
        string archive = Path.Combine(directory, "package.zip");
        using var client = Client(TimeSpan.FromMinutes(15));
        using var deadline = new CancellationTokenSource(TimeSpan.FromMinutes(15));
        using var response = await client.GetAsync(Url, HttpCompletionOption.ResponseHeadersRead, deadline.Token);
        response.EnsureSuccessStatusCode();
        try
        {
            using (var output = File.Create(partial))
            using (var input = await response.Content.ReadAsStreamAsync(deadline.Token))
            {
                byte[] buffer = new byte[81920];
                long length = 0;
                int count;
                while ((count = await input.ReadAsync(buffer, deadline.Token)) > 0)
                {
                    length += count;
                    if (length > Size) throw new InvalidDataException("安装包大小不符。 ");
                    await output.WriteAsync(buffer.AsMemory(0, count), deadline.Token);
                }
                if (length != Size) throw new InvalidDataException("安装包下载不完整。 ");
            }
            using var sha = SHA256.Create();
            using (var input = File.OpenRead(partial))
                if (!Convert.ToHexString(sha.ComputeHash(input)).Equals(Sha256, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("安装包校验失败，原版本未更改。 ");
            File.Move(partial, archive, true);
            return archive;
        }
        finally { if (File.Exists(partial)) File.Delete(partial); }
    }
}
