using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>
/// Cross-language wire contract lock for the §1 life.* chat-family messages.
/// Runs the Python exporter (production protocol factories + real ChatBridge
/// stores) and requires the C# DTOs to deserialize and re-serialize the exact
/// field surface, modulo the project convention that C# omits nulls.
/// </summary>
public class LifeWireContractTests
{
    private static string ExportFixtures()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root != null && !File.Exists(Path.Combine(root.FullName, "tools", "export-life-wire-test.py")))
            root = root.Parent;
        Assert.NotNull(root);
        string output = Path.Combine(
            root!.FullName, "artifacts", "acceptance", "life-wire-" + Guid.NewGuid().ToString("N"));
        var start = new ProcessStartInfo(Path.Combine(root.FullName, "runtime", ".venv", "Scripts", "python.exe"))
        { WorkingDirectory = root.FullName, UseShellExecute = false, CreateNoWindow = true, RedirectStandardError = true };
        start.ArgumentList.Add(Path.Combine(root.FullName, "tools", "export-life-wire-test.py"));
        start.ArgumentList.Add(output);
        using var process = Process.Start(start)!;
        Assert.True(process.WaitForExit(30000));
        Assert.True(process.ExitCode == 0, process.StandardError.ReadToEnd());
        return output;
    }

    private static string Canonical(JsonNode node)
    {
        if (node is JsonObject obj)
        {
            var map = new SortedDictionary<string, string>(StringComparer.Ordinal);
            foreach (var pair in obj)
            {
                if (pair.Value is null) continue; // C# DTOs omit nulls (WhenWritingNull)
                map[pair.Key] = Canonical(pair.Value);
            }
            return "{" + string.Join(",", map.Select(kv => JsonSerializer.Serialize(kv.Key) + ":" + kv.Value)) + "}";
        }
        if (node is JsonArray array)
            return "[" + string.Join(",", array.Select(item => item is null ? "null" : Canonical(item))) + "]";
        return node.ToJsonString();
    }

    private static TPayload LockPayload<TPayload>(string dir, string file, string messageType)
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(Path.Combine(dir, file)));
        Assert.Equal(messageType, doc.RootElement.GetProperty("messageType").GetString());
        Assert.Equal("0.1", doc.RootElement.GetProperty("protocolVersion").GetString());
        var payload = doc.RootElement.GetProperty("payload");
        var dto = payload.Deserialize<TPayload>()!;
        Assert.NotNull(dto);
        var once = JsonSerializer.Serialize(dto);
        var twice = JsonSerializer.Serialize(JsonSerializer.Deserialize<TPayload>(once)!);
        Assert.Equal(once, twice); // byte-stable roundtrip
        Assert.Equal(
            Canonical(JsonNode.Parse(payload.GetRawText())!),
            Canonical(JsonNode.Parse(once)!));
        return dto;
    }

    [Fact]
    public void LifeChatSubmit_ChatAndPlanModes_Roundtrip()
    {
        var dir = ExportFixtures();
        var chat = LockPayload<LifeChatSubmitPayload>(dir, "life-chat-submit-chat.json", "life.chat.submit");
        Assert.Equal("wire-req-1", chat.RequestId);
        Assert.Equal("wire-save", chat.SaveId);
        Assert.Equal("chat", chat.Mode);
        Assert.Equal("今天过得怎么样？", chat.Text);
        Assert.Equal("life-menu", chat.Source);
        var plan = LockPayload<LifeChatSubmitPayload>(dir, "life-chat-submit-plan.json", "life.chat.submit");
        Assert.Equal("plan", plan.Mode);
    }

    [Fact]
    public void LifeChatReply_EveryStatus_Roundtrips()
    {
        var dir = ExportFixtures();
        var processing = LockPayload<LifeChatReplyPayload>(dir, "life-chat-reply-processing.json", "life.chat.reply");
        Assert.Equal("processing", processing.Status);
        var queued = LockPayload<LifeChatReplyPayload>(dir, "life-chat-reply-queued.json", "life.chat.reply");
        Assert.Equal("queued", queued.Status);
        Assert.Equal(1, queued.QueuePosition);
        var completed = LockPayload<LifeChatReplyPayload>(dir, "life-chat-reply-completed.json", "life.chat.reply");
        Assert.Equal("completed", completed.Status);
        Assert.Equal("早上好呀，今天也一起加油吧", completed.ReplyText);
        var failed = LockPayload<LifeChatReplyPayload>(dir, "life-chat-reply-failed.json", "life.chat.reply");
        Assert.Equal("failed", failed.Status);
        Assert.Equal("RESOURCE_EXHAUSTED", failed.Error);
        Assert.Equal(1, completed.ProfileRevision);
        Assert.True(completed.MemoryRevision >= 2);
    }

    [Fact]
    public void LifeChatReply_NeverCarriesTokenUsageOrSessionFields()
    {
        var dir = ExportFixtures();
        using var doc = JsonDocument.Parse(File.ReadAllText(Path.Combine(dir, "life-chat-reply-completed.json")));
        var payload = doc.RootElement.GetProperty("payload");
        foreach (var forbidden in new[]
        {
            "tokensUsed", "promptTokens", "outputTokens", "cachedTokens",
            "conversationId", "usageSource", "sessionId", "commandId", "commandComplete",
        })
            Assert.False(payload.TryGetProperty(forbidden, out _), $"life.chat.reply must not carry '{forbidden}'");
    }

    [Fact]
    public void LifeProfileGet_Roundtrips()
    {
        var dir = ExportFixtures();
        var payload = LockPayload<LifeProfileGetPayload>(dir, "life-profile-get.json", "life.profile.get");
        Assert.Equal("wire-req-1", payload.RequestId);
        Assert.Equal("wire-save", payload.SaveId);
    }

    [Fact]
    public void LifeProfileSet_Roundtrips()
    {
        var dir = ExportFixtures();
        var payload = LockPayload<LifeProfileSetPayload>(dir, "life-profile-set.json", "life.profile.set");
        Assert.Equal(1, payload.ExpectedRevision);
        Assert.True(payload.Patch.Onboarded);
        Assert.Equal("workhorse", payload.Patch.PlayStyle);
        Assert.Null(payload.Patch.Personality);
        Assert.Null(payload.Patch.CompanionName);
    }

    [Fact]
    public void LifeProfileState_ConfirmedWithWorkProjection_Roundtrips()
    {
        var dir = ExportFixtures();
        var state = LockPayload<LifeProfileStatePayload>(dir, "life-profile-state.json", "life.profile.state");
        Assert.Equal("confirmed", state.Status);
        Assert.Equal("wire-req-1", state.RequestId);
        Assert.NotNull(state.Profile);
        Assert.True(state.Profile!.Onboarded);
        Assert.False(state.Profile.Skipped);
        Assert.Equal("earn", state.Profile.PlayStyle);
        Assert.Equal("lively", state.Profile.Personality);
        Assert.Equal("chatty", state.Profile.CareFrequency);
        Assert.Equal("阿星", state.Profile.CompanionName);
        Assert.Equal(1, state.ProfileRevision);
        var work = state.Work;
        Assert.Equal("command", work.Mode);
        Assert.False(work.Paused);
        Assert.Equal("优先赚钱：收获出货", work.Goal);
        Assert.Equal(500, work.DailySpendLimit);
        Assert.Equal("shipping", work.BoxPreference);
        Assert.NotNull(work.DailySpend);
        Assert.NotNull(work.ActiveGoals);
        Assert.True(work.ActiveGoals!.Count <= 5);
        Assert.Contains(work.ActiveGoals, g => g.Text == "优先赚钱：收获出货" && g.Status == "active");
        Assert.True(work.RecentTodos!.Count <= 5);
        Assert.Contains(work.RecentTodos, t => t.Intent == "晴天给作物浇水" && t.Status == "pending");
        // Contract §1.3: waitingConditions is a string list (≤5).
        Assert.True(work.WaitingConditions!.Count <= 5);
        // Real render shape: wait:inventory:WateringCan>=... — assert the item, not the operation.
        Assert.Contains(work.WaitingConditions, c => c.Contains("WateringCan"));
    }

    [Fact]
    public void LifeProfileState_NullProfile_Roundtrips()
    {
        var dir = ExportFixtures();
        var state = LockPayload<LifeProfileStatePayload>(dir, "life-profile-state-null.json", "life.profile.state");
        Assert.Equal("confirmed", state.Status);
        Assert.Null(state.Profile);
        Assert.Equal(0, state.ProfileRevision);
        Assert.NotNull(state.Work);
    }

    [Fact]
    public void LifeProfileState_RejectedCarriesStaleRevisionReason()
    {
        var dir = ExportFixtures();
        var state = LockPayload<LifeProfileStatePayload>(dir, "life-profile-state-rejected.json", "life.profile.state");
        Assert.Equal("rejected", state.Status);
        Assert.Equal("STALE_REVISION", state.Reason);
        Assert.NotNull(state.Work);
    }

    [Fact]
    public void LifeMemoryList_Roundtrips()
    {
        var dir = ExportFixtures();
        var payload = LockPayload<LifeMemoryListPayload>(dir, "life-memory-list.json", "life.memory.list");
        Assert.Equal("wire-req-1", payload.RequestId);
        Assert.Equal("wire-save", payload.SaveId);
    }

    [Fact]
    public void LifeMemoryState_Roundtrips()
    {
        var dir = ExportFixtures();
        var state = LockPayload<LifeMemoryStatePayload>(dir, "life-memory-state.json", "life.memory.state");
        Assert.Equal("confirmed", state.Status);
        Assert.True(state.MemoryRevision >= 2);
        var agreement = Assert.Single(state.Entries, e => e.Kind == "agreement");
        Assert.Equal("每天给鸡喂食", agreement.Text);
        Assert.Equal("player", agreement.Source);
        Assert.Equal("1:spring:2", agreement.GameDate);
        Assert.False(string.IsNullOrEmpty(agreement.CreatedAt));
        var completed = Assert.Single(state.Entries, e => e.Kind == "event");
        Assert.Equal("system", completed.Source);
        Assert.Equal("完成了「给胡萝卜浇水」", completed.Text);
    }

    [Fact]
    public void LifeMemoryEdit_AllOps_Roundtrip()
    {
        var dir = ExportFixtures();
        var add = LockPayload<LifeMemoryEditPayload>(dir, "life-memory-edit-add.json", "life.memory.edit");
        Assert.Equal("add", add.Op);
        Assert.Equal("agreement", add.Kind);
        Assert.Equal("晚上八点一起钓鱼", add.Text);
        Assert.Null(add.Id);
        var correct = LockPayload<LifeMemoryEditPayload>(dir, "life-memory-edit-correct.json", "life.memory.edit");
        Assert.Equal("correct", correct.Op);
        Assert.False(string.IsNullOrEmpty(correct.Id));
        Assert.Equal("每天给鸡喂食并收蛋", correct.Text);
        var delete = LockPayload<LifeMemoryEditPayload>(dir, "life-memory-edit-delete.json", "life.memory.edit");
        Assert.Equal("delete", delete.Op);
        Assert.Equal(correct.Id, delete.Id);
        Assert.Null(delete.Text);
    }

    [Fact]
    public void LifeCare_Roundtrips()
    {
        var dir = ExportFixtures();
        var care = LockPayload<LifeCarePayload>(dir, "life-care.json", "life.care");
        Assert.Equal("wire-save", care.SaveId);
        Assert.Equal("work-done", care.Kind);
        Assert.Equal("work-done:1:spring:2:cmd-42", care.EventKey);
        Assert.Equal("1:spring:2", care.GameDate);
        Assert.Equal("辛苦啦，喝口水吧", care.Text);
    }

    // ------------------------------------------------------------ §2 life.milestones.*
    // Cross-language fixtures (life-milestones-*.json, world-snapshot-player-items.json)
    // are exported by tools/export-life-wire-test.py; these tests lock the C#
    // serialization surface against them (canonical JSON, C# omitting nulls).

    [Fact]
    public void LifeMilestonesGet_LocksPythonFixture()
    {
        var dir = ExportFixtures();
        var payload = LockPayload<LifeMilestonesGetPayload>(dir, "life-milestones-get.json", "life.milestones.get");
        Assert.Equal("lmg-wire-1", payload.RequestId);
        Assert.Equal("wire-save", payload.SaveId);

        using var doc = JsonDocument.Parse(File.ReadAllText(Path.Combine(dir, "life-milestones-get.json")));
        var root = doc.RootElement.GetProperty("payload");
        Assert.Equal(2, root.EnumerateObject().Count());
    }

    [Fact]
    public void LifeMilestonesState_FullFields_LockPythonFixture()
    {
        var dir = ExportFixtures();
        var state = LockPayload<LifeMilestonesStatePayload>(dir, "life-milestones-state.json", "life.milestones.state");
        Assert.Equal("ok", state.Status);
        Assert.Equal("lmg-wire-1", state.RequestId);
        Assert.Equal("1:spring:11", state.GameDate);
        Assert.Null(state.Error);
        Assert.Equal(2, state.Nodes!.Count);

        // Node 1: the Egg Festival strawberry node as adopted through the real
        // plan-mode store (goal/todo wiring already applied by the exporter).
        var node = state.Nodes[0];
        Assert.Equal("spring-egg-festival-strawberry:y1", node.Id);
        Assert.Equal("蛋蛋节草莓种子准备", node.Title);
        Assert.Equal("adopted", node.Status);
        Assert.Null(node.Verification);
        Assert.Equal("1:spring:13", node.TargetDate);
        Assert.Equal(2, node.DaysUntil);
        Assert.Equal("https://stardewvalleywiki.com/Egg_Festival", node.SourceUrl);
        Assert.Equal(1000, node.ReservedFunds);
        Assert.Equal(10, node.PlannedCount);
        Assert.Equal("预留1000g仅用于蛋蛋节当天购买草莓种子", node.TermsNote);
        Assert.True(node.UpdatedAt > 1_700_000_000);
        Assert.Equal(4, node.PrepItems!.Count);
        Assert.Equal("reserve-funds", node.PrepItems[0].Key);
        Assert.Equal("manual", node.PrepItems[0].Support);
        Assert.Equal("pending", node.PrepItems[0].Status);
        Assert.Null(node.PrepItems[0].Note);
        Assert.Equal("buy-at-festival", node.PrepItems[1].Key);
        Assert.Equal("仅核验种子准备，不代表种植完成；彩蛋寻宝后无法再购买",
            node.PrepItems[1].Note);
        Assert.All(node.PrepItems, p => Assert.NotEqual("done", p.Status));

        // Node 2: a completed/verified retention node exercising verification
        // plus a zero reservedFunds that must survive the roundtrip.
        var completed = state.Nodes[1];
        Assert.Equal("spring-crops-bundle-retention:y1", completed.Id);
        Assert.Equal("completed", completed.Status);
        Assert.Equal("verified", completed.Verification);
        Assert.Equal(0, completed.DaysUntil);
        Assert.Equal(0, completed.ReservedFunds);
        Assert.Equal(4, completed.PlannedCount);
        Assert.Equal("季末核对", completed.TermsNote);
        var prep = Assert.Single(completed.PrepItems!);
        Assert.Equal("retain-parsnip", prep.Key);
        Assert.Equal("done", prep.Status);
        Assert.Equal("背包中已确认", prep.Note);
    }

    [Fact]
    public void LifeMilestonesState_NullFields_AreOmittedOnWrite()
    {
        var dir = ExportFixtures();
        var state = LockPayload<LifeMilestonesStatePayload>(dir, "life-milestones-state-minimal.json", "life.milestones.state");
        Assert.Equal("ok", state.Status);
        // Proactive push shape: requestId "" and no gameDate in the fixture.
        Assert.Equal(string.Empty, state.RequestId);
        Assert.Null(state.GameDate);

        var node = Assert.Single(state.Nodes!);
        Assert.Equal("spring-egg-festival-strawberry:y1", node.Id);
        Assert.Equal("suggested", node.Status);
        Assert.Null(node.Verification);
        Assert.Equal("1:spring:13", node.TargetDate);
        Assert.Equal(1727340000.5, node.UpdatedAt!.Value);
        Assert.Empty(node.PrepItems!);

        // The C# re-serialization must keep omitting every optional the
        // Python fixture omits (LockPayload already canonical-compared; this
        // pins the omission on the actual written bytes).
        var once = JsonSerializer.Serialize(state);
        using var doc = JsonDocument.Parse(once);
        var root = doc.RootElement;
        Assert.False(root.TryGetProperty("gameDate", out _), "null gameDate must be omitted");
        Assert.False(root.TryGetProperty("error", out _), "null error must be omitted");
        var written = root.GetProperty("nodes")[0];
        foreach (var omitted in new[]
        {
            "verification", "daysUntil", "sourceUrl",
            "reservedFunds", "plannedCount", "termsNote",
        })
            Assert.False(written.TryGetProperty(omitted, out _), $"null {omitted} must be omitted");
    }

    [Fact]
    public void LifeMilestonesState_Failed_LocksPythonFixture()
    {
        var dir = ExportFixtures();
        var state = LockPayload<LifeMilestonesStatePayload>(dir, "life-milestones-state-failed.json", "life.milestones.state");
        Assert.Equal("lmg-wire-1", state.RequestId);
        Assert.Equal("failed", state.Status);
        Assert.Equal("MILESTONE_STORE_UNAVAILABLE", state.Error);
        Assert.Empty(state.Nodes!);
        Assert.Null(state.GameDate);
    }

    [Fact]
    public void LifeCare_MilestoneKind_LocksPythonFixture()
    {
        var dir = ExportFixtures();
        var care = LockPayload<LifeCarePayload>(dir, "life-care-milestone.json", "life.care");
        Assert.Equal("wire-save", care.SaveId);
        Assert.Equal("milestone", care.Kind);
        Assert.Equal("milestone:1:spring:12:spring-egg-festival-strawberry:y1", care.EventKey);
        Assert.Equal("1:spring:12", care.GameDate);
        Assert.Contains("蛋蛋节", care.Text);
    }

    [Fact]
    public void WorldStateSnapshot_PlayerItems_OldPayloadWithoutField_Deserializes()
    {
        // §2.4 compatibility: a snapshot written before playerItems existed must
        // still deserialize, leaving the aggregate unknown (null).
        const string oldPayload =
            "{\"currentLocation\":\"Farm\",\"timeOfDay\":600,\"season\":\"spring\",\"dayOfMonth\":13,\"isRaining\":false,\"year\":1}";
        var snapshot = JsonSerializer.Deserialize<WorldStateSnapshot>(oldPayload);
        Assert.NotNull(snapshot);
        Assert.Equal("Farm", snapshot!.CurrentLocation);
        Assert.Equal(13, snapshot.DayOfMonth);
        Assert.Null(snapshot.PlayerItems);
        Assert.Null(snapshot.PlayerMoney);
        Assert.Null(snapshot.PlayerStamina);

        var roundtripped = JsonSerializer.Serialize(snapshot);
        Assert.DoesNotContain("playerItems", roundtripped);
    }

    [Fact]
    public void WorldStateSnapshot_PlayerItems_LockPythonFixture()
    {
        var dir = ExportFixtures();
        var payload = LockPayload<WorldSnapshotPayload>(dir, "world-snapshot-player-items.json", "world.snapshot");
        Assert.Equal(7, payload.CapturedRevision);
        Assert.Equal("Farm", payload.World.CurrentLocation);

        Assert.Equal(500, payload.World.PlayerMoney);
        Assert.Equal(180.5f, payload.World.PlayerStamina);
        Assert.Equal(270, payload.World.PlayerMaxStamina);
        var items = payload.World.PlayerItems!;
        Assert.Equal(2, items.Count);
        Assert.Equal("Strawberry Seeds", items[0].Name);
        Assert.Equal(10, items[0].Quantity);
        Assert.Equal("Parsnip", items[1].Name);
        Assert.Equal(3, items[1].Quantity);
    }
}
