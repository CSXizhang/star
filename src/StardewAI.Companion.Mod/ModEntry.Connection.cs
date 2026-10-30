using StardewAI.Companion.Mod.Menus;
using StardewAI.Companion.Mod.Transport;
using StardewModdingAPI;
using StardewValley;

namespace StardewAI.Companion.Mod;

public sealed partial class ModEntry
{
    private readonly CompanionConnectionState _connectionState = new();
    private bool _connectionChoiceShown;
    private long _connectionSentGeneration = -1;

    private void UpdateConnectionSelection()
    {
        if (_connectionState.IsSwitching && _transportServer?.IsChatConnected == true &&
            _transportServer.ChatGeneration != _connectionSentGeneration)
            SendConnectionSelection();
        if (_config.ConnectionSourceChosen || _connectionChoiceShown || Game1.activeClickableMenu != null ||
            Environment.GetEnvironmentVariable("STARDEW_AI_MANUAL_BRIDGE") == "1") return;
        _connectionChoiceShown = true;
        OpenConnectionSelection();
    }

    private void OpenConnectionSelection()
    {
        Game1.activeClickableMenu = new CompanionOptionsMenu("伙伴接入方式",
            _connectionState.Description + "\n连接已有伙伴会沿用原会话；游戏内启动使用已配置的后端。切换会取消当前动作并更换伙伴，已完成成果保留；F9 接管状态不变。",
            OpenGameSettings,
            ("等待已有 AI 伙伴连接", () => SelectConnectionSource("external")),
            ("在游戏内启动伙伴", () => SelectConnectionSource("builtin")),
            ("返回", OpenGameSettings));
    }

    private void SelectConnectionSource(string source)
    {
        if (!_connectionState.IsSwitching && _config.ConnectionSourceChosen && _connectionState.Source == source)
        {
            if (_connectionState.NeedsPersistence && !SaveConnectionSource(source)) return;
            OpenGameSettings(); return;
        }
        // With no service and no previous choice there is no owner to retire.
        if (!_config.ConnectionSourceChosen && _transportServer?.IsChatConnected != true && _ownedBridgeLaunch == null)
        {
            if (!SaveConnectionSource(source)) return;
            _connectionState.Initialize(source);
            TryAutoStartChatBridge();
            OpenGameSettings();
            return;
        }
        string requestId = "connection-" + Guid.NewGuid().ToString("N");
        _connectionState.Begin(requestId, source);
        RequestCancel("connection source switch", out _);
        _deferredResumeMachine = null;
        _thinking?.ReleaseAll("connection-source-switch");
        ProjectBridgeConnection();
        _connectionSentGeneration = -1;
        if (_transportServer?.IsChatConnected != true)
        {
            // Restart only our coordinator, using the requested source so an
            // unavailable built-in backend cannot prevent choosing an external one.
            StopOwnedBridgeLauncher();
            _bridgeStartup.Reset();
            TryAutoStartChatBridge();
        }
        else SendConnectionSelection();
        OpenGameSettings();
    }

    private void SendConnectionSelection()
    {
        string? requestId = _connectionState.PendingRequestId;
        string? source = _connectionState.PendingSource;
        if (requestId == null || source == null) return;
        var server = _transportServer;
        _connectionSentGeneration = server?.ChatGeneration ?? -1;
        _ = Task.Run(async () =>
        {
            bool sent;
            try { sent = server != null && await server.SendConnectionSourceAsync(requestId, source).ConfigureAwait(false); }
            catch (Exception ex) { Monitor.Log($"Source handoff send failed: {ex.Message}", LogLevel.Warn); sent = false; }
            if (!sent) _mainThreadActions.Enqueue(() =>
            {
                if (!ReferenceEquals(_transportServer, server)) return;
                _connectionState.Apply(new(source, "error", requestId));
                ProjectBridgeConnection();
            });
        });
    }

    private bool SaveConnectionSource(string source)
    {
        try
        {
            var saved = Helper.ReadConfig<ModConfig>();
            saved.ConnectionSource = source;
            saved.ConnectionSourceChosen = true;
            Helper.WriteConfig(saved);
            _config.ConnectionSource = source;
            _config.ConnectionSourceChosen = true;
            _connectionState.NoteSaved(source);
            return true;
        }
        catch (Exception ex)
        {
            Monitor.Log($"Save connection source: {ex.Message}", LogLevel.Warn);
            Game1.activeClickableMenu = new CompanionOptionsMenu("接入选择未保存", "请检查 Mod 目录是否可写，再重试。", OpenGameSettings, ("返回", OpenGameSettings));
            return false;
        }
    }

    private void HandleConnectionState(ConnectionStatePayload state)
    {
        bool confirmed = _connectionState.Apply(state);
        if (confirmed)
        {
            // Runtime has retired the previous owner before acknowledging. Never
            // request player authority or clear a user pause as part of connection.
            _config.ConnectionSource = state.Source;
            if (!SaveConnectionSource(state.Source))
            {
                ProjectBridgeConnection();
                return;
            }
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", _connectionState.Description, Microsoft.Xna.Framework.Color.SeaGreen));
        }
        ProjectBridgeConnection();
    }
}
