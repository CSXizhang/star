using System;
using System.IO;
using System.Text.Json;
using StardewAI.Companion.Mod.Updates;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class ReleaseUpdateTests
{
    private static string Release(string version = "0.3.16", bool preview = false,
        string? digest = null, string? url = null) => JsonSerializer.Serialize(new {
            tag_name = "v" + version, draft = false, prerelease = preview, body = "更新说明",
            assets = new[] { new { name = $"StardewAI.Companion.Mod-{version}-windows-x64.zip",
                browser_download_url = url ?? $"https://github.com/CSXizhang/star/releases/download/v{version}/package.zip",
                digest = digest ?? "sha256:" + new string('a', 64), size = 100 } }
        });

    [Fact]
    public void SelectsNewerStablePackageOnly()
    {
        Assert.Equal("0.3.16", ReleaseUpdate.Parse(Release(), "0.3.15")!.Version);
        Assert.Null(ReleaseUpdate.Parse(Release(), "0.3.16"));
        Assert.Null(ReleaseUpdate.Parse(Release(), "0.4.0"));
        Assert.Null(ReleaseUpdate.Parse(Release(preview: true), "0.3.15"));
        Assert.Null(ReleaseUpdate.Parse(Release("0.3.16-beta"), "0.3.15"));
    }

    [Fact]
    public void RejectsMissingDigestAndForeignRepository()
    {
        Assert.Throws<InvalidDataException>(() => ReleaseUpdate.Parse(Release(digest: ""), "0.3.15"));
        Assert.Throws<InvalidDataException>(() => ReleaseUpdate.Parse(Release(url: "https://github.com/other/repo/package.zip"), "0.3.15"));
    }
}
