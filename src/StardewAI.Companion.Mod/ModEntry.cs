using Color = Microsoft.Xna.Framework.Color;
using System.Text.Json.Nodes;
using System.Collections.Concurrent;
using StardewModdingAPI;
using StardewModdingAPI.Events;
using StardewValley;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Discovery;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Menus;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Observation;
using StardewAI.Companion.Mod.Transport;

namespace StardewAI.Companion.Mod;

/// <summary>
/// Main SMAPI Mod entry point.
/// Connects SMAPI lifecycle events to the Mechanics Actor, tick-driven
/// state machine, visible avatar rendering, per-save partition persistence,
/// and WebSocket transport server with local endpoint discovery.
/// </summary>
public sealed class ModEntry : StardewModdingAPI.Mod
{
    private DiscoveryService? _discoveryService;
    private IActorStateRepository? _stateRepository;
    private FarmerMechanicsActor? _actor;
    private CompanionAvatar? _avatar;
    private GameWorldObserver? _observer;
    private SameMapNavigator? _navigator;
    private NormalWateringCanAdapter? _adapter;
    private NormalHarvestAdapter? _harvestAdapter;
    private NormalChestAdapter? _chestAdapter;
    private NormalHoeAdapter? _hoeAdapter;
    private NormalPlantAdapter? _plantAdapter;
    private NormalShippingAdapter? _shippingAdapter;
    private NormalPurchaseAdapter? _purchaseAdapter;
    private NormalNativeActionAdapter? _nativeActionAdapter;
    private CompanionMechanicsCoordinator? _coordinator;
    private WebSocketTransportServer? _transportServer;
    private readonly ConcurrentQueue<Action> _mainThreadActions = new();
    private string? _pendingAutonomyRequestId;
    private DateTime _autonomySentAt;
    private DateTime _chatActivityAt = DateTime.UtcNow;
    private string? _watchedChatRequest;
    private readonly Dictionary<string, string> _autonomyRequestActions = new();

    public override void Entry(IModHelper helper)
    {
        Monitor.Log("Stardew AI Companion initializing Stage 0 mechanics and transport.", LogLevel.Info);

        _discoveryService = new DiscoveryService(helper.DirectoryPath, Monitor);

        helper.Events.GameLoop.SaveLoaded += OnSaveLoaded;
        helper.Events.GameLoop.DayStarted += OnDayStarted;
        helper.Events.GameLoop.UpdateTicked += OnUpdateTicked;
        helper.Events.GameLoop.TimeChanged += OnTimeChanged;
        helper.Events.Display.RenderedWorld += OnRenderedWorld;
        helper.Events.Display.RenderedHud += OnRenderedHud;
        helper.Events.GameLoop.Saving += OnSaving;
        helper.Events.GameLoop.ReturnedToTitle += OnReturnedToTitle;
        helper.Events.Input.ButtonPressed += OnButtonPressed;

        helper.ConsoleCommands.Add("ai_water",
            "Waters tilled unwatered tiles centered at <x> <y> with optional [radius] (0-2, default 0) on the Farm.\nUsage: ai_water <x> <y> [radius]",
            OnCommandWater);

        helper.ConsoleCommands.Add("ai_pause",
            "Pauses the currently executing companion watering task.\nUsage: ai_pause",
            OnCommandPause);

        helper.ConsoleCommands.Add("ai_resume",
            "Resumes a paused companion watering task.\nUsage: ai_resume",
            OnCommandResume);

        helper.ConsoleCommands.Add("ai_cancel",
            "Cancels the currently executing companion watering task.\nUsage: ai_cancel",
            OnCommandCancel);

        helper.ConsoleCommands.Add("ai_status",
            "Displays the current status, position, resources, and progress of the companion.\nUsage: ai_status",
            OnCommandStatus);

        helper.ConsoleCommands.Add("ai_chat",
            "Sends a natural language chat message to the companion agent.\nUsage: ai_chat <text>",
            (cmd, args) => ExecuteInGameCommand(string.Join(" ", args)));
    }

    private void OnSaveLoaded(object? sender, SaveLoadedEventArgs e)
    {
        InitializeCompanion();
    }

    private void OnDayStarted(object? sender, DayStartedEventArgs e)
    {
        if (_actor == null)
        {
            InitializeCompanion();
        }
    }

    private void InitializeCompanion()
    {
        if (!Context.IsWorldReady || Game1.currentLocation == null)
            return;

        try
        {
            // 1. Per-save state partition: scope repository strictly by active save folder
            string saveId = Constants.SaveFolderName ?? "default-save";
            string storageDir = Path.Combine(Helper.DirectoryPath, "data", saveId);
            _stateRepository = new JsonActorStateRepository(storageDir);

            _observer = new GameWorldObserver(Monitor);
            bool isNewCompanion = !_stateRepository.StateExists("companion-1");
            CompanionActorState activeState;
            GameLocation? targetLocation;
            TileCoordinate activeTile;

            if (isNewCompanion)
            {
                // Fresh-state initialization
                string locName = "Farm";
                targetLocation = Game1.getLocationFromName(locName) ?? Game1.currentLocation;
                if (targetLocation == null)
                {
                    Monitor.Log("Cannot place fresh companion: Farm location not found.", LogLevel.Error);
                    return;
                }

                // Compute a legal initial tile for fresh actor
                var candidateTile = new TileCoordinate(64, 15);
                if (!_observer.IsTilePassable(targetLocation.Name, candidateTile))
                {
                    bool foundLegal = false;
                    for (int r = 1; r <= 4 && !foundLegal; r++)
                    {
                        for (int dx = -r; dx <= r && !foundLegal; dx++)
                        {
                            for (int dy = -r; dy <= r && !foundLegal; dy++)
                            {
                                var testTile = new TileCoordinate(candidateTile.X + dx, candidateTile.Y + dy);
                                if (_observer.IsTilePassable(targetLocation.Name, testTile))
                                {
                                    candidateTile = testTile;
                                    foundLegal = true;
                                }
                            }
                        }
                    }
                    if (!foundLegal)
                    {
                        Monitor.Log($"Cannot place fresh companion: Farm tile {candidateTile} and surroundings are impassable.", LogLevel.Error);
                        return;
                    }
                }

                activeTile = candidateTile;
                activeState = CompanionActorState.CreateDefault("companion-1");
                activeState.Pose = new AuthoritativePose(targetLocation.Name, activeTile, FacingDirection.Down);
                _stateRepository.Save(activeState);
                Monitor.Log($"Fresh companion initialized at legal tile {activeTile} on {targetLocation.Name}.", LogLevel.Info);

            }
            else
            {
                // Restoration of existing persisted companion
                activeState = _stateRepository.LoadOrInitializeDefaults("companion-1");
                targetLocation = Game1.getLocationFromName(activeState.Pose.LocationName);
                string? currLocName = !string.IsNullOrWhiteSpace(Game1.currentLocation?.NameOrUniqueName)
                    ? Game1.currentLocation.NameOrUniqueName
                    : Game1.currentLocation?.Name;
                if (targetLocation == null && Game1.currentLocation != null &&
                    string.Equals(currLocName, activeState.Pose.LocationName, StringComparison.OrdinalIgnoreCase))
                {
                    targetLocation = Game1.currentLocation;
                }

                if (targetLocation == null)
                {
                    Monitor.Log($"Cannot restore companion: Saved location '{activeState.Pose.LocationName}' cannot be resolved.", LogLevel.Error);
                    return;
                }

                activeTile = activeState.Pose.Tile;
                // Strict validation: NEVER silently relocate persisted blocked/invalid position
                string locKey = !string.IsNullOrWhiteSpace(targetLocation.NameOrUniqueName)
                    ? targetLocation.NameOrUniqueName
                    : targetLocation.Name;
                if (!_observer.IsTilePassable(locKey, activeTile))
                {
                    Monitor.Log($"Cannot restore companion: Persisted tile {activeTile} on '{locKey}' is blocked/impassable. Halting placement safely without silent relocation.", LogLevel.Error);
                    return;
                }
            }

            // 3. Instantiate persistent independent Farmer Mechanics Actor and equipped WateringCan
            var wateringCan = new WateringCan { WaterLeft = activeState.Water };
            var gameFarmer = new Farmer();
            gameFarmer.Name = "Companion";
            gameFarmer.UniqueMultiplayerID = 9876543210L;
            gameFarmer.Stamina = activeState.Stamina;
            gameFarmer.maxStamina.Value = (int)activeState.MaxStamina;
            gameFarmer.Position = new Microsoft.Xna.Framework.Vector2(activeTile.X * 64, activeTile.Y * 64);
            gameFarmer.faceDirection((int)activeState.Pose.Facing);
            gameFarmer.currentLocation = targetLocation;

            // Visual configuration: setup clothing, hair, skin, and FakeEventActor flags for rendering
            gameFarmer.isFakeEventActor = true;
            gameFarmer.hidden.Value = false;
            gameFarmer.viewingLocation.Value = null;
            gameFarmer.Sprite = new FarmerSprite("Characters\\Farmer\\farmer_base");
            // Farmer.draw renders through FarmerRenderer (not Farmer.Sprite): without a textured
            // renderer the detached farmer is logically present but completely invisible.
            gameFarmer.FarmerRenderer = new FarmerRenderer("Characters\\Farmer\\farmer_base", gameFarmer);
            gameFarmer.changeGender(true);
            gameFarmer.changeShirt("1000");
            gameFarmer.changePantStyle("Basic");
            gameFarmer.changePantsColor(new Microsoft.Xna.Framework.Color(46, 85, 180));
            gameFarmer.changeSkinColor(0);
            gameFarmer.changeHairStyle(0);
            gameFarmer.changeHairColor(new Microsoft.Xna.Framework.Color(137, 70, 48));
            gameFarmer.changeEyeColor(new Microsoft.Xna.Framework.Color(137, 70, 48));
            gameFarmer.changeShoeColor("2");
            gameFarmer.UpdateClothing();

            // Populate hotbar slots explicitly and verify equipped tool identity
            while (gameFarmer.Items.Count < 12)
            {
                gameFarmer.Items.Add(null);
            }
            var hoe = new Hoe();
            int toolSlot = 0;
            gameFarmer.Items[toolSlot] = wateringCan;
            gameFarmer.Items[1] = hoe;
            gameFarmer.CurrentToolIndex = toolSlot;

            _actor = new FarmerMechanicsActor("companion-1", gameFarmer, wateringCan, targetLocation.Name, activeTile, Monitor.Log, hoe: hoe);
            _actor.ApplyPersistentState(activeState);


            _avatar = new CompanionAvatar(_actor, msg => Monitor.Log(msg, LogLevel.Info));
            _navigator = new SameMapNavigator(_observer);
            _adapter = new NormalWateringCanAdapter(_observer, Monitor);
            _harvestAdapter = new NormalHarvestAdapter(_observer, Monitor);
            _chestAdapter = new NormalChestAdapter(_observer, Monitor);
            _hoeAdapter = new NormalHoeAdapter(_observer, Monitor);
            _plantAdapter = new NormalPlantAdapter(_observer, Monitor);
            _shippingAdapter = new NormalShippingAdapter(_observer, Monitor);
            _purchaseAdapter = new NormalPurchaseAdapter(_observer, Monitor);
            _nativeActionAdapter = new NormalNativeActionAdapter(_observer, Monitor);

            _coordinator = new CompanionMechanicsCoordinator(_actor, _observer, _navigator, _adapter, _avatar, Monitor.Log,
                harvestAdapter: _harvestAdapter, chestAdapter: _chestAdapter, hoeAdapter: _hoeAdapter, plantAdapter: _plantAdapter,
                shippingAdapter: _shippingAdapter, purchaseAdapter: _purchaseAdapter, mapGraph: new WorldMapGraph(null, Monitor.Log),
                nativeActionAdapter: _nativeActionAdapter);

            // 4. Start WebSocket server on loopback with rotating session token
            string sessionToken = Guid.NewGuid().ToString("N") + Guid.NewGuid().ToString("N");
            string gameSessionId = Guid.NewGuid().ToString("N")[..8];

            _transportServer = new WebSocketTransportServer(
                port: 0,
                sessionToken: sessionToken,
                handler: _coordinator,
                saveId: saveId,
                gameSessionId: gameSessionId,
                smapiLogger: Monitor.Log
            );

            _coordinator.SetTransportServer(_transportServer);
            _transportServer.OnChatReplyReceived += HandleChatReplyReceived;
            _transportServer.OnAutonomyStateReceived += HandleAutonomyStateReceived;
            _transportServer.OnChatChannelProblem += (request, save, problem) =>
                _mainThreadActions.Enqueue(() => ShowChatChannelProblem(request, save, problem));
            _transportServer.Start();

            // Persist companion state on every task completion, not only at game-save time:
            // save/exit/reload durability must not depend on save-event timing.
            _coordinator.OnExecutionCompleted += () => PersistActorState("task completion");
            _coordinator.OnNotification += OnCompanionNotification;
            _coordinator.OnNativeActionProgress += HandleNativeActionProgress;

            // 5. Publish local restricted discovery (never log sessionToken!)
            _discoveryService?.Publish(saveId, gameSessionId, _transportServer.Port, sessionToken);

            Monitor.Log($"Companion Mechanics Actor and Transport online on port {_transportServer.Port} (Save: {saveId}).", LogLevel.Info);
        }
        catch (Exception ex)
        {
            Monitor.Log($"Failed to initialize companion: {ex}", LogLevel.Error);
        }
    }

    private void OnUpdateTicked(object? sender, UpdateTickedEventArgs e)
    {
        while (_mainThreadActions.TryDequeue(out var action)) action();
        if (_watchedChatRequest != CompanionCommandMenu.CurrentRequestId)
        {
            _watchedChatRequest = CompanionCommandMenu.CurrentRequestId;
            _chatActivityAt = DateTime.UtcNow;
        }
        if (_pendingAutonomyRequestId != null && DateTime.UtcNow - _autonomySentAt > TimeSpan.FromSeconds(30))
            ShowChatChannelProblem(_pendingAutonomyRequestId, null, "模式/控制确认超时，实际结果未知；仍显示上次确认模式，请恢复连接后重试。");
        if (CompanionCommandMenu.IsProcessing && DateTime.UtcNow - _chatActivityAt > TimeSpan.FromSeconds(120))
            ShowChatChannelProblem(CompanionCommandMenu.CurrentRequestId, null, "120秒未收到伙伴进度，结果未确认；可检查连接后重试，或取消原任务。");
        // 0. Defensive initialization: ensure companion is initialized as soon as the world is ready,
        // even if SaveLoaded/DayStarted fired under edge-case timing or custom loaders.
        if (_actor == null && Context.IsWorldReady && Game1.currentLocation != null)
        {
            InitializeCompanion();
        }

        // 1. Process transport messages (cancellation, pause, disconnect) FIRST
        _transportServer?.Update();

        // 2. Only advance game action if not cancelled or paused
        _coordinator?.Update(Game1.currentGameTime, e.Ticks);

        if (Game1.activeClickableMenu is CompanionCommandMenu && _coordinator != null && _coordinator.GetActivityStatus() != "idle")
        {
            CompanionCommandMenu.CurrentStatusText = _coordinator.GetActivityStatus() switch
            {
                "idle" => "待命",
                "paused" => "已暂停",
                var status => status
            };
        }
    }

    private void OnRenderedWorld(object? sender, RenderedWorldEventArgs e)
    {
        _avatar?.Draw(e.SpriteBatch);
    }

    private void OnTimeChanged(object? sender, TimeChangedEventArgs e)
    {
        _coordinator?.OnTimeChanged(e.NewTime);
    }

    private void OnSaving(object? sender, SavingEventArgs e)
    {
        try
        {
            // Quiesce state machine on saving
            if (_coordinator?.StateMachine != null && _coordinator.StateMachine.IsExecuting)
            {
                _coordinator.StateMachine.RequestPause();
                _actor?.Halt();
                Monitor.Log("Quiesced companion state machine for save event.", LogLevel.Info);
            }

            if (_actor != null && _stateRepository != null)
            {
                PersistActorState("save event");
            }
        }
        catch (Exception ex)
        {
            Monitor.Log($"Error during companion saving: {ex}", LogLevel.Error);
        }
    }

    private void PersistActorState(string reason)
    {
        try
        {
            if (_actor != null && _stateRepository != null)
            {
                var state = _actor.CapturePersistentState();
                _stateRepository.Save(state);
                Monitor.Log($"Companion actor state persisted after {reason}.", LogLevel.Debug);
            }
        }
        catch (Exception ex)
        {
            Monitor.Log($"Error persisting companion state ({reason}): {ex}", LogLevel.Error);
        }
    }

    private void OnReturnedToTitle(object? sender, ReturnedToTitleEventArgs e)
    {
        try
        {
            _discoveryService?.Invalidate();

            _transportServer?.Stop();
            _transportServer?.Dispose();
            _transportServer = null;

            _actor = null;
            _avatar = null;
            _coordinator = null;
            _stateRepository = null;
            Monitor.Log("Companion actor and transport shutdown cleanly.", LogLevel.Info);
        }
        catch (Exception ex)
        {
            Monitor.Log($"Error during companion shutdown: {ex}", LogLevel.Warn);
        }
    }

    private void OnCompanionNotification(string message)
    {
        if (Context.IsWorldReady)
        {
            Game1.addHUDMessage(new HUDMessage(message));
        }
    }

    /// <summary>
    /// Structured progress from the native execution state machine (Q5). F8 gets a
    /// real action/phase/completed/total line; when the action has no countable
    /// targets (total 0) no percentage is shown.
    /// </summary>
    private void HandleNativeActionProgress(StardewAI.Companion.Mod.Execution.NativeActionProgress progress)
    {
        if (progress is null)
            return;

        string text = $"行动 {progress.Action} · 阶段 {progress.Phase}";
        if (progress.Total > 0)
            text += $" · 完成 {progress.Completed}/{progress.Total}";
        if (!string.IsNullOrEmpty(progress.ReasonCode))
            text += $" · {progress.ReasonCode}";

        CompanionCommandMenu.StructuredProgressText = text;
        CompanionCommandMenu.CurrentToolName = progress.Action;
    }

    private void OnRenderedHud(object? sender, RenderedHudEventArgs e)
    {
        if (!Context.IsWorldReady || _actor == null || _coordinator == null)
            return;

        string statusText = "AI: " + _coordinator.GetActivityStatus();

        // Position text in screen coordinates at top-left (16, 16) with subtle shadow; never obstructs bottom toolbar
        Microsoft.Xna.Framework.Vector2 pos = new(16f, 16f);
        e.SpriteBatch.DrawString(Game1.smallFont, statusText, new Microsoft.Xna.Framework.Vector2(pos.X + 1, pos.Y + 1), Microsoft.Xna.Framework.Color.Black * 0.75f);
        e.SpriteBatch.DrawString(Game1.smallFont, statusText, pos, Microsoft.Xna.Framework.Color.White);
    }

    private void OnButtonPressed(object? sender, ButtonPressedEventArgs e)
    {
        if (e.Button == SButton.F8)
        {
            if (!Context.IsWorldReady || _actor == null || _coordinator == null)
            {
                return;
            }

            if (Game1.activeClickableMenu != null)
            {
                if (Game1.activeClickableMenu is CompanionCommandMenu)
                {
                    Game1.activeClickableMenu.exitThisMenu(playSound: false);
                }
                return;
            }

            string curLoc = !string.IsNullOrWhiteSpace(Game1.currentLocation?.NameOrUniqueName)
                ? Game1.currentLocation.NameOrUniqueName
                : Game1.currentLocation?.Name ?? "Farm";
            CompanionCommandMenu.AvailableChestOptions = (_observer?.ScanChests(curLoc) ?? Array.Empty<ChestScanInfo>())
                .Select(chest => $"{curLoc}@({chest.Tile.X},{chest.Tile.Y})")
                .Prepend("none")
                .Distinct(StringComparer.Ordinal)
                .ToArray();

            Game1.activeClickableMenu = new CompanionCommandMenu(
                ExecuteInGameCommand,
                RequestPauseMenuAction,
                RequestResumeMenuAction,
                RequestCancelMenuAction,
                ToggleAutonomyMode,
                OpenAutonomySettings
            );
            CompanionCommandMenu.CurrentStatusText = _coordinator.GetActivityStatus() switch
            {
                "idle" => "待命",
                "paused" => "已暂停",
                var status => status
            };
            Monitor.Log("Companion command menu opened (F8).", LogLevel.Info);
        }
    }

    private void ExecuteInGameCommand(string rawInput)
    {
        Monitor.Log($"In-game command submitted: '{rawInput}'", LogLevel.Info);

        if (CompanionCommandHandler.TryParseInGameCommand(rawInput, out var parsed, out _))
        {
            switch (parsed!.Type)
            {
                case InGameCommandType.Water:
                    if (DispatchWaterZone(parsed.X, parsed.Y, parsed.Radius, "menu", out string waterMsg))
                    {
                        Game1.addHUDMessage(new HUDMessage(waterMsg));
                        Monitor.Log(waterMsg, LogLevel.Info);
                        CompanionCommandMenu.ChatHistory.Add(new ChatMessage("伙伴", waterMsg, Microsoft.Xna.Framework.Color.DarkGreen));
                    }
                    else
                    {
                        Game1.addHUDMessage(new HUDMessage(waterMsg));
                        Monitor.Log(waterMsg, LogLevel.Warn);
                        CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", waterMsg, Microsoft.Xna.Framework.Color.Red));
                    }
                    CompanionCommandMenu.IsProcessing = false;
                    CompanionCommandMenu.CurrentStatusText = "原生指令处理结果";
                    break;

                case InGameCommandType.Pause:
                    RequestPauseMenuAction();
                    break;

                case InGameCommandType.Resume:
                    RequestResumeMenuAction();
                    break;

                case InGameCommandType.Cancel:
                    RequestCancelMenuAction();
                    break;

                case InGameCommandType.Status:
                    string summary = GetStatusSummary();
                    Game1.addHUDMessage(new HUDMessage(summary));
                    CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", summary, Microsoft.Xna.Framework.Color.Teal));
                    CompanionCommandMenu.IsProcessing = false;
                    CompanionCommandMenu.CurrentStatusText = "就绪";
                    break;
            }
            return;
        }

        // Natural language chat command dispatched to runtime agent bridge
        DispatchNaturalLanguageChat(rawInput);
    }

    private void DispatchNaturalLanguageChat(string rawInput)
    {
        if (_transportServer == null)
        {
            CompanionCommandMenu.IsProcessing = false;
            CompanionCommandMenu.CurrentStatusText = "未就绪";
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", "错误：传输服务尚未就绪。", Microsoft.Xna.Framework.Color.Red));
            return;
        }

        if (!_transportServer.IsChatConnected)
        {
            TryAutoStartChatBridge();

            CompanionCommandMenu.IsProcessing = false;
            CompanionCommandMenu.CurrentStatusText = "桥接未连接";
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", "提示：后台 AI 伙伴桥接尚未连接。已尝试启动桥接服务，或请双击根目录『启动伙伴服务.cmd』。", Microsoft.Xna.Framework.Color.DarkOrange));
            Game1.addHUDMessage(new HUDMessage("AI 伙伴桥接服务未连接，请启动服务。", HUDMessage.error_type));
            return;
        }

        string reqId = Guid.NewGuid().ToString("N")[..8];
        string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        CompanionCommandMenu.BeginRequest(reqId, saveId, "正在规划");
        var payload = new ChatSubmitPayload(reqId, rawInput, "text", saveId);

        _ = Task.Run(async () =>
        {
            bool ok = await _transportServer.SendChatSubmitAsync(payload).ConfigureAwait(false);
            if (!ok)
            {
                CompanionCommandMenu.IsProcessing = false;
                CompanionCommandMenu.CurrentRequestId = null;
                CompanionCommandMenu.CurrentStatusText = "发送失败";
                Game1.addHUDMessage(new HUDMessage("发送指令至 AI 伙伴桥接失败。", HUDMessage.error_type));
            }
        });
    }

    private void HandleChatReplyReceived(ChatReplyPayload reply)
    {
        if (CompanionCommandMenu.IsCurrentReply(reply.RequestId, reply.SaveId)) _chatActivityAt = DateTime.UtcNow;
        Monitor.Log($"Chat reply received: Status={reply.Status}, Reply='{reply.ReplyText}', Tokens={reply.TokensUsed}", LogLevel.Info);

        if (reply.Status == "processing" && CompanionCommandMenu.CurrentRequestId is null &&
            CompanionCommandMenu.AutonomyMode == "free" &&
            reply.SaveId == (Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString()))
            CompanionCommandMenu.BeginRequest(reply.RequestId, reply.SaveId, "模型正在思考");

        if (reply.Status.StartsWith("job-", StringComparison.OrdinalIgnoreCase))
        {
            if (!CompanionCommandMenu.IsCurrentReply(reply.RequestId, reply.SaveId)) return;
            bool terminal = reply.Status is "job-completed" or "job-failed";
            CompanionCommandMenu.IsProcessing = !terminal;
            CompanionCommandMenu.CurrentStatusText = reply.Status switch {
                "job-completed" => "作业已完成", "job-failed" => "作业未完成",
                "job-waiting" => "作业等待", _ => "原生执行中" };
            CompanionCommandMenu.CurrentActionText = reply.ReplyText;
            if (terminal)
            {
                CompanionCommandMenu.ChatHistory.Add(new ChatMessage("执行结果", reply.ReplyText,
                    reply.Status == "job-completed" ? Color.DarkGreen : Color.Red));
                CompanionCommandMenu.CurrentRequestId = null;
            }
            return;
        }
        if (reply.Status is "selected" or "decision-completed")
        {
            if (!CompanionCommandMenu.IsCurrentReply(reply.RequestId, reply.SaveId)) return;
            bool selected = reply.Status == "selected";
            CompanionCommandMenu.IsProcessing = selected;
            CompanionCommandMenu.CurrentStatusText = selected ? "已选择，等待执行" : "模型回复完成";
            CompanionCommandMenu.CurrentActionText = selected ? "短作业等待原生执行" : null;
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("伙伴", reply.ReplyText, Color.DarkGreen, BuildUsageLine(reply, "本轮用量")));
            if (!selected) CompanionCommandMenu.CurrentRequestId = null;
            return;
        }

        if (string.Equals(reply.Status, "processing", StringComparison.OrdinalIgnoreCase))
        {
            // A late processing reply from an older request/save must never overwrite
            // the visible progress of the request currently on screen.
            if (!CompanionCommandMenu.IsCurrentReply(reply.RequestId, reply.SaveId))
                return;

            CompanionCommandMenu.IsProcessing = true;
            if (!string.IsNullOrWhiteSpace(reply.ReplyText))
                CompanionCommandMenu.CurrentActionText = reply.ReplyText;
            if (!string.IsNullOrWhiteSpace(reply.ToolName))
                CompanionCommandMenu.CurrentToolName = reply.ToolName;
            if (string.IsNullOrEmpty(CompanionCommandMenu.CurrentStatusText) ||
                CompanionCommandMenu.CurrentStatusText == "就绪")
                CompanionCommandMenu.CurrentStatusText = "模型思考 / 观察与选择";
            return;
        }

        if (!CompanionCommandMenu.IsCurrentReply(reply.RequestId, reply.SaveId))
            return;

        if (string.Equals(reply.Status, "completed", StringComparison.OrdinalIgnoreCase))
        {
            CompanionCommandMenu.IsProcessing = false;
            CompanionCommandMenu.CurrentRequestId = null;
            CompanionCommandMenu.CurrentStatusText = "模型回复完成";
            CompanionCommandMenu.CurrentActionText = null;

            string? tokenInfo = BuildUsageLine(reply, "本轮用量");
            CompanionCommandMenu.LastTokenInfo = tokenInfo;
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("伙伴", reply.ReplyText, Microsoft.Xna.Framework.Color.DarkGreen, tokenInfo));
            Game1.addHUDMessage(new HUDMessage($"[AI伙伴] {reply.ReplyText}"));
            return;
        }

        if (string.Equals(reply.Status, "cancelled", StringComparison.OrdinalIgnoreCase))
        {
            CompanionCommandMenu.IsProcessing = false;
            CompanionCommandMenu.CurrentRequestId = null;
            CompanionCommandMenu.CurrentStatusText = "已取消";
            CompanionCommandMenu.CurrentActionText = null;

            string? tokenInfo = BuildUsageLine(reply, "已中止用量");
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("伙伴", reply.ReplyText, Microsoft.Xna.Framework.Color.DarkGoldenrod, tokenInfo));
            Game1.addHUDMessage(new HUDMessage($"[AI伙伴] {reply.ReplyText}"));
            return;
        }

        // Failed or error
        CompanionCommandMenu.IsProcessing = false;
        CompanionCommandMenu.CurrentRequestId = null;
        CompanionCommandMenu.CurrentStatusText = "失败";
        CompanionCommandMenu.CurrentActionText = null;
        string err = !string.IsNullOrEmpty(reply.Error) ? reply.Error : reply.ReplyText;
        CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", $"任务未完成: {err}", Microsoft.Xna.Framework.Color.Red));
        Game1.addHUDMessage(new HUDMessage($"[AI伙伴] {err}", HUDMessage.error_type));
    }

    /// <summary>
    /// Builds the F8 usage line from the real provider fields only: input, cache
    /// read, cache write, output, total and model calls. Unknown stays "未知";
    /// no cost or saving ratio is invented.
    /// </summary>
    private static string? BuildUsageLine(ChatReplyPayload reply, string prefix)
    {
        if (!reply.TokensUsed.HasValue &&
            !reply.PromptTokens.HasValue &&
            !reply.OutputTokens.HasValue &&
            !reply.CachedTokens.HasValue)
        {
            string missingTag = !string.IsNullOrEmpty(reply.UsageSource) ? $" ({reply.UsageSource})" : "";
            return $"用量: 客户端未提供{missingTag}";
        }

        string input = reply.PromptTokens?.ToString("N0") ?? "未知";
        string cacheRead = (reply.CacheReadTokens ?? reply.CachedTokens)?.ToString("N0") ?? "未知";
        string cacheWrite = reply.CacheWriteTokens?.ToString("N0") ?? "未知";
        string output = reply.OutputTokens?.ToString("N0") ?? "未知";
        string total = reply.TokensUsed?.ToString("N0") ?? "未知";
        string calls = reply.ModelCalls?.ToString() ?? "未知";
        string srcTag = !string.IsNullOrEmpty(reply.UsageSource) ? $" [{reply.UsageSource}]" : "";
        return $"{prefix}: {total} tokens (输入: {input}, 缓存读: {cacheRead}, 缓存写: {cacheWrite}, 输出: {output}, 模型调用: {calls}){srcTag}";
    }

    private void ShowChatChannelProblem(string? requestId, string? saveId, string problem)
    {
        if (!string.IsNullOrEmpty(saveId) && saveId != (Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString())) return;
        if (requestId != null && requestId != _pendingAutonomyRequestId && requestId != CompanionCommandMenu.CurrentRequestId) return;
        if (requestId == null || requestId == _pendingAutonomyRequestId)
        {
            if (_pendingAutonomyRequestId != null) _autonomyRequestActions.Remove(_pendingAutonomyRequestId);
            _pendingAutonomyRequestId = null;
            CompanionCommandMenu.ModeChangePending = false;
        }
        CompanionCommandMenu.IsProcessing = false;
        CompanionCommandMenu.CurrentStatusText = "连接/回复异常，可重试";
        CompanionCommandMenu.CurrentActionText = problem;
        CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", problem, Color.Red));
        // Keep the chat request correlation: a delayed real result can still settle it.
    }

    private void ToggleAutonomyMode()
    {
        if (CompanionCommandMenu.ModeChangePending) return;
        string next = CompanionCommandMenu.AutonomyMode == "free" ? "command" : "free";
        CompanionCommandMenu.CurrentStatusText = "正在连接/等待确认";
        _ = SendAutonomyControl("set_mode", new JsonObject { ["mode"] = next });
    }

    private void OpenAutonomySettings()
    {
        CompanionCommandMenu.CurrentStatusText = "等待设置确认";
        _ = SendAutonomyControl("set_preferences", new JsonObject
        {
            ["budget_limit"] = CompanionCommandMenu.DailySpendLimit,
            ["box_preference"] = CompanionCommandMenu.BoxPreference
        });
    }

    private async Task SendAutonomyControl(string action, JsonObject parameters)
    {
        if (_transportServer == null || !_transportServer.IsChatConnected)
        {
            ShowChatChannelProblem(null, null, "伙伴服务未连接，请启动原有伙伴服务后重试。");
            return;
        }
        string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        string requestId = Guid.NewGuid().ToString("N")[..8];
        _pendingAutonomyRequestId = requestId;
        _autonomyRequestActions[requestId] = action;
        _autonomySentAt = DateTime.UtcNow;
        CompanionCommandMenu.ModeChangePending = action == "set_mode";
        try
        {
            bool sent = await _transportServer.SendAutonomyControlAsync(
                new AutonomyControlPayload(requestId, saveId, action, parameters)).ConfigureAwait(false);
            if (!sent) _mainThreadActions.Enqueue(() => ShowChatChannelProblem(requestId, saveId, "控制发送失败，模式未确认；请恢复连接后重试。"));
        }
        catch (Exception ex)
        {
            _mainThreadActions.Enqueue(() => ShowChatChannelProblem(requestId, saveId, $"控制发送失败：{ex.Message}；请重试。"));
        }
    }

    private void HandleAutonomyStateReceived(AutonomyStatePayload state)
    {
        string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        if (!string.Equals(state.SaveId, saveId, StringComparison.Ordinal)) return;
        if (!string.Equals(state.RequestId, _pendingAutonomyRequestId, StringComparison.Ordinal)) return;
        string action = _autonomyRequestActions.TryGetValue(state.RequestId, out string? knownAction) ? knownAction : "unknown";
        _autonomyRequestActions.Remove(state.RequestId);
        Monitor.Log($"[AutonomyAck] requestId={state.RequestId} action={action} status={state.Status} mode={state.Mode} paused={state.Paused} saveId={state.SaveId}", LogLevel.Info);
        _mainThreadActions.Enqueue(() => ApplyAutonomyStateOnMainThread(state, saveId, action));
    }

    private void ApplyAutonomyStateOnMainThread(AutonomyStatePayload state, string saveId, string action)
    {
        if (!string.Equals(state.RequestId, _pendingAutonomyRequestId, StringComparison.Ordinal)) return;
        CompanionCommandMenu.ModeChangePending = false;
        if (!string.Equals(state.Status, "confirmed", StringComparison.OrdinalIgnoreCase) || state.Mode is not ("free" or "command"))
        {
            ShowChatChannelProblem(state.RequestId, state.SaveId, "模式/控制失败：" + (state.Reason ?? "服务拒绝请求"));
            _pendingAutonomyRequestId = null;
            return;
        }
        CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", action == "set_mode"
            ? (state.Mode == "free" ? "已确认：自由模式已开启" : "已确认：自由模式已退出")
            : "控制已确认：" + action, Color.DarkGreen));
        CompanionCommandMenu.AutonomyMode = state.Mode;
        CompanionCommandMenu.AutonomyPaused = state.Paused;
        if (state.Preferences != null)
        {
            if (state.Preferences.TryGetPropertyValue("dailySpendLimit", out var limit) && int.TryParse(limit?.ToString(), out int parsedLimit))
                CompanionCommandMenu.DailySpendLimit = Math.Max(0, parsedLimit);
            if (state.Preferences.TryGetPropertyValue("boxPreference", out var box) && !string.IsNullOrWhiteSpace(box?.ToString()))
                CompanionCommandMenu.BoxPreference = box!.ToString();
        }
        CompanionCommandMenu.CurrentStatusText = state.Paused ? "已暂停" : (state.Mode == "free" ? "自由模式已开启" : "指令模式");
        // F8 fixed progress lines: last plan action, wait reason and waiting conditions
        // come from the real bridge payload.
        CompanionCommandMenu.PlanWaitReason = state.PlanWaitReason;
        CompanionCommandMenu.WaitingConditions = state.WaitingConditions?.Select(condition => condition.DisplayText).ToArray() ?? Array.Empty<string>();
        if (state.LastPlanAction != null)
        {
            string? operation = state.LastPlanAction.TryGetPropertyValue("operation", out var op) ? op?.ToString() : null;
            string? outcome = state.LastPlanAction.TryGetPropertyValue("outcome", out var oc) ? oc?.ToString() : null;
            if (!string.IsNullOrEmpty(operation))
                CompanionCommandMenu.CurrentActionText = $"计划动作 {operation}（{outcome ?? "unknown"}）";
        }
        if (!string.IsNullOrEmpty(state.PlanWaitReason) && string.IsNullOrEmpty(CompanionCommandMenu.CurrentActionText))
            CompanionCommandMenu.CurrentActionText = "等待：" + state.PlanWaitReason;
        if (action == "set_mode" && state.Mode == "free" && !state.Paused && Game1.activeClickableMenu is CompanionCommandMenu)
        {
            Game1.activeClickableMenu.exitThisMenu(playSound: false);
            Game1.addHUDMessage(new HUDMessage("自由模式已开启，伙伴正在安排今天。"));
        }
        _pendingAutonomyRequestId = null;
    }

    private void RequestPauseMenuAction()
    {
        CompanionCommandMenu.AutonomyPaused = true;
        _ = SendAutonomyControl("pause", new JsonObject());
        if (RequestPause(out string msg))
        {
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", msg, Microsoft.Xna.Framework.Color.DarkGoldenrod));
            CompanionCommandMenu.CurrentStatusText = "已暂停";
            Game1.addHUDMessage(new HUDMessage(msg));
        }
    }

    private void RequestResumeMenuAction()
    {
        CompanionCommandMenu.AutonomyPaused = false;
        _ = SendAutonomyControl("resume", new JsonObject());
        if (RequestResume(out string msg))
        {
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", msg, Microsoft.Xna.Framework.Color.SeaGreen));
            CompanionCommandMenu.CurrentStatusText = "已恢复";
            Game1.addHUDMessage(new HUDMessage(msg));
        }
    }

    private void RequestCancelMenuAction()
    {
        _ = SendAutonomyControl("cancel", new JsonObject());
        // 1. Notify background chat bridge to cancel active agent CLI task
        _ = _transportServer?.SendChatCancelAsync(new ChatCancelPayload(CompanionCommandMenu.CurrentRequestId, "Player clicked cancel in menu"));

        // 2. Request cancel on active game skill state machine
        bool cancelledSkill = RequestCancel("menu", out string msg);

        // 3. Immediately reflect cancellation in UI regardless of whether skill was already executing
        CompanionCommandMenu.IsProcessing = false;
        CompanionCommandMenu.CurrentStatusText = "已取消";
        string displayMsg = cancelledSkill ? msg : "已请求取消当前任务。";
        CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", displayMsg, Microsoft.Xna.Framework.Color.Firebrick));
        Game1.addHUDMessage(new HUDMessage(displayMsg));
    }

    private void TryAutoStartChatBridge()
    {
        try
        {
            string repoRoot = Path.GetFullPath(Path.Combine(Helper.DirectoryPath, "..", "..", "..", ".."));
            string scriptPath = Path.Combine(repoRoot, "启动伙伴服务.cmd");
            if (File.Exists(scriptPath))
            {
                var psi = new System.Diagnostics.ProcessStartInfo
                {
                    FileName = scriptPath,
                    Arguments = $"--run-dir \"{Helper.DirectoryPath}\"",
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WorkingDirectory = repoRoot
                };
                System.Diagnostics.Process.Start(psi);
                Monitor.Log("Auto-started companion chat bridge via 启动伙伴服务.cmd", LogLevel.Info);
            }
        }
        catch (Exception ex)
        {
            Monitor.Log($"Could not auto-start companion chat bridge: {ex.Message}", LogLevel.Warn);
        }
    }

    private bool DispatchWaterZone(int x, int y, int radius, string source, out string message)
    {
        if (!Context.IsWorldReady || _actor == null || _coordinator == null || _observer == null)
        {
            message = "Companion is not initialized or world is not ready.";
            return false;
        }

        if (!string.Equals(_actor.LocationName, "Farm", StringComparison.OrdinalIgnoreCase))
        {
            message = $"Companion is on '{_actor.LocationName}'. Watering is restricted to Farm in Stage 0.";
            return false;
        }

        if (_actor.ActiveTaskId != null || _coordinator.StateMachine.IsExecuting)
        {
            string currentTask = _actor.ActiveTaskId ?? _coordinator.StateMachine.CurrentRequest?.TaskId ?? "active";
            message = $"Companion is busy executing task '{currentTask}'. Use pause, cancel, or status.";
            return false;
        }

        var targets = CompanionCommandHandler.FindTilledUnwateredTiles(_observer, "Farm", x, y, radius);
        if (targets.Count == 0)
        {
            message = $"No tilled unwatered tiles found in area centered at ({x}, {y}) with radius {radius}.";
            return false;
        }

        string cmdId = $"cmd-{source}-" + Guid.NewGuid().ToString("N")[..8];
        string taskId = $"task-{source}-" + Guid.NewGuid().ToString("N")[..8];

        var request = new WaterZoneRequest(
            CommandId: cmdId,
            TaskId: taskId,
            LocationId: "Farm",
            TargetTiles: targets,
            MaxStamina: _actor.Stamina,
            MaxWater: _actor.WaterLeft,
            MaxGameMinutes: 120,
            CancelPolicy: "safe-point",
            IdempotencyKey: cmdId,
            ExpectedWorldRevision: _observer.WorldRevision
        );

        if (!_coordinator.TryStartWaterZone(request, envelope: null, out var earlyResult, out var rejectReason))
        {
            message = $"Failed to start watering task: {rejectReason ?? earlyResult?.ErrorMessage ?? "Rejected"}.";
            return false;
        }

        message = $"Dispatched watering task '{taskId}' for {targets.Count} tile(s).";
        return true;
    }

    private bool RequestPause(out string message)
    {
        if (!Context.IsWorldReady || _coordinator == null || !_coordinator.StateMachine.IsExecuting)
        {
            message = "No active watering task to pause.";
            return false;
        }

        if (_coordinator.StateMachine.IsPaused)
        {
            message = "Task is already paused.";
            return false;
        }

        _coordinator.StateMachine.RequestPause();
        message = "Pause requested for active task.";
        return true;
    }

    private bool RequestResume(out string message)
    {
        if (!Context.IsWorldReady || _coordinator == null || !_coordinator.StateMachine.IsExecuting)
        {
            message = "No active watering task to resume.";
            return false;
        }

        if (!_coordinator.StateMachine.IsPaused)
        {
            message = "Task is not paused.";
            return false;
        }

        _coordinator.StateMachine.Resume();
        message = "Resumed active task.";
        return true;
    }

    private bool RequestCancel(string source, out string message)
    {
        if (!Context.IsWorldReady || _coordinator == null || !_coordinator.StateMachine.IsExecuting)
        {
            message = "No active watering task to cancel.";
            return false;
        }

        _coordinator.StateMachine.RequestCancel($"Cancelled via {source}.");
        message = "Cancellation requested for active task.";
        return true;
    }

    private string GetStatusDetails()
    {
        if (!Context.IsWorldReady || _actor == null)
            return "Companion is not initialized or world is not ready.";

        return CompanionCommandHandler.FormatStatusDetails(_actor, _coordinator?.StateMachine);
    }

    private string GetStatusSummary()
    {
        if (!Context.IsWorldReady || _actor == null)
            return "Companion is not initialized or world is not ready.";

        string line = CompanionCommandHandler.FormatStatusLine(
            _coordinator?.StateMachine.IsExecuting == true,
            _coordinator?.StateMachine.IsPaused == true,
            _coordinator?.StateMachine.CurrentTargetIndex ?? 0,
            _coordinator?.StateMachine.TotalTargets ?? 0
        );

        return $"Companion: ({_actor.Tile.X}, {_actor.Tile.Y}) | Stamina: {_actor.Stamina:F0} | Water: {_actor.WaterLeft} | {line}";
    }

    private void OnCommandWater(string command, string[] args)
    {
        if (!CompanionCommandHandler.ParseWaterCommand(args, out int x, out int y, out int radius, out string? error))
        {
            Monitor.Log(error!, LogLevel.Warn);
            return;
        }

        if (DispatchWaterZone(x, y, radius, "console", out string message))
        {
            Monitor.Log($"{message} Use 'ai_status' to monitor progress.", LogLevel.Info);
        }
        else
        {
            Monitor.Log(message, LogLevel.Warn);
        }
    }

    private void OnCommandPause(string command, string[] args)
    {
        if (RequestPause(out string message))
        {
            Monitor.Log(message, LogLevel.Info);
        }
        else
        {
            Monitor.Log(message, LogLevel.Info);
        }
    }

    private void OnCommandResume(string command, string[] args)
    {
        if (RequestResume(out string message))
        {
            Monitor.Log(message, LogLevel.Info);
        }
        else
        {
            Monitor.Log(message, LogLevel.Info);
        }
    }

    private void OnCommandCancel(string command, string[] args)
    {
        if (RequestCancel("console command", out string message))
        {
            Monitor.Log(message, LogLevel.Info);
        }
        else
        {
            Monitor.Log(message, LogLevel.Info);
        }
    }

    private void OnCommandStatus(string command, string[] args)
    {
        Monitor.Log(GetStatusDetails(), LogLevel.Info);
    }
}
