using StardewAI.Companion.Mod.Domain;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public sealed class CompanionReloadTests
{
    [Fact]
    public void ReloadKeepsLatestDecisionAndOriginalPlayerReason()
    {
        var pending = new CompanionConversationEntry("n", "伙伴", "保留树吗？", "1:spring:1", false,
            DecisionId: "notice", DecisionStatus: "pending");
        var answered = pending with { DecisionStatus = "answered" };
        var answer = new CompanionConversationEntry("a", "你", "保留，喜欢树荫", "1:spring:2", true, ReplyToDecisionId: "notice");
        var result = CompanionSaveCheckpoint.RestoreWithReloadHistory(new[] { pending }, new[] { answered, answer });
        Assert.Equal("answered", result[0].DecisionStatus);
        Assert.Equal("notice", result[1].ReplyToDecisionId);
        Assert.Equal(answer.Text, result[1].Text);
        Assert.True(result[1].RolledBack);
    }

    [Fact]
    public void FullOriginalsSurviveVisibleHistoryLimitAndStatusUpdates()
    {
        string directory = Path.Combine(Path.GetTempPath(), "companion-reload-" + Guid.NewGuid().ToString("N"));
        try
        {
            var store = new CompanionConversationStore(directory);
            var entries = new List<CompanionConversationEntry>();
            for (int i = 0; i < 260; i++)
            {
                entries.Add(new("m" + i, "你", "原文" + i + new string('长', 180), "1:spring:2", true));
                store.Save("farm", CompanionConversationStore.Retain(entries));
            }
            var restarted = new CompanionConversationStore(directory);
            Assert.Equal(200, restarted.Load("farm").Count);
            Assert.Equal(entries, restarted.LoadHistory("farm"));
            Assert.Empty(restarted.LoadHistory("other"));
            var archive = restarted.ArchiveReload("farm", restarted.LoadHistory("farm"));
            Assert.Contains(entries[0].Id, File.ReadAllText(archive));
        }
        finally { Directory.Delete(directory, recursive: true); }
    }
}
