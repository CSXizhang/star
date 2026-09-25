using System.Diagnostics;
using System.Text.Json;
using StardewAI.Companion.Mod.Transport;
using Xunit;

public class ChatCommandWireContractTests
{
    [Fact]
    public void PythonRepliesPreserveCommandIdentityAndNullableCompletion()
    {
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root != null && !File.Exists(Path.Combine(root.FullName, "runtime", "pyproject.toml")))
            root = root.Parent;
        Assert.NotNull(root);
        string python = Path.Combine(root!.FullName, "runtime", ".venv",
            OperatingSystem.IsWindows() ? "Scripts" : "bin",
            OperatingSystem.IsWindows() ? "python.exe" : "python");
        var start = new ProcessStartInfo(python)
        {
            WorkingDirectory = root.FullName,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };
        start.ArgumentList.Add("-c");
        start.ArgumentList.Add("import json; from stardew_ai_runtime.protocol import Envelope; " +
            "print(json.dumps([Envelope.create_chat_reply('bridge', 'chain-turn', 'job-completed', 'done', " +
            "save_id='farm', command_id='player-command', command_complete=c).to_mapping()['payload'] " +
            "for c in (False, True, None)]))");
        using var process = Process.Start(start)!;
        Assert.True(process.WaitForExit(15000));
        Assert.True(process.ExitCode == 0, process.StandardError.ReadToEnd());
        var replies = JsonSerializer.Deserialize<ChatReplyPayload[]>(process.StandardOutput.ReadToEnd())!;
        Assert.Equal(3, replies.Length);
        Assert.All(replies, reply =>
        {
            Assert.Equal("chain-turn", reply.RequestId);
            Assert.Equal("player-command", reply.CommandId);
            Assert.Equal("farm", reply.SaveId);
        });
        Assert.False(replies[0].CommandComplete);
        Assert.True(replies[1].CommandComplete);
        Assert.Null(replies[2].CommandComplete);
    }
}
