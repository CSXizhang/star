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
    private readonly ChatCommandUiState _chatUiState = new();
    private ISkillExecutionMachine? _localPauseMachine;
    private ISkillExecutionMachine? _deferredResumeMachine;
    private DateTime _autonomySentAt;
    private DateTime _chatActivityAt = DateTime.UtcNow;
    private string? _watchedChatRequest;
    private DateTime _lastBridgeStartAttempt = DateTime.MinValue;

    // -----------------------------------------------------------------------
    // Life-system fields
    // -----------------------------------------------------------------------

    private readonly LifeMenuUiState _lifeMenuUiState = new();
    private CompanionDialogueController? _companionDialogue;
    private readonly CareHintController _careHintController = new();
    private readonly CompanionInteractionDetector _interactionDetector = new();
    private bool _lifeSaveProfileFetched;   // true once life.profile.get was sent for this save
    private bool _lifeOnboardingHudShown;   // avoid repeated HUD nudge

    // §1.8 start-together sequence, ack-chained: ① life.profile.set ②
    // set_preferences(goal) ③ set_mode(free). Each step waits for its
    // confirmation; a failure or timeout aborts with the real state kept, so
    // the player can retry from setup. Ordering lives in LifeStartSequence.
    private readonly LifeStartSequence _lifeStartSequence = new();

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
            "Pauses the currently executing companion task.\nUsage: ai_pause",
            OnCommandPause);

        helper.ConsoleCommands.Add("ai_resume",
            "Resumes a paused companion task.\nUsage: ai_resume",
            OnCommandResume);

        helper.ConsoleCommands.Add("ai_cancel",
            "Cancels the currently executing companion task.\nUsage: ai_cancel",
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

        // Expire care hints from the previous day (§1.7: unread hints are NOT carried over).
        _careHintController.OnDayStarted(GetCurrentGameDate());
        _lifeMenuUiState.MarkAllCareHintsRead();
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
            _transportServer.OnLifeChatReplyReceived += HandleLifeChatReplyReceived;
            _transportServer.OnLifeProfileStateReceived += HandleLifeProfileStateReceived;
            _transportServer.OnLifeMemoryStateReceived += HandleLifeMemoryStateReceived;
            _transportServer.OnLifeCareReceived += HandleLifeCareReceived;
            _transportServer.OnLifeMilestonesStateReceived += HandleLifeMilestonesStateReceived;
            _transportServer.Start();

            // Reset life-session state for the new save
            _lifeSaveProfileFetched = false;
            _lifeOnboardingHudShown = false;
            _lifeStartSequence.Abort();
            _lifeMenuUiState.Reset();
            CompanionCommandMenu.TaskState.Reset();
            _companionDialogue?.Reset();
            _careHintController.OnDayStarted(GetCurrentGameDate());

            // Persist companion state on every task completion, not only at game-save time:
            // save/exit/reload durability must not depend on save-event timing.
            _coordinator.OnExecutionCompleted += () => PersistActorState("task completion");
            _coordinator.OnNotification += OnCompanionNotification;
            _coordinator.OnNativeActionProgress += HandleNativeActionProgress;

            // 5. Publish local restricted discovery (never log sessionToken!)
            _discoveryService?.Publish(saveId, gameSessionId, _transportServer.Port, sessionToken);
            TryAutoStartChatBridge();

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
        if (Context.IsWorldReady) _companionDialogue?.Update();
        if (_watchedChatRequest != _chatUiState.CommandId)
        {
            _watchedChatRequest = _chatUiState.CommandId;
            _chatActivityAt = DateTime.UtcNow;
        }
        if (_chatUiState.PendingControlId != null && DateTime.UtcNow - _autonomySentAt > TimeSpan.FromSeconds(30))
            ShowChatChannelProblem(_chatUiState.PendingControlId, null, "控制确认超时，服务端结果未知；可重试控制。游戏中的动作保持本地实际状态。");
        if (_chatUiState.HasActiveCommand && DateTime.UtcNow - _chatActivityAt > TimeSpan.FromSeconds(120))
        {
            _chatActivityAt = DateTime.UtcNow;
            ShowChatChannelProblem(_chatUiState.CommandId, null, "120秒未收到伙伴进度，结果未确认；请检查连接或取消原任务。");
        }
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
        if (e.IsMultipleOf(15) && _coordinator != null)
        {
            var water = _coordinator.StateMachine;
            var harvest = _coordinator.HarvestMachine;
            if (water.IsExecuting)
                CompanionCommandMenu.TaskState.NativeProgress("浇水", water.IsPaused ? "已暂停" : water.CurrentState.ToString() == "Navigating" ? "前往作物" : "照料作物", water.WateredCount, water.TotalTargets);
            else if (harvest?.IsExecuting == true)
                CompanionCommandMenu.TaskState.NativeProgress("收获", harvest.IsPaused ? "已暂停" : harvest.CurrentState.ToString() == "Navigating" ? "前往作物" : "收获作物", harvest.HarvestedCount, harvest.TotalTargets);
        }
        if (_deferredResumeMachine != null && _deferredResumeMachine.IsPaused)
        {
            _deferredResumeMachine.Resume();
            _deferredResumeMachine = null;
            _localPauseMachine = null;
            _chatUiState.NoteLocalResumed();
            ProjectChatUiState();
        }
        if (_localPauseMachine != null && !_localPauseMachine.IsExecuting)
        {
            _localPauseMachine = null;
            _deferredResumeMachine = null;
            _chatUiState.NoteLocalResumed();
            ProjectChatUiState();
        }

        // 2b. Life profile auto-fetch: once transport is ready and not yet fetched
        if (!_lifeSaveProfileFetched && _transportServer?.IsChatConnected == true && _actor != null)
        {
            _lifeSaveProfileFetched = true;
            string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
            string reqId = Guid.NewGuid().ToString("N")[..8];
            _ = Task.Run(async () =>
            {
                try
                {
                    await _transportServer!.SendLifeProfileGetAsync(
                        new LifeProfileGetPayload(reqId, saveId)).ConfigureAwait(false);
                }
                catch (Exception ex)
                {
                    Monitor.Log($"life.profile.get send failed: {ex.Message}", LogLevel.Warn);
                }
            });
        }

        // 2c. §1.8 start-together timeout: abort with real state kept and retry hint.
        if (_lifeStartSequence.Step != LifeStartStep.Idle &&
            DateTime.UtcNow - _lifeStartSequence.StepSentAtUtc > TimeSpan.FromSeconds(30))
            AbortLifeStart("等待确认超时。");

        // 2d. Drain deferred care hints once nothing blocks the HUD.
        if (Game1.activeClickableMenu == null && !Game1.eventUp)
        {
            foreach (var hint in _careHintController.TryDrainDeferred())
                Game1.addHUDMessage(new HUDMessage("阿星想和你聊聊 — 打开生活菜单查看"));
        }

        if (Game1.activeClickableMenu is CompanionCommandMenu && !_chatUiState.HasActiveCommand && !_chatUiState.HasPendingControl && _coordinator != null && _coordinator.GetActivityStatus() != "idle")
        {
            CompanionCommandMenu.CurrentStatusText = _coordinator.GetActivityStatus() switch
            {
                "idle" => "待命",
                "paused" => "已暂停",
                var status => status
            };
        }
        ProjectChatUiAvailability();
        if (Game1.activeClickableMenu is CompanionCommandMenu && !_chatUiState.HasActiveCommand && !_chatUiState.HasPendingControl && !_chatUiState.IsPaused && !_chatUiState.LocalPauseRequested && _transportServer?.IsChatConnected != true)
            CompanionCommandMenu.CurrentStatusText = "桥接未连接，输入会保留";
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
            _localPauseMachine = null;
            _deferredResumeMachine = null;
            _stateRepository = null;
            _chatUiState.Reset();
            ProjectChatUiState();
            CompanionCommandMenu.DraftText = string.Empty;

            // Life-system reset
            _lifeMenuUiState.Reset();
            CompanionCommandMenu.TaskState.Reset();
            _companionDialogue?.Reset();
            _lifeSaveProfileFetched = false;
            _lifeOnboardingHudShown = false;
            _lifeStartSequence.Abort();

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

        string actionName = progress.Action switch
        {
            "RefillWateringCan" => "加水", "ApplyFertilizer" => "施肥", "ClearDebris" => "清理杂物",
            "PickupItems" => "拾取", "InsertMachine" => "投放机器", "CollectMachine" => "收取机器",
            "PetAnimal" => "抚摸动物", "FeedAnimals" => "喂养动物", "ToggleAnimalDoor" => "开关畜舍门",
            "CollectAnimalProduce" => "收取畜产品", "ChopTree" => "砍树", _ => progress.Action
        };
        string phaseName = progress.Phase switch
        {
            "Navigating" => "前往目标", "Facing" => "面向目标", "Acting" => "执行",
            "Verifying" => "核对结果", "Paused" => "已暂停", "Cancelling" => "取消中",
            _ => progress.Phase
        };
        string text = $"行动 {actionName} · 阶段 {phaseName}";
        if (progress.Total > 0)
            text += $" · 完成 {progress.Completed}/{progress.Total}";
        if (!string.IsNullOrEmpty(progress.ReasonCode))
            text += $" · {progress.ReasonCode}";

        CompanionCommandMenu.StructuredProgressText = text;
        CompanionCommandMenu.CurrentToolName = progress.Action;
        CompanionCommandMenu.TaskState.NativeProgress(actionName, phaseName, progress.Completed, progress.Total);
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
        // Life menu: detect player interacting with companion
        if (e.Button.IsActionButton() && Context.IsWorldReady && _actor != null && Game1.activeClickableMenu == null)
        {
            TryOpenLifeMenuFromInteraction();
        }

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
                    ((CompanionCommandMenu)Game1.activeClickableMenu).PreserveDraft();
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
                OpenAutonomySettings,
                OpenLifeMenu,
                OpenLifeDirections
            );
            ProjectChatUiState();
            if (!_chatUiState.HasActiveCommand && !_chatUiState.HasPendingControl && _coordinator.GetActivityStatus() != "idle")
                CompanionCommandMenu.CurrentStatusText = _coordinator.GetActivityStatus() == "paused" ? "已暂停" : _coordinator.GetActivityStatus();
            if (!_chatUiState.HasActiveCommand && !_chatUiState.HasPendingControl && !_chatUiState.IsPaused && !_chatUiState.LocalPauseRequested && _transportServer?.IsChatConnected != true)
                CompanionCommandMenu.CurrentStatusText = "桥接未连接，输入会保留";
            Monitor.Log("Companion command menu opened (F8).", LogLevel.Info);
            DispatchLifeProfileRefresh();
            if (_transportServer?.IsChatConnected != true) CompanionCommandMenu.TaskState.Disconnected();
        }
    }

    private bool ExecuteInGameCommand(string rawInput)
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
            return true;
        }

        // Natural language chat command dispatched to runtime agent bridge
        return DispatchNaturalLanguageChat(rawInput);
    }

    private bool DispatchNaturalLanguageChat(string rawInput)
    {
        if (_transportServer == null)
        {
            CompanionCommandMenu.CurrentStatusText = "未就绪";
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", "错误：传输服务尚未就绪。", Microsoft.Xna.Framework.Color.Red));
            return false;
        }

        if (!_transportServer.IsChatConnected)
        {
            TryAutoStartChatBridge();

            CompanionCommandMenu.CurrentStatusText = "桥接未连接";
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", "伙伴服务正在连接。稍后再试；若仍未连接，请在已安装的伙伴目录运行『设置星露谷伙伴.cmd』检查模型配置。", Microsoft.Xna.Framework.Color.DarkOrange));
            Game1.addHUDMessage(new HUDMessage("AI 伙伴桥接服务未连接，请启动服务。", HUDMessage.error_type));
            return false;
        }

        string reqId = Guid.NewGuid().ToString("N")[..8];
        string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        if (!_chatUiState.BeginCommand(reqId, saveId)) return false;
        ProjectChatUiState();
        CompanionCommandMenu.CurrentActionText = null;
        CompanionCommandMenu.CurrentToolName = null;
        CompanionCommandMenu.PlanWaitReason = null;
        CompanionCommandMenu.WaitingConditions = Array.Empty<string>();
        CompanionCommandMenu.StructuredProgressText = null;
        CompanionCommandMenu.LastTokenInfo = null;
        var payload = new ChatSubmitPayload(reqId, rawInput, "text", saveId);
        CompanionCommandMenu.TaskState.Begin(rawInput, planning: false);

        _ = Task.Run(async () =>
        {
            bool ok;
            try { ok = await _transportServer.SendChatSubmitAsync(payload).ConfigureAwait(false); }
            catch (Exception ex)
            {
                Monitor.Log($"Chat submit send failed: {ex}", LogLevel.Warn);
                ok = false;
            }
            if (!ok) _mainThreadActions.Enqueue(() =>
            {
                if (!_chatUiState.FailSend(reqId)) return;
                ProjectChatUiState();
                CompanionCommandMenu.DraftText = rawInput;
                CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", "发送失败，原文已保留；请恢复连接后重试。", Color.Red));
                Game1.addHUDMessage(new HUDMessage("发送指令至 AI 伙伴桥接失败；F8 可重试。", HUDMessage.error_type));
            });
        });
        return true;
    }

    private void HandleChatReplyReceived(ChatReplyPayload reply)
    {
        string currentSave = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        if (reply.Status == "processing" && !_chatUiState.HasActiveCommand &&
            !_chatUiState.HasPendingControl && CompanionCommandMenu.AutonomyMode == "free" &&
            string.Equals(reply.SaveId, currentSave, StringComparison.Ordinal))
            _chatUiState.BeginCommand(reply.CommandId ?? reply.RequestId, currentSave, "模型正在思考");

        // NPC preparation jobs share the panel without pretending they were F8 commands.
        if (string.Equals(reply.SaveId, currentSave, StringComparison.Ordinal) && reply.Status.StartsWith("job-", StringComparison.Ordinal))
        {
            if (reply.Status is "job-completed" or "job-failed")
                CompanionCommandMenu.TaskState.Complete(reply.ReplyText, reply.Status == "job-failed");
            else CompanionCommandMenu.TaskState.SetPlanning("正在干活", reply.ReplyText, "关闭面板，让伙伴继续；需要时可暂停或取消。");
        }
        string? previousTurn = _chatUiState.TurnId;
        if (!_chatUiState.ApplyReply(reply.RequestId, reply.CommandId, reply.SaveId, reply.Status, reply.CommandComplete))
            return;
        if (reply.Status is "job-completed" or "job-failed" or "completed" or "failed" or "cancelled")
            CompanionCommandMenu.TaskState.Complete(reply.ReplyText, reply.Status is "failed" or "job-failed");
        else if (reply.Status == "processing")
            CompanionCommandMenu.TaskState.SetPlanning("正在安排", reply.ReplyText, "关闭面板让游戏继续；有进展会更新这里。");
        else if (reply.Status is "job-started" or "job-progress")
            CompanionCommandMenu.TaskState.SetPlanning("正在干活", reply.ReplyText);
        else if (reply.Status is "selected" or "decision-completed")
            CompanionCommandMenu.TaskState.SetPlanning(reply.CommandComplete == true ? "本次结果" : "正在安排", reply.ReplyText);
        _chatActivityAt = DateTime.UtcNow;
        ProjectChatUiState();
        if (previousTurn != null && previousTurn != reply.RequestId)
        {
            CompanionCommandMenu.CurrentActionText = null;
            CompanionCommandMenu.CurrentToolName = null;
            CompanionCommandMenu.StructuredProgressText = null;
        }
        Monitor.Log($"Chat reply received: Status={reply.Status}, Command={reply.CommandId}, Turn={reply.RequestId}, Tokens={reply.TokensUsed}", LogLevel.Info);

        if (reply.Status.StartsWith("job-", StringComparison.OrdinalIgnoreCase))
        {
            CompanionCommandMenu.CurrentActionText = reply.ReplyText;
            if (reply.Status is "job-completed" or "job-failed")
                CompanionCommandMenu.ChatHistory.Add(new ChatMessage("执行结果", reply.ReplyText,
                    reply.Status == "job-completed" ? Color.DarkGreen : Color.Red));
            return;
        }
        if (reply.Status is "selected" or "decision-completed")
        {
            CompanionCommandMenu.CurrentActionText = reply.Status == "selected" ? "短作业等待原生执行" : null;
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("伙伴", reply.ReplyText, Color.DarkGreen, BuildUsageLine(reply, "本轮用量")));
            if (reply.CommandComplete == true) Game1.addHUDMessage(new HUDMessage("AI 伙伴已回复，按 F8 查看。"));
            return;
        }
        if (reply.Status == "processing")
        {
            if (!string.IsNullOrWhiteSpace(reply.ReplyText)) CompanionCommandMenu.CurrentActionText = reply.ReplyText;
            if (!string.IsNullOrWhiteSpace(reply.ToolName)) CompanionCommandMenu.CurrentToolName = reply.ToolName;
            return;
        }

        CompanionCommandMenu.CurrentActionText = null;
        string? tokenInfo = BuildUsageLine(reply, reply.Status == "cancelled" ? "已中止用量" : "本轮用量");
        CompanionCommandMenu.LastTokenInfo = tokenInfo;
        if (reply.Status == "cancelled")
        {
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("伙伴", reply.ReplyText, Color.DarkGoldenrod, tokenInfo));
            Game1.addHUDMessage(new HUDMessage("AI 伙伴任务已取消，按 F8 查看。"));
        }
        else if (reply.Status == "completed")
        {
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("伙伴", reply.ReplyText, Color.DarkGreen, tokenInfo));
            if (reply.CommandComplete != false) Game1.addHUDMessage(new HUDMessage("AI 伙伴已回复，按 F8 查看。"));
        }
        else
        {
            string err = !string.IsNullOrEmpty(reply.Error) ? reply.Error : reply.ReplyText;
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", $"任务未完成: {err}", Color.Red, tokenInfo));
            Game1.addHUDMessage(new HUDMessage("AI 伙伴任务未完成，按 F8 查看。", HUDMessage.error_type));
        }
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

    private void ProjectChatUiState()
    {
        CompanionCommandMenu.CurrentRequestId = _chatUiState.CommandId;
        CompanionCommandMenu.CurrentRequestSaveId = _chatUiState.SaveId;
        CompanionCommandMenu.IsProcessing = _chatUiState.HasActiveCommand;
        CompanionCommandMenu.ControlPending = _chatUiState.HasPendingControl;
        CompanionCommandMenu.PendingControlAction = _chatUiState.PendingControlAction;
        CompanionCommandMenu.ModeChangePending = _chatUiState.PendingControlAction == "set_mode";
        CompanionCommandMenu.AutonomyPaused = _chatUiState.IsPaused;
        CompanionCommandMenu.CurrentStatusText = _chatUiState.StatusText;
        ProjectChatUiAvailability();
    }

    private void ProjectChatUiAvailability()
    {
        CompanionCommandMenu.CanSubmitText = _chatUiState.CanSubmit;
        CompanionCommandMenu.SubmissionBlockReason = _chatUiState.HasPendingControl
            ? "正在等待控制确认，请稍候。"
            : _chatUiState.IsPaused ? "伙伴已暂停，请先点击[继续]。"
            : _chatUiState.LocalPauseRequested ? "游戏动作仍在暂停或等待安全恢复，请点击[继续]。"
            : _chatUiState.HasActiveCommand ? "当前指令仍在执行，请稍候或点击[取消]。"
            : null;
    }

    private void ShowChatChannelProblem(string? requestId, string? saveId, string problem)
    {
        if (!string.IsNullOrEmpty(saveId) && saveId != (Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString())) return;
        bool pendingControl = requestId != null && requestId == _chatUiState.PendingControlId;
        bool activeCommand = requestId != null && (requestId == _chatUiState.CommandId || requestId == _chatUiState.TurnId);
        if (requestId != null && !pendingControl && !activeCommand) return;
        bool controlProblem = pendingControl || requestId == null && _chatUiState.HasPendingControl;
        if (controlProblem)
            _chatUiState.FailControl(_chatUiState.PendingControlId!);
        else _chatUiState.ShowConnectionProblem("连接/回复异常，请检查连接");
        ProjectChatUiState();
        string detail = controlProblem && _chatUiState.LocalPauseRequested
            ? problem + " 游戏动作的暂停请求已发出；仍可点击[继续]请求恢复。"
            : problem;
        CompanionCommandMenu.CurrentActionText = detail;
        CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", detail, Color.Red));
        // Keep an active command correlated until a real terminal or confirmed cancel.
    }

    private void ToggleAutonomyMode()
    {
        if (_chatUiState.HasPendingControl) return;
        string next = CompanionCommandMenu.AutonomyMode == "free" ? "command" : "free";
        _ = SendAutonomyControl("set_mode", new JsonObject { ["mode"] = next });
    }

    private void OpenAutonomySettings()
    {
        _ = SendAutonomyControl("set_preferences", new JsonObject
        {
            ["budget_limit"] = CompanionCommandMenu.DailySpendLimit,
            ["box_preference"] = CompanionCommandMenu.BoxPreference
        });
    }

    private async Task SendAutonomyControl(string action, JsonObject parameters)
    {
        if (_chatUiState.HasPendingControl) return;
        if (_transportServer == null || !_transportServer.IsChatConnected)
        {
            ShowChatChannelProblem(null, null, "伙伴服务未连接，请启动原有伙伴服务后重试。");
            return;
        }
        string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        string requestId = Guid.NewGuid().ToString("N")[..8];
        if (action is "pause" or "resume" or "cancel" && _chatUiState.CommandId is string commandId)
            parameters["commandId"] = commandId;
        if (!_chatUiState.BeginControl(requestId, action)) return;
        _autonomySentAt = DateTime.UtcNow;
        ProjectChatUiState();
        try
        {
            bool sent = await _transportServer.SendAutonomyControlAsync(
                new AutonomyControlPayload(requestId, saveId, action, parameters)).ConfigureAwait(false);
            if (!sent) _mainThreadActions.Enqueue(() => ShowChatChannelProblem(requestId, saveId, "控制发送失败，服务端未确认；请检查连接后重试。"));
        }
        catch (Exception ex)
        {
            _mainThreadActions.Enqueue(() => ShowChatChannelProblem(requestId, saveId, $"控制发送失败：{ex.Message}；请重试。"));
        }
    }

    private void HandleAutonomyStateReceived(AutonomyStatePayload state)
    {
        _mainThreadActions.Enqueue(() =>
        {
            // §1.8 start-together acks are tracked outside the F8 pending-control flow.
            AdvanceLifeStartAfterControl(state.RequestId, state.Status);

            string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
            if (!string.Equals(state.SaveId, saveId, StringComparison.Ordinal)) return;
            if (!string.Equals(state.RequestId, _chatUiState.PendingControlId, StringComparison.Ordinal)) return;
            string action = _chatUiState.PendingControlAction ?? "unknown";
            Monitor.Log($"[AutonomyAck] requestId={state.RequestId} action={action} status={state.Status} mode={state.Mode} paused={state.Paused} saveId={state.SaveId}", LogLevel.Info);
            ApplyAutonomyStateOnMainThread(state, action);
        });
    }

    private void ApplyAutonomyStateOnMainThread(AutonomyStatePayload state, string action)
    {
        if (!string.Equals(state.RequestId, _chatUiState.PendingControlId, StringComparison.Ordinal)) return;
        bool confirmed = string.Equals(state.Status, "confirmed", StringComparison.OrdinalIgnoreCase) && state.Mode is "free" or "command";
        _chatUiState.ApplyControlAck(state.RequestId, confirmed, state.Paused);
        ProjectChatUiState();
        if (!confirmed)
        {
            string reason = "服务未确认" + (string.IsNullOrWhiteSpace(state.Reason) ? "。" : "：" + state.Reason);
            CompanionCommandMenu.CurrentActionText = reason + " 游戏中的动作保持本地实际状态，可重试控制。";
            CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", CompanionCommandMenu.CurrentActionText, Color.Red));
            return;
        }
        string controlResult = action switch
        {
            "set_mode" => state.Mode == "free" ? "已确认：自由模式已开启" : "已确认：自由模式已退出",
            "pause" => state.Paused ? "已确认：伙伴已暂停" : "已确认：暂停请求已处理",
            "resume" => state.Paused ? "已确认：伙伴仍处于暂停" : "已确认：伙伴已继续",
            "cancel" => "已确认：当前指令已取消",
            "set_preferences" => "已确认：设置已保存",
            _ => "控制已确认"
        };
        CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", controlResult, Color.DarkGreen));
        CompanionCommandMenu.AutonomyMode = state.Mode;
        if (state.Preferences != null)
        {
            if (state.Preferences.TryGetPropertyValue("dailySpendLimit", out var limit) && int.TryParse(limit?.ToString(), out int parsedLimit))
                CompanionCommandMenu.DailySpendLimit = Math.Max(0, parsedLimit);
            if (state.Preferences.TryGetPropertyValue("boxPreference", out var box) && !string.IsNullOrWhiteSpace(box?.ToString()))
                CompanionCommandMenu.BoxPreference = box!.ToString();
        }
        if (action == "set_mode") CompanionCommandMenu.CurrentStatusText = state.Paused ? "已暂停" : (state.Mode == "free" ? "自由模式已开启" : "指令模式");
        if (action == "cancel")
        {
            CompanionCommandMenu.CurrentActionText = null;
            CompanionCommandMenu.CurrentToolName = null;
            CompanionCommandMenu.StructuredProgressText = null;
            CompanionCommandMenu.PlanWaitReason = null;
            CompanionCommandMenu.WaitingConditions = Array.Empty<string>();
        }
        // F8 fixed progress lines: last plan action, wait reason and waiting conditions
        // come from the real bridge payload.
        if (action != "cancel")
        {
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
        }
        if (action == "set_mode" && state.Mode == "free" && !state.Paused && Game1.activeClickableMenu is CompanionCommandMenu)
        {
            Game1.activeClickableMenu.exitThisMenu(playSound: false);
            Game1.addHUDMessage(new HUDMessage("自由模式已开启，伙伴正在安排今天。"));
        }
    }

    private void RequestPauseMenuAction()
    {
        if (_chatUiState.HasPendingControl) return;
        bool local = RequestPause(out string msg);
        if (local)
        {
            _localPauseMachine = _coordinator?.ActiveMachine;
            _deferredResumeMachine = null;
            _chatUiState.NoteLocalPauseRequested();
        }
        _ = SendAutonomyControl("pause", new JsonObject());
        if (local) CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", "游戏中的动作已请求暂停；等待伙伴服务确认。", Color.DarkGoldenrod));
    }

    private void RequestResumeMenuAction()
    {
        if (_chatUiState.HasPendingControl) return;
        bool local = RequestResume(out string msg);
        _ = SendAutonomyControl("resume", new JsonObject());
        if (local) CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", "游戏中的动作已请求继续；等待伙伴服务确认。", Color.SeaGreen));
    }

    private void RequestCancelMenuAction()
    {
        if (_chatUiState.HasPendingControl) return;
        bool local = RequestCancel("menu", out string msg);
        _ = SendAutonomyControl("cancel", new JsonObject());
        CompanionCommandMenu.ChatHistory.Add(new ChatMessage("系统", local
            ? "游戏中的动作已收到取消请求；等待伙伴服务确认。"
            : "正在请求取消当前指令；等待伙伴服务确认。", Color.Firebrick));
    }

    private void TryAutoStartChatBridge()
    {
        if (_transportServer?.IsChatConnected == true || DateTime.UtcNow - _lastBridgeStartAttempt < TimeSpan.FromSeconds(30)) return;
        try
        {
            var start = ReleaseBridgeLauncher.CreateStartInfo(Helper.DirectoryPath);
            if (start == null) return; // Source builds use the documented developer launcher.
            _lastBridgeStartAttempt = DateTime.UtcNow;
            using var process = System.Diagnostics.Process.Start(start);
            Monitor.Log("Started installed companion service launcher; waiting for connection.", LogLevel.Info);
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
        var machine = Context.IsWorldReady ? _coordinator?.ActiveMachine : null;
        if (machine == null)
        {
            message = "No active companion task to pause.";
            return false;
        }

        if (machine.IsPaused)
        {
            message = "Task is already paused.";
            return false;
        }

        machine.RequestPause();
        message = "Pause requested for active task.";
        return true;
    }

    private bool RequestResume(out string message)
    {
        var machine = Context.IsWorldReady ? _coordinator?.ActiveMachine : null;
        if (machine == null)
        {
            message = "No active companion task to resume.";
            return false;
        }

        if (!machine.IsPaused && ReferenceEquals(machine, _localPauseMachine))
        {
            _deferredResumeMachine = machine;
            message = "Resume requested; waiting for the native safe pause point.";
            return true;
        }

        if (!machine.IsPaused)
        {
            message = "Task is not paused.";
            return false;
        }

        machine.Resume();
        if (ReferenceEquals(machine, _localPauseMachine))
        {
            _localPauseMachine = null;
            _deferredResumeMachine = null;
            _chatUiState.NoteLocalResumed();
        }
        message = "Resumed active task.";
        return true;
    }

    private bool RequestCancel(string source, out string message)
    {
        var machine = Context.IsWorldReady ? _coordinator?.ActiveMachine : null;
        if (machine == null)
        {
            message = "No active companion task to cancel.";
            return false;
        }

        machine.RequestCancel($"Cancelled via {source}.");
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

    // =========================================================================
    // Life-system methods
    // =========================================================================

    /// <summary>
    /// Checks if the player is close enough to the companion and facing it,
    /// then opens the life menu.
    /// </summary>
    private void TryOpenLifeMenuFromInteraction()
    {
        if (_actor == null || !Context.IsWorldReady) return;

        // Get player tile and facing
        var player = Game1.player;
        var playerTile = new Domain.TileCoordinate((int)player.Tile.X, (int)player.Tile.Y);
        var cursorTile = new Domain.TileCoordinate(
            (int)Game1.currentCursorTile.X,
            (int)Game1.currentCursorTile.Y);
        var companionTile = _actor.Tile;

        bool interactionPressed = true; // already inside OnButtonPressed guard

        if (_interactionDetector.ShouldOpenLifeMenu(
            playerTile, player.FacingDirection, cursorTile, companionTile, interactionPressed))
        {
            OpenLifeMenu();
        }
    }

    /// <summary>
    /// Opens the <see cref="CompanionLifeMenu"/>.
    /// </summary>
    private void OpenLifeMenu()
    {
        TryAutoStartChatBridge();
        GetCompanionDialogue().Open();
    }

    private void OpenLifeDirections()
    {
        TryAutoStartChatBridge();
        GetCompanionDialogue().OpenDirections();
    }

    private CompanionDialogueController GetCompanionDialogue()
    {
        _companionDialogue ??= new CompanionDialogueController(
            _lifeMenuUiState, DispatchLifeChat,
            () =>
            {
                DispatchLifeProfileRefresh(); DispatchLifeMilestonesRefresh();
                if (!_lifeMenuUiState.IsOnboarded && !_lifeMenuUiState.IsSkipped) DispatchMemoryListRefresh();
            },
            () =>
            {
                OpenSetupMenu();
                if (Game1.activeClickableMenu != null)
                    Game1.activeClickableMenu.exitFunction = () => _companionDialogue?.ReturnToConversation();
            },
            OpenCompanionMemory, () => _actor?.GameFarmer,
            SendFirstMeetingProfile,
            text => DispatchMemoryEdit("add", null, "preference", text) ? _lifeMenuUiState.PendingMemoryEditRequestId : null,
            () => !_lifeMenuUiState.WorkPaused && !_chatUiState.IsPaused && !_chatUiState.LocalPauseRequested &&
                !_chatUiState.HasActiveCommand && !_chatUiState.HasPendingControl &&
                !_lifeMenuUiState.IsChatPending && (_coordinator == null || _coordinator.GetActivityStatus() == "idle"),
            OpenTaskPanel);
        return _companionDialogue;
    }

    private void OpenTaskPanel()
    {
        DispatchLifeProfileRefresh();
        Game1.activeClickableMenu = new CompanionCommandMenu(ExecuteInGameCommand,
            RequestPauseMenuAction, RequestResumeMenuAction, RequestCancelMenuAction,
            ToggleAutonomyMode, OpenAutonomySettings, OpenLifeMenu, OpenLifeDirections);
        ProjectChatUiState();
        if (_transportServer?.IsChatConnected != true) CompanionCommandMenu.TaskState.Disconnected();
    }

    private void OpenCompanionMemory()
    {
        if (Game1.activeClickableMenu is CompanionLifeMenu) return;

        Game1.activeClickableMenu = new CompanionLifeMenu(
            _lifeMenuUiState,
            onSubmitLifeChat: (text, mode) => DispatchLifeChat(text, mode),
            onOpenCommandMenu: prefill =>
            {
                // Pre-populate F8 draft and open command menu
                CompanionCommandMenu.DraftText = prefill;
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
                    OpenAutonomySettings, OpenLifeMenu, OpenLifeDirections);
                ProjectChatUiState();
            },
            onOpenSetup: OpenSetupMenu,
            onRefreshWork: DispatchLifeProfileRefresh,
            onRefreshMemory: DispatchMemoryListRefresh,
            onRefreshMilestones: DispatchLifeMilestonesRefresh,
            onMemoryEdit: (op, id, kind, text) => DispatchMemoryEdit(op, id, kind, text));

        if (Game1.activeClickableMenu is CompanionLifeMenu memoryMenu)
        {
            memoryMenu.OpenMemoryOnly();
            memoryMenu.exitFunction = () => _companionDialogue?.ReturnToConversation();
        }
        Monitor.Log("Companion memory panel opened.", LogLevel.Info);
    }

    /// <summary>
    /// Opens the <see cref="CompanionSetupMenu"/>.
    /// </summary>
    private void OpenSetupMenu()
    {
        Game1.activeClickableMenu = new CompanionSetupMenu(
            companionName: _lifeMenuUiState.CompanionName,
            playStyle: _lifeMenuUiState.PlayStyle,
            personality: _lifeMenuUiState.Personality,
            careFrequency: _lifeMenuUiState.CareFrequency,
            dailySpendLimit: _lifeMenuUiState.DailySpendLimit ?? CompanionCommandMenu.DailySpendLimit,
            currentWorkMode: _lifeMenuUiState.WorkMode,
            liveState: _lifeMenuUiState,
            onSave: (name, style, pers, freq) => SendLifeProfileSetOnly(name, style, pers, freq),
            onStart: (name, style, pers, freq) => GetCompanionDialogue().SaveSettingsAndPlan(name, style, pers, freq),
            onSkip: () => SendLifeProfileSkip());
    }

    // -----------------------------------------------------------------------
    // Life-chat dispatch
    // -----------------------------------------------------------------------

    private void DispatchLifeProfileRefresh()
    {
        if (_transportServer == null || !_transportServer.IsChatConnected) return;
        string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        string reqId = Guid.NewGuid().ToString("N")[..8];
        _ = Task.Run(async () =>
        {
            try { await _transportServer.SendLifeProfileGetAsync(new LifeProfileGetPayload(reqId, saveId)).ConfigureAwait(false); }
            catch (Exception ex) { Monitor.Log($"life.profile.get refresh failed: {ex.Message}", LogLevel.Warn); }
        });
    }

    private void DispatchLifeMilestonesRefresh()
    {
        if (_transportServer == null || !_transportServer.IsChatConnected) return;
        string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        string reqId = Guid.NewGuid().ToString("N")[..8];
        _ = Task.Run(async () =>
        {
            try { await _transportServer.SendLifeMilestonesGetAsync(new LifeMilestonesGetPayload(reqId, saveId)).ConfigureAwait(false); }
            catch (Exception ex) { Monitor.Log($"life.milestones.get send failed: {ex.Message}", LogLevel.Warn); }
        });
    }

    private bool DispatchLifeChat(string text, string mode, string? acceptedNodeId = null)
    {
        if (_transportServer == null || !_transportServer.IsChatConnected) return false;
        string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        string reqId = Guid.NewGuid().ToString("N")[..8];
        if (!_lifeMenuUiState.BeginChat(reqId, mode)) return false;

        var payload = new LifeChatSubmitPayload(reqId, saveId, mode, text, AcceptedNodeId: acceptedNodeId);
        if (mode == "plan") CompanionCommandMenu.TaskState.Begin(acceptedNodeId == null ? "正在结合农场现状商量方案。" : "正在保存你认可的方案。", planning: true);
        _ = Task.Run(async () =>
        {
            try { await _transportServer.SendLifeChatSubmitAsync(payload).ConfigureAwait(false); }
            catch (Exception ex) { Monitor.Log($"life.chat.submit send failed: {ex.Message}", LogLevel.Warn); }
        });
        return true;
    }

    // -----------------------------------------------------------------------
    // Memory edit dispatch
    // -----------------------------------------------------------------------

    private void DispatchMemoryListRefresh()
    {
        if (_transportServer == null || !_transportServer.IsChatConnected) return;
        string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        string reqId = Guid.NewGuid().ToString("N")[..8];
        _ = Task.Run(async () =>
        {
            try { await _transportServer.SendLifeMemoryListAsync(new LifeMemoryListPayload(reqId, saveId)).ConfigureAwait(false); }
            catch (Exception ex) { Monitor.Log($"life.memory.list send failed: {ex.Message}", LogLevel.Warn); }
        });
    }

    private bool DispatchMemoryEdit(string op, string? id, string? kind, string? text)
    {
        if (_transportServer == null || !_transportServer.IsChatConnected) return false;
        string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        string reqId = Guid.NewGuid().ToString("N")[..8];
        _lifeMenuUiState.BeginMemoryEdit(reqId);

        var payload = new LifeMemoryEditPayload(reqId, saveId, _lifeMenuUiState.MemoryRevision, op, id, kind, text);
        _ = Task.Run(async () =>
        {
            try { await _transportServer.SendLifeMemoryEditAsync(payload).ConfigureAwait(false); }
            catch (Exception ex) { Monitor.Log($"life.memory.edit send failed: {ex.Message}", LogLevel.Warn); }
        });
        return true;
    }

    // -----------------------------------------------------------------------
    // Profile set helpers
    // -----------------------------------------------------------------------

    private void SendLifeProfileSetOnly(string name, string style, string personality, string careFreq)
    {
        if (_transportServer == null) return;
        string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        string reqId = Guid.NewGuid().ToString("N")[..8];
        int expected = _lifeMenuUiState.ProfileRevision;
        _lifeMenuUiState.BeginProfileSet(reqId, expected);

        var patch = new LifeProfilePatchDto(
            CompanionName: name,
            PlayStyle: style,
            Personality: personality,
            CareFrequency: careFreq);
        var payload = new LifeProfileSetPayload(reqId, saveId, expected, patch);
        _ = Task.Run(async () =>
        {
            try { await _transportServer.SendLifeProfileSetAsync(payload).ConfigureAwait(false); }
            catch (Exception ex) { Monitor.Log($"life.profile.set send failed: {ex.Message}", LogLevel.Warn); }
        });
    }

    private string? SendFirstMeetingProfile(LifeProfilePatchDto patch)
    {
        if (_transportServer?.IsChatConnected != true || !_lifeMenuUiState.HasProfileState ||
            _lifeMenuUiState.PendingProfileSetRequestId != null) return null;
        string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        string reqId = Guid.NewGuid().ToString("N")[..8];
        int expected = _lifeMenuUiState.ProfileRevision;
        _lifeMenuUiState.BeginProfileSet(reqId, expected);
        _ = Task.Run(async () =>
        {
            bool sent;
            try { sent = await _transportServer.SendLifeProfileSetAsync(new LifeProfileSetPayload(reqId, saveId, expected, patch)).ConfigureAwait(false); }
            catch (Exception ex) { Monitor.Log($"First meeting save failed: {ex.Message}", LogLevel.Warn); sent = false; }
            if (!sent) _mainThreadActions.Enqueue(() =>
            {
                _lifeMenuUiState.EndProfileSet(reqId);
                _companionDialogue?.ReceiveProfile(reqId, "failed");
            });
        });
        return reqId;
    }

    private void SendLifeProfileSkip()
    {
        if (_transportServer == null) return;
        string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        string reqId = Guid.NewGuid().ToString("N")[..8];
        int expected = _lifeMenuUiState.ProfileRevision;
        _lifeMenuUiState.BeginProfileSet(reqId, expected);

        var patch = new LifeProfilePatchDto(Skipped: true);
        var payload = new LifeProfileSetPayload(reqId, saveId, expected, patch);
        _ = Task.Run(async () =>
        {
            try { await _transportServer!.SendLifeProfileSetAsync(payload).ConfigureAwait(false); }
            catch (Exception ex) { Monitor.Log($"life.profile.set(skip) send failed: {ex.Message}", LogLevel.Warn); }
        });
    }

    /// <summary>
    /// §1.8 start sequence, ack-chained: ① persist profile (onboarded=true), wait for
    /// life.profile.state confirmed; ② autonomy set_preferences(goal), wait for ack;
    /// ③ autonomy set_mode(free), wait for ack. The goal is confirmed before free
    /// mode so autonomy never dispatches work under a stale goal. The final HUD
    /// celebration only fires after all three confirmations; any rejection/timeout
    /// aborts with the real state kept and a retry hint (setup menu can re-trigger).
    /// Ordering/stepping lives in <see cref="LifeStartSequence"/>; this method only
    /// wires sends and rendering.
    /// </summary>
    private void SendLifeStart(string name, string style, string personality, string careFreq)
    {
        if (_transportServer == null) return;
        string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
        string reqId = Guid.NewGuid().ToString("N")[..8];
        int expected = _lifeMenuUiState.ProfileRevision;
        _lifeMenuUiState.BeginProfileSet(reqId, expected);

        _lifeStartSequence.Begin(saveId, name, style, reqId);
        _lifeStartSequence.StepSentAtUtc = DateTime.UtcNow;

        var patch = new LifeProfilePatchDto(
            Onboarded: true,
            Skipped: false,
            CompanionName: name,
            PlayStyle: style,
            Personality: personality,
            CareFrequency: careFreq);
        var profilePayload = new LifeProfileSetPayload(reqId, saveId, expected, patch);

        _ = Task.Run(async () =>
        {
            try
            {
                bool ok = await _transportServer.SendLifeProfileSetAsync(profilePayload).ConfigureAwait(false);
                if (!ok) _mainThreadActions.Enqueue(() => AbortLifeStart("设置保存未送达，请检查连接。"));
            }
            catch (Exception ex)
            {
                Monitor.Log($"life.profile.set(start) send failed: {ex.Message}", LogLevel.Warn);
                _mainThreadActions.Enqueue(() => AbortLifeStart("设置保存发送失败，请检查连接。"));
            }
        });
    }

    private void AdvanceLifeStartAfterProfile(string requestId, string status, string? reason)
    {
        var result = _lifeStartSequence.ApplyProfileState(requestId, status, reason);
        switch (result)
        {
            case LifeStartAdvance.Ignore:
                return;
            case LifeStartAdvance.Abort:
                AbortLifeStart(_lifeStartSequence.AbortReason ?? "设置未确认。");
                return;
            case LifeStartAdvance.SendGoalPreference:
                SendLifeStartControl(result, new JsonObject { ["goal"] = _lifeStartSequence.Goal }, "目标设置");
                return;
        }
    }

    private void AdvanceLifeStartAfterControl(string requestId, string status)
    {
        var result = _lifeStartSequence.ApplyControlAck(requestId, status);
        switch (result)
        {
            case LifeStartAdvance.Ignore:
                return;
            case LifeStartAdvance.Abort:
                AbortLifeStart(_lifeStartSequence.AbortReason ?? "设置未确认。");
                return;
            case LifeStartAdvance.SendModeFree:
                SendLifeStartControl(result, new JsonObject { ["mode"] = "free" }, "自由模式开启");
                return;
            case LifeStartAdvance.Complete:
                Game1.addHUDMessage(new HUDMessage($"和{_lifeStartSequence.CompanionName}一起生活开始了！"));
                // The control ack is authoritative, but the setup/life menus read
                // their mode and plan from life.profile.state. Refresh that view now.
                DispatchLifeProfileRefresh();
                return;
        }
    }

    /// <summary>
    /// Sends one §1.8 autonomy control step. The sequence has already advanced to
    /// the matching await-step; a send failure aborts with the real state kept.
    /// </summary>
    private void SendLifeStartControl(LifeStartAdvance step, JsonObject parameters, string failureNoun)
    {
        string action = step == LifeStartAdvance.SendGoalPreference ? "set_preferences" : "set_mode";
        string reqId = step == LifeStartAdvance.SendGoalPreference
            ? _lifeStartSequence.GoalRequestId!
            : _lifeStartSequence.ModeRequestId!;
        var payload = new AutonomyControlPayload(reqId, _lifeStartSequence.SaveId ?? string.Empty, action, parameters);
        _lifeStartSequence.StepSentAtUtc = DateTime.UtcNow;
        _ = Task.Run(async () =>
        {
            try
            {
                bool ok = await _transportServer!.SendAutonomyControlAsync(payload).ConfigureAwait(false);
                if (!ok) _mainThreadActions.Enqueue(() => AbortLifeStart($"{failureNoun}未送达，请检查连接。"));
            }
            catch (Exception ex)
            {
                Monitor.Log($"life.start {action} send failed: {ex.Message}", LogLevel.Warn);
                _mainThreadActions.Enqueue(() => AbortLifeStart($"{failureNoun}发送失败，请检查连接。"));
            }
        });
    }

    private void AbortLifeStart(string reason)
    {
        if (_lifeStartSequence.Step == LifeStartStep.Idle) return;
        _lifeStartSequence.Abort();
        Game1.addHUDMessage(new HUDMessage($"开始一起生活未完成：{reason} 游戏内状态保持真实结果，可在「伙伴设置」中重试。", HUDMessage.error_type));
    }

    // -----------------------------------------------------------------------
    // Life event handlers (called from main thread via event dispatch in Update)
    // -----------------------------------------------------------------------

    private void HandleLifeChatReplyReceived(LifeChatReplyPayload reply)
    {
        _mainThreadActions.Enqueue(() =>
        {
            bool accepted = _lifeMenuUiState.ApplyChatReply(
                reply.RequestId,
                reply.Status,
                reply.ReplyText,
                reply.QueuePosition,
                reply.Error,
                reply.ProfileRevision,
                reply.MemoryRevision);
            if (accepted)
            {
                _companionDialogue?.Receive(reply.RequestId, reply.Status, reply.ReplyText, reply.ProposalReady, reply.ProposalNodeId);
                if (reply.Activity != null)
                    CompanionCommandMenu.TaskState.ApplyActivity(reply.Activity.Phase, reply.Activity.Summary, reply.Activity.NextStep);
            }

            Monitor.Log($"life.chat.reply: status={reply.Status} reqId={reply.RequestId}", LogLevel.Debug);
        });
    }

    private void HandleLifeProfileStateReceived(LifeProfileStatePayload state)
    {
        _mainThreadActions.Enqueue(() =>
        {
            string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
            if (!string.Equals(state.SaveId, saveId, StringComparison.Ordinal)) return;

            bool isStandaloneSaveReply = _lifeStartSequence.Step == LifeStartStep.Idle &&
                state.RequestId == _lifeMenuUiState.PendingProfileSetRequestId;
            _lifeMenuUiState.MarkProfileStateReceived();
            _lifeMenuUiState.ApplyWorkProjection(
                state.Work.Goal,
                state.Work.ActiveGoals?.Select(goal => goal.Text) ?? Enumerable.Empty<string>(),
                state.Work.RecentTodos?.Select(todo => todo.Intent) ?? Enumerable.Empty<string>(),
                state.Work.WaitingConditions ?? Enumerable.Empty<string>(),
                state.Work.PlanWaitReason);

            if (state.Profile != null)
            {
                _lifeMenuUiState.ApplyProfileState(
                    state.Profile.Onboarded,
                    state.Profile.Skipped,
                    state.Profile.CompanionName,
                    state.Profile.PlayStyle,
                    state.Profile.Personality,
                    state.Profile.CareFrequency,
                    state.ProfileRevision,
                    state.Work.Mode,
                    state.Work.Paused,
                    state.Work.DailySpendLimit,
                    // Keep the memory revision already learned from life.memory.state;
                    // profile.state does not carry it.
                    memoryRevision: _lifeMenuUiState.MemoryRevision,
                    requestId: state.RequestId);

                // HUD onboarding nudge: not onboarded and not skipped
                if (!state.Profile.Onboarded && !state.Profile.Skipped && !_lifeOnboardingHudShown)
                {
                    _lifeOnboardingHudShown = true;
                    Game1.addHUDMessage(new HUDMessage("新伙伴想认识你，走近按互动键聊聊吧。"));
                }
            }
            else
            {
                // profile null = not onboarded yet
                if (!_lifeOnboardingHudShown)
                {
                    _lifeOnboardingHudShown = true;
                    Game1.addHUDMessage(new HUDMessage("新伙伴想认识你，走近按互动键聊聊吧。"));
                }
            }

            AdvanceLifeStartAfterProfile(state.RequestId, state.Status, state.Reason);
            CompanionCommandMenu.TaskState.ApplyProjection(_lifeMenuUiState.PlayStyle,
                state.Work.ActiveGoals?.FirstOrDefault()?.Text ?? state.Work.Goal,
                state.Work.WaitingConditions ?? new List<string>(), state.Work.PlanWaitReason, state.Work.Paused);
            if (state.Work.Activity != null)
                CompanionCommandMenu.TaskState.ApplyActivity(state.Work.Activity.Phase, state.Work.Activity.Summary, state.Work.Activity.NextStep);
            _lifeMenuUiState.EndProfileSet(state.RequestId);
            _companionDialogue?.ReceiveProfile(state.RequestId, state.Status);

            if (isStandaloneSaveReply)
            {
                string message = state.Status == "confirmed"
                    ? "伙伴设置已保存。"
                    : state.Reason == "STALE_REVISION"
                        ? "伙伴设置已变化，请重新打开设置后重试。"
                        : "伙伴设置未保存，请重试。";
                Game1.addHUDMessage(new HUDMessage(message,
                    state.Status == "confirmed" ? HUDMessage.newQuest_type : HUDMessage.error_type));
            }

            Monitor.Log($"life.profile.state received: revision={state.ProfileRevision} onboarded={state.Profile?.Onboarded}", LogLevel.Debug);
        });
    }

    private void HandleLifeMemoryStateReceived(LifeMemoryStatePayload state)
    {
        _mainThreadActions.Enqueue(() =>
        {
            string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
            if (!string.Equals(state.SaveId, saveId, StringComparison.Ordinal)) return;

            bool isEditReply = state.RequestId == _lifeMenuUiState.PendingMemoryEditRequestId;
            _lifeMenuUiState.ApplyMemoryState(
                state.MemoryRevision,
                state.Entries.Select(e => new Menus.MemoryEntrySnapshot(
                    e.Id, e.Kind, e.Text, e.Source, e.GameDate, e.CreatedAt)),
                state.RequestId, state.Status, state.Reason);
            _companionDialogue?.ReceiveMemory(state.RequestId, state.Status);
            if (isEditReply && _lifeMenuUiState.MemoryEditFeedback != null)
                Game1.addHUDMessage(new HUDMessage(_lifeMenuUiState.MemoryEditFeedback,
                    state.Status == "confirmed" ? HUDMessage.newQuest_type : HUDMessage.error_type));

            Monitor.Log($"life.memory.state received: revision={state.MemoryRevision} count={state.Entries.Count}", LogLevel.Debug);
        });
    }

    private void HandleLifeMilestonesStateReceived(LifeMilestonesStatePayload state)
    {
        _mainThreadActions.Enqueue(() =>
        {
            string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
            if (!string.Equals(state.SaveId, saveId, StringComparison.Ordinal)) return;

            if (!string.Equals(state.Status, "ok", StringComparison.Ordinal))
            {
                // Keep the previous snapshot; a failed refresh must not wipe known nodes.
                Monitor.Log($"life.milestones.state failed: {state.Error}", LogLevel.Debug);
                return;
            }

            _lifeMenuUiState.ApplyMilestoneState((state.Nodes ?? new List<MilestoneNodeDto>())
                .Select(n => new Menus.MilestoneNodeSnapshot(
                    n.Id,
                    n.Title,
                    n.Status,
                    n.Verification,
                    n.TargetDate,
                    n.DaysUntil,
                    n.Summary,
                    n.SourceUrl,
                    (n.PrepItems ?? new List<MilestonePrepItemDto>())
                        .Select(p => new Menus.MilestonePrepItemSnapshot(p.Key, p.Label, p.Support, p.Status, p.Note))
                        .ToArray(),
                    n.ReservedFunds,
                    n.PlannedCount,
                    n.TermsNote,
                    n.UpdatedAt)));

            Monitor.Log($"life.milestones.state received: gameDate={state.GameDate} count={state.Nodes?.Count ?? 0} proactive={string.IsNullOrEmpty(state.RequestId)}", LogLevel.Debug);
        });
    }

    private void HandleLifeCareReceived(LifeCarePayload care)
    {
        _mainThreadActions.Enqueue(() =>
        {
            string saveId = Constants.SaveFolderName ?? Game1.uniqueIDForThisGame.ToString();
            if (!string.Equals(care.SaveId, saveId, StringComparison.Ordinal)) return;

            bool menuOpen = Game1.activeClickableMenu != null;
            bool eventPlaying = Game1.eventUp;
            bool blocked = menuOpen || eventPlaying;

            var hint = _careHintController.ReceiveCareHint(
                care.EventKey, care.GameDate, care.Kind, care.Text, blocked);

            if (hint != null)
            {
                // Add to life-menu unread list
                _lifeMenuUiState.AddUnreadCareHint(hint);
                // Show low-intrusion HUD message
                Game1.addHUDMessage(new HUDMessage($"阿星想和你聊聊 — 打开生活菜单查看"));
            }
            else if (blocked && _careHintController.PendingHints.Any(h => h.EventKey == care.EventKey))
            {
                // Still add to pending-read list even if deferred display
                _lifeMenuUiState.AddUnreadCareHint(
                    new Domain.PendingCareHint(care.EventKey, care.GameDate, care.Kind, care.Text));
            }

            Monitor.Log($"life.care received: eventKey={care.EventKey} kind={care.Kind} blocked={blocked}", LogLevel.Debug);
        });
    }

    // -----------------------------------------------------------------------
    // Helper
    // -----------------------------------------------------------------------

    private static string GetCurrentGameDate()
    {
        if (!Context.IsWorldReady) return string.Empty;
        return $"{Game1.year}:{Game1.currentSeason}:{Game1.dayOfMonth}";
    }
}
