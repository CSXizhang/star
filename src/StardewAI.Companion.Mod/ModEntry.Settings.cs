using System.Diagnostics;
using System.Text.Json;
using StardewAI.Companion.Mod.Menus;
using StardewAI.Companion.Mod.Updates;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace StardewAI.Companion.Mod;

public sealed partial class ModEntry
{
    private ReleaseUpdate? _availableUpdate;
    private bool _updateNoticePending, _updateBusy, _restartPending, _savingForRestart, _exitWhenSaved;
    private string _updateStatus = "尚未检查更新。";
    private string? _preparedUpdateRequest;
    private string? _updaterScript;
    private string _savedMode = "companion";
    private bool CanRestartHere => OperatingSystem.IsWindows() && Path.GetFullPath(Helper.DirectoryPath)
        .StartsWith(Path.Combine(Constants.GamePath, "Mods") + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);

    private void InitializeUpdates()
    {
        _savedMode = _config.Mode;
        Helper.Events.GameLoop.Saving += (_, _) => _savingForRestart = true;
        Helper.Events.GameLoop.Saved += (_, _) => _savingForRestart = false;
        Helper.Events.GameLoop.ReturnedToTitle += (_, _) => _savingForRestart = false;
        Helper.Events.GameLoop.GameLaunched += (_, _) => CheckUpdates(false);
    }

    private void CheckUpdates(bool showResult)
    {
        if (_updateBusy) return;
        if (!CanRestartHere || !File.Exists(Path.Combine(Helper.DirectoryPath, "release-manifest.json")))
        {
            _updateStatus = "源码或自定义 Mods 目录请手动更新。";
            if (showResult) OpenGameSettings();
            return;
        }
        _updateBusy = true;
        _updateStatus = "正在检查更新…";
        _ = Task.Run(async () =>
        {
            ReleaseUpdate? update = null;
            string status;
            try
            {
                update = await ReleaseUpdate.Check(ModManifest.Version.ToString());
                status = update == null ? "已是最新稳定版。" : $"可更新至 {update.Version}。";
            }
            catch (Exception ex) { status = "检查失败，可稍后重试。"; Monitor.Log($"Update check: {ex.Message}", LogLevel.Debug); }
            _mainThreadActions.Enqueue(() => {
                _availableUpdate = update; _updateStatus = status; _updateBusy = false;
                _updateNoticePending = update != null;
                if (showResult) OpenGameSettings();
            });
        });
    }

    private void ShowPendingUpdate()
    {
        if (_exitWhenSaved && !_savingForRestart) { _exitWhenSaved = false; ExitWhenReady(); return; }
        if (!_updateNoticePending || _updateBusy || Game1.eventUp) return;
        if (Game1.activeClickableMenu != null && Game1.activeClickableMenu is not TitleMenu) return;
        if (Game1.activeClickableMenu is TitleMenu && TitleMenu.subMenu != null) return;
        _updateNoticePending = false;
        var previous = Game1.activeClickableMenu;
        ShowUpdate(() => Game1.activeClickableMenu = previous);
    }

    private void ShowUpdate(Action back)
    {
        if (_preparedUpdateRequest != null)
        {
            ShowRestartPrompt(back, _preparedUpdateRequest);
            return;
        }
        var update = _availableUpdate;
        if (update == null) { back(); return; }
        Game1.activeClickableMenu = new CompanionOptionsMenu("发现新版本 " + update.Version,
            update.Notes + "\n下载完成后，退出游戏安装。配置和记忆会保留。", back,
            ("下载更新", () => DownloadUpdate(update, back)), ("稍后", back));
    }

    private void DownloadUpdate(ReleaseUpdate update, Action back)
    {
        if (_updateBusy) return;
        _updateBusy = true; _updateStatus = "正在下载和校验…";
        var downloadMenu = new CompanionOptionsMenu("更新", _updateStatus, back, ("返回，后台下载", back));
        Game1.activeClickableMenu = downloadMenu;
        string root = Helper.DirectoryPath;
        string smapi = Constants.ApiVersion.ToString();
        _ = Task.Run(async () =>
        {
            try
            {
                string directory = Path.Combine(root, "data", "updates", Guid.NewGuid().ToString("N"));
                string archive = await update.Download(directory);
                var request = WriteUpdaterRequest(directory, archive, update.Sha256, update.Version, smapi);
                using var process = Process.Start(UpdaterStartInfo(request.Script, request.Request, prepare: true))
                    ?? throw new IOException("无法启动更新校验。 ");
                await process.WaitForExitAsync();
                if (process.ExitCode != 0) throw new IOException("安装包准备失败，详见 data/update-result.json。 ");
                _mainThreadActions.Enqueue(() => {
                    _updateBusy = false; _preparedUpdateRequest = request.Request; _updaterScript = request.Script;
                    _updateStatus = "更新已就绪，退出游戏后安装。";
                    _updateNoticePending = true;
                    if (ReferenceEquals(Game1.activeClickableMenu, downloadMenu)) {
                        _updateNoticePending = false;
                        ShowRestartPrompt(back, request.Request);
                    }
                });
            }
            catch (Exception ex)
            {
                Monitor.Log($"Update preparation: {ex.Message}", LogLevel.Warn);
                _mainThreadActions.Enqueue(() => {
                    _updateBusy = false; _updateStatus = "下载或校验失败，原版本未更改。可重试。";
                    if (ReferenceEquals(Game1.activeClickableMenu, downloadMenu))
                        Game1.activeClickableMenu = new CompanionOptionsMenu("更新", _updateStatus, back, ("返回", back));
                });
            }
        });
    }

    private (string Request, string Script) WriteUpdaterRequest(string directory, string? archive = null,
        string? sha256 = null, string? version = null, string? smapi = null)
    {
        Directory.CreateDirectory(directory);
        foreach (string file in new[] { "apply-update.ps1", "release-package.ps1" })
            File.Copy(Path.Combine(Helper.DirectoryPath, "tools", file), Path.Combine(directory, file), true);
        string request = Path.Combine(directory, "request.json");
        File.WriteAllText(request, JsonSerializer.Serialize(new {
            target = Helper.DirectoryPath, game = Constants.GamePath,
            processId = Environment.ProcessId,
            processStarted = Process.GetCurrentProcess().StartTime.ToUniversalTime().ToString("o"),
            archive, sha256, version, smapi,
            installedVersion = ModManifest.Version.ToString()
        }));
        return (request, Path.Combine(directory, "apply-update.ps1"));
    }

    private static ProcessStartInfo UpdaterStartInfo(string script, string request, bool prepare = false)
    {
        var start = new ProcessStartInfo("powershell.exe") { UseShellExecute = false, CreateNoWindow = true };
        foreach (string arg in new[] { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-Request", request })
            start.ArgumentList.Add(arg);
        if (prepare) start.ArgumentList.Add("-Prepare");
        return start;
    }

    private void ShowRestartPrompt(Action back, string? request = null)
    {
        string message = Context.IsWorldReady
            ? "未睡觉保存的当天进度会丢失。建议先保存，再重启。"
            : "关闭游戏后自动完成操作，再启动游戏。";
        Game1.activeClickableMenu = new CompanionOptionsMenu(request == null ? "重启使模式生效" : "更新已就绪", message, back,
            (request == null ? "确认重启" : "确认退出并更新", () => RestartGame(request)), ("稍后", back));
    }

    private void RestartGame(string? request)
    {
        if (_restartPending) return;
        try
        {
            if (!CanRestartHere) throw new IOException("自定义 Mods 目录请手动重启。 ");
            string script;
            if (request == null)
            {
                var launch = WriteUpdaterRequest(Path.Combine(Helper.DirectoryPath, "data", "updates", Guid.NewGuid().ToString("N")));
                request = launch.Request; script = launch.Script;
            }
            else script = _updaterScript ?? throw new IOException("更新尚未准备好。 ");
            string ready = request + ".ready";
            if (File.Exists(ready)) File.Delete(ready);
            var start = UpdaterStartInfo(script, request);
            _restartPending = true;
            _ = Task.Run(async () => {
                try
                {
                    using var process = Process.Start(start) ?? throw new IOException("无法启动更新程序。 ");
                    DateTime deadline = DateTime.UtcNow.AddSeconds(90);
                    while (!File.Exists(ready))
                    {
                        if (process.HasExited || DateTime.UtcNow > deadline)
                        {
                            if (!process.HasExited) process.Kill();
                            throw new IOException("重启准备未完成，游戏保持运行。 ");
                        }
                        await Task.Delay(100);
                    }
                    _mainThreadActions.Enqueue(ExitWhenReady);
                }
                catch (Exception ex)
                {
                    Monitor.Log($"Restart preparation: {ex.Message}", LogLevel.Warn);
                    _mainThreadActions.Enqueue(() => {
                        _restartPending = false;
                        _updateStatus = "重启准备失败，游戏未关闭。请稍后重试。";
                        OpenGameSettings();
                    });
                }
            });
        }
        catch (Exception ex)
        {
            Monitor.Log($"Restart: {ex.Message}", LogLevel.Warn);
            _updateStatus = "无法重启，游戏仍在运行。请稍后重试。";
            OpenGameSettings();
        }
    }

    private void ExitWhenReady()
    {
        if (_savingForRestart) { _exitWhenSaved = true; return; }
        StopOwnedBridgeLauncher(gameExiting: true);
        Game1.game1.Exit();
    }

    private void OpenGameSettings()
    {
        _updateNoticePending = false;
        Action back = () => { if (Context.IsWorldReady) OpenCompanionHub(2); else Game1.exitActiveMenu(); };
        string mode = _savedMode == "player" ? "AI 自己玩" : "伙伴模式";
        string message = $"游戏模式：{mode}" + (_savedMode != _config.Mode ? "（重启后生效）" : "") +
            "\nF8 随时指导；自玩模式按 F9 接管或交还。\n" + _updateStatus;
        var choices = new List<(string Label, Action Click)>();
        if (PlayerModeEnabled && _authority != null)
            choices.Add((_authority.AgentOwns ? "接管玩家" : "开始托管", () => {
                if (_authority.AgentOwns) ReleasePlayerControl("human-takeover");
                else { _playerControlApproved = true; _authority.RequestAgent("human-explicit-handoff"); }
                OpenGameSettings();
            }));
        choices.AddRange(new (string Label, Action Click)[] {
            ("切换为" + (_savedMode == "player" ? "伙伴模式" : "AI 自己玩"), ChangeGameMode),
            (_updateBusy ? "正在处理更新…" : _preparedUpdateRequest != null ? "安装已下载更新" : _availableUpdate != null ? "查看更新" : "检查更新",
                () => { if (_updateBusy) return; if (_preparedUpdateRequest != null || _availableUpdate != null) ShowUpdate(OpenGameSettings); else CheckUpdates(true); }),
            ("重启游戏", () => ShowRestartPrompt(OpenGameSettings, _preparedUpdateRequest)), ("返回", back) });
        Game1.activeClickableMenu = new CompanionOptionsMenu("游戏模式与更新", message, back, choices.ToArray());
    }

    private void ChangeGameMode()
    {
        if (Context.IsMultiplayer && _savedMode != "player")
        {
            Game1.activeClickableMenu = new CompanionOptionsMenu("游戏模式", "AI 自己玩仅支持单人存档。", OpenGameSettings, ("返回", OpenGameSettings));
            return;
        }
        try
        {
            // Keep the running actor binding unchanged until the next process.
            var saved = Helper.ReadConfig<ModConfig>();
            saved.Mode = _savedMode == "player" ? "companion" : "player";
            Helper.WriteConfig(saved);
            _savedMode = saved.Mode;
            ShowRestartPrompt(OpenGameSettings, _preparedUpdateRequest);
        }
        catch (Exception ex)
        {
            Monitor.Log($"Save mode: {ex.Message}", LogLevel.Warn);
            Game1.activeClickableMenu = new CompanionOptionsMenu("设置未保存", "请检查 Mod 目录是否可写，再重试。", OpenGameSettings, ("返回", OpenGameSettings));
        }
    }
}
