using System.Diagnostics;
using System.Text.Json;
using StardewAI.Companion.Mod.Menus;
using StardewAI.Companion.Mod.Transport;
using Xunit;

public class ChatWireContractTests
{
    [Fact]
    public void PythonProductionSerializerFeedsCSharpProductionDto()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root != null && !File.Exists(Path.Combine(root.FullName, "tools", "export-chat-wire-test.py"))) root = root.Parent;
        Assert.NotNull(root);
        string output = Path.Combine(root!.FullName, "artifacts", "acceptance", "r45", "wire-" + Guid.NewGuid().ToString("N"));
        var start = new ProcessStartInfo(Path.Combine(root.FullName, "runtime", ".venv", "Scripts", "python.exe"))
        { WorkingDirectory = root.FullName, UseShellExecute = false, CreateNoWindow = true, RedirectStandardError = true };
        start.ArgumentList.Add(Path.Combine(root.FullName, "tools", "export-chat-wire-test.py"));
        start.ArgumentList.Add(output);
        using var process = Process.Start(start)!;
        Assert.True(process.WaitForExit(15000));
        Assert.True(process.ExitCode == 0, process.StandardError.ReadToEnd());
        using var doc = JsonDocument.Parse(File.ReadAllText(Path.Combine(output, "autonomy-state.json")));
        var payload = doc.RootElement.GetProperty("payload");
        var state = payload.Deserialize<AutonomyStatePayload>()!;
        Assert.Equal("confirmed", state.Status);
        var condition = Assert.Single(state.WaitingConditions!);
        Assert.Equal("refill_watering_can", condition.Operation);
        Assert.Equal("inventory", condition.WaitCondition!["type"]!.ToString());
        Assert.Contains("refill_watering_can", condition.DisplayText);
        // Prove the previous string-list contract rejects these actual Python bytes.
        Assert.Throws<JsonException>(() => payload.GetProperty("waitingConditions").Deserialize<List<string>>());
        using var context = JsonDocument.Parse(File.ReadAllText(Path.Combine(output, "work-context.json")));
        var modelConditions = context.RootElement.GetProperty("waitingConditions").Deserialize<List<WaitingConditionPayload>>()!;
        Assert.Equal(condition.Operation, Assert.Single(modelConditions).Operation);
        Assert.Equal(condition.WaitDescription, modelConditions[0].WaitDescription);
    }

    [Fact]
    public void ModeButtonShowsActionAndPendingNeverClaimsModeChanged()
    {
        CompanionCommandMenu.AutonomyMode = "command";
        CompanionCommandMenu.ModeChangePending = false;
        Assert.Equal("开启自由模式", CompanionCommandMenu.ModeButtonText);
        CompanionCommandMenu.ModeChangePending = true;
        Assert.Equal("等待模式确认", CompanionCommandMenu.ModeButtonText);
        Assert.Equal("command", CompanionCommandMenu.AutonomyMode);
        CompanionCommandMenu.ModeChangePending = false;
        CompanionCommandMenu.AutonomyMode = "free";
        Assert.Equal("退出自由模式", CompanionCommandMenu.ModeButtonText);
        CompanionCommandMenu.AutonomyMode = "command";
    }
}
