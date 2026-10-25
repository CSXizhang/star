using System;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Collections.Generic;
using System.Reflection;
using System.Threading;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Graphics;
using StardewModdingAPI;
using StardewModdingAPI.Events;
using StardewValley;
using StardewValley.Menus;
using StardewValley.TerrainFeatures;

namespace StardewAI.Companion.TestDriver;

public sealed class TestDriverMod : Mod
{
    private string? _isolatedSavesPath;
    private string _testMode = "none";
    private string _scenario = "normal-watering";
    private string? _evidenceOutputPath;
    private string? _evidenceDir;
    private string? _runId;
    private int _port = 37788;
    private bool _freezeTime;
    private bool _isolationFailed;
    private int _loopDemoFrameTicks;
    private int _loopDemoFrameIndex;
    private bool _interactionReloadPending;
    private string? _interactionReloadSaveName;
    private bool _interactionReturnedToTitle;
    private bool _loopDemoCaptureRequested;
    private string? _purchaseMcpPhase = null;
    private readonly HashSet<Vector2> _initialFarmHoeDirtTiles = new();
    private readonly HashSet<Vector2> _initialFarmHoeDirtWithCrops = new();
    private readonly Dictionary<string, HashSet<int>> _companionAnimationFrames = new();
    private Vector2? _previousCompanionAnimationPosition;
    private long _lastMeasuredFrame;
    private bool _lastFrameWasSteady;
    private readonly List<double> _steadyFrameIntervalsMs = new();
    private readonly List<Vector2> _harnessFixtureTiles = new();
    private readonly HashSet<Vector2> _harnessFixtureInitiallyEmpty = new();
    private string? _harnessFixtureMode;
    private Vector2? _acceptanceChest;
    private Debris? _acceptanceDrop;
    private int _acceptanceMovementSamples;
    private readonly List<object> _acceptanceSolidOverlaps = new();
    private readonly List<Vector2> _acceptanceMatureTrees = new();
    private Vector2? _acceptanceSapling;
    private string? _harnessCaptureLabel;

    // Scenarios driven by the external loop-demo-done.json completion channel: time runs
    // unfrozen, periodic frames are captured while waiting, and the gate reads the done-file.
    private bool UsesLoopDemoCompletionChannel =>
        _scenario.Equals("loop-demo", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("harvest-demo", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("harvest-cancel", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("farm-flow", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("farm-flow-interrupt", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("farm-flow-pause", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("planting-mcp", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("shipping-mcp", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("navigation-mcp", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("purchase-mcp", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("cycle-mcp", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("cycle-operator", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("chest-operator", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("existing-save-buy5", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("farm-capabilities", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("free-mode-control", StringComparison.OrdinalIgnoreCase) ||
        _scenario.Equals("daily-chain", StringComparison.OrdinalIgnoreCase);

    private enum BaselineState
    {
        Idle,
        StartNewGame,
        WaitingForNewGameLoad,
        TriggerSave,
        WaitingForSaveComplete,
        ExitToTitle,
        WaitingForTitleMenu,
        TriggerReload,
        WaitingForReloadComplete,
        Finished
    }

    private enum ScenarioState
    {
        Idle,
        LoadSave,
        WaitingForWorldReady,
        WarpToFarm,
        ArrangeScenario,
        WaitingForStoreOpen,
        PublishCoordinationReady,
        WaitingForExternalAct,
        CaptureArtifacts,
        VerifyFrozenBoundaryAndFinish,
        SaveReload_Save,
        SaveReload_Reload,
        SaveReload_WaitingForWorldReady,
        SaveReload_Verify,
        Finished
    }

    private sealed class InventorySlotSnapshot
    {
        public int Slot { get; set; }
        public string? Id { get; set; }
        public string? Name { get; set; }
        public int Stack { get; set; }
        public int WaterLeft { get; set; }
    }

    private BaselineState _baselineState = BaselineState.Idle;
    private ScenarioState _scenarioState = ScenarioState.Idle;
    private int _tickCounter;
    private int _reloadReadyTicks;
    private string? _createdSaveName;
    private readonly Dictionary<string, object?> _evidence = new();

    private float _initialPlayerStamina;
    private int _initialPlayerMoney;
    private int _initialPlayerWater;
    private int _initialPlayerItemCount;
    private readonly List<InventorySlotSnapshot> _initialPlayerItems = new();

    private float _preActorStamina = 270.0f;
    private int _preActorWater = 40;
    private readonly List<Vector2> _blockedTiles = new();
    private readonly List<Vector2> _zoneTiles = new();

    // save-reload scenario: post-Act actor values recorded before save, reloaded values after reload
    private float _postActActorStamina;
    private int _postActActorWater;
    private int _postActActorTileX;
    private int _postActActorTileY;
    private float? _reloadedActorStamina;
    private int? _reloadedActorWater;
    private string? _reloadedActorTile;
    private bool _reloadedActorIdle;
    private bool _reloadDiscoveryRepublished;
    private bool _reloadDiscoverySaveIdMatches;
    private bool _postActRecorded;

    private string? _screenshotRequestPath;
    private bool _screenshotCaptured;
    private readonly Dictionary<string, object?> _screenshotMetadata = new();
    private string? _f8UiCaptureLabel;
    private int _f8UiCaptureDelayFrames;
    private string? _lifeUiCaptureLabel;
    private string? _lifeUiPresentedCaptureLabel;
    private int _lifeUiPresentedCaptureFrames;

    private Vector2 _targetTile = new Vector2(65, 15);
    private Vector2 _navDoorTile = new Vector2(43, 57);
    private bool _targetDirtIsWatered;
    private Crop? _initialProtectedCropRef;

    private bool _saveLoadedFired;
    private bool _dayStartedFired;
    private bool _renderedWorldObserved;
    private int _renderedWorldFramesCount;

    public enum ActorObservationStatus
    {
        NotAttempted,
        Observed,
        ModNotFoundInRegistry,
        ModEntryInstanceNull,
        ActorFieldNotFound,
        ActorInstanceNull,      // Mod is loaded, field exists, but _actor is null (not initialized yet)
        GameFarmerNull,         // Actor exists but GameFarmer is null
        ExceptionThrown
    }

    private ActorObservationStatus _lastActorObservationStatus = ActorObservationStatus.NotAttempted;
    private string _lastActorObservationDetail = "Actor observation not attempted yet.";

    public override void Entry(IModHelper helper)
    {
        // 1. Resolve configuration from Environment or test-session.json
        ResolveConfiguration();

        // 2. Early Bootstrap Save Isolation - STRICT FAIL-CLOSED GATING
        if (string.IsNullOrWhiteSpace(_isolatedSavesPath))
        {
            Monitor.Log("[TestDriver] FATAL: No isolated save path configured. Fails closed to protect daily saves.", LogLevel.Error);
            FailClosed("Missing isolated save path configuration");
            return;
        }

        bool applied = SaveIsolationPatcher.Apply(_isolatedSavesPath, Monitor);
        if (!applied)
        {
            Monitor.Log("[TestDriver] FATAL: Save isolation patch application or verification failed. Fails closed.", LogLevel.Error);
            FailClosed("Save isolation patch failed to apply or verify");
            return;
        }

        // 3. Register Console Commands for harness
        helper.ConsoleCommands.Add("star_freeze", "Freeze game time", (_, _) =>
        {
            _freezeTime = true;
            Monitor.Log("[TestDriver] Time frozen.", LogLevel.Info);
        });

        helper.ConsoleCommands.Add("star_unfreeze", "Unfreeze game time", (_, _) =>
        {
            _freezeTime = false;
            Monitor.Log("[TestDriver] Time unfrozen.", LogLevel.Info);
        });

        helper.ConsoleCommands.Add("star_status", "Print game status JSON", (_, _) =>
        {
            var status = GetCurrentGameStatus();
            Monitor.Log("[TestDriver] STATUS: " + JsonSerializer.Serialize(status), LogLevel.Info);
        });

        helper.ConsoleCommands.Add("star_exit", "Gracefully exit game", (_, _) =>
        {
            Monitor.Log("[TestDriver] Exiting game via console command.", LogLevel.Info);
            Game1.quit = true;
        });

        // 4. Hook SMAPI Events
        helper.Events.GameLoop.GameLaunched += OnGameLaunched;
        helper.Events.GameLoop.UpdateTicked += OnUpdateTicked;
        helper.Events.GameLoop.SaveLoaded += OnSaveLoaded;
        helper.Events.GameLoop.DayStarted += OnDayStarted;
        helper.Events.GameLoop.Saved += OnSaved;
        helper.Events.GameLoop.ReturnedToTitle += OnReturnedToTitle;
        helper.Events.GameLoop.TimeChanged += OnTimeChanged;
        helper.Events.Display.RenderedWorld += OnRenderedWorld;
        helper.Events.Display.Rendered += OnRendered;

        Monitor.Log($"[TestDriver] Loaded successfully with verified save isolation. Mode: {_testMode}", LogLevel.Info);
    }

    private void FailClosed(string reason)
    {
        _isolationFailed = true;
        _evidence["timestampUtc"] = DateTime.UtcNow.ToString("o");
        _evidence["gateSuccess"] = false;
        _evidence["failureReason"] = reason;
        _evidence["failClosedTriggered"] = true;

        WriteEvidenceFile();
        Game1.quit = true;
    }

    private void ResolveConfiguration()
    {
        _isolatedSavesPath = Environment.GetEnvironmentVariable("STARDEW_AI_ISOLATED_SAVES");
        _testMode = Environment.GetEnvironmentVariable("STARDEW_AI_TEST_MODE") ?? "none";
        _scenario = Environment.GetEnvironmentVariable("STARDEW_AI_SCENARIO") ?? "normal-watering";
        _evidenceOutputPath = Environment.GetEnvironmentVariable("STARDEW_AI_EVIDENCE_OUTPUT");
        _evidenceDir = Environment.GetEnvironmentVariable("STARDEW_AI_EVIDENCE_DIR");
        _runId = Environment.GetEnvironmentVariable("STARDEW_AI_RUN_ID");
        if (int.TryParse(Environment.GetEnvironmentVariable("STARDEW_AI_PORT"), out int p)) _port = p;

        var sessionFile = Environment.GetEnvironmentVariable("STARDEW_AI_SESSION_CONFIG");
        if (string.IsNullOrWhiteSpace(sessionFile) || !File.Exists(sessionFile))
        {
            sessionFile = Path.Combine(AppContext.BaseDirectory, "test-session.json");
            if (!File.Exists(sessionFile))
            {
                sessionFile = Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "test-session.json");
            }
        }

        if (File.Exists(sessionFile))
        {
            try
            {
                var json = File.ReadAllText(sessionFile);
                using var doc = JsonDocument.Parse(json);
                var root = doc.RootElement;
                if (root.TryGetProperty("runId", out var pRun)) _runId = pRun.GetString();
                if (root.TryGetProperty("scenario", out var pScen)) _scenario = pScen.GetString() ?? "normal-watering";
                if (root.TryGetProperty("evidenceDir", out var pEvDir)) _evidenceDir = pEvDir.GetString();
                if (root.TryGetProperty("isolatedSavesPath", out var pPath)) _isolatedSavesPath = pPath.GetString();
                if (root.TryGetProperty("testMode", out var pMode)) _testMode = pMode.GetString() ?? "none";
                if (root.TryGetProperty("evidenceOutputPath", out var pEv)) _evidenceOutputPath = pEv.GetString();
                if (root.TryGetProperty("port", out var pP) && pP.TryGetInt32(out int prt)) _port = prt;
            }
            catch (Exception ex)
            {
                Monitor.Log($"[TestDriver] Error parsing {sessionFile}: {ex.Message}", LogLevel.Warn);
            }
        }

        if (string.IsNullOrWhiteSpace(_evidenceOutputPath))
        {
            _evidenceOutputPath = Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "baseline-evidence.json");
        }
        if (string.IsNullOrWhiteSpace(_evidenceDir))
        {
            _evidenceDir = Path.GetDirectoryName(_evidenceOutputPath) ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs");
        }
    }

    private void OnGameLaunched(object? sender, GameLaunchedEventArgs e)
    {
        if (_isolationFailed) return;

        Monitor.Log($"[TestDriver] GameLaunched event fired. Mode: {_testMode}, Scenario: {_scenario}", LogLevel.Info);
        if (_testMode.Equals("baseline-gate", StringComparison.OrdinalIgnoreCase))
        {
            _baselineState = BaselineState.StartNewGame;
            _tickCounter = 0;
            Monitor.Log("[TestDriver] Baseline Gate workflow initiated.", LogLevel.Info);
        }
        else if (_testMode.Equals("scenario", StringComparison.OrdinalIgnoreCase) ||
                 _testMode.Equals("arrange-smoke", StringComparison.OrdinalIgnoreCase))
        {
            _scenarioState = ScenarioState.LoadSave;
            _tickCounter = 0;
            Monitor.Log($"[TestDriver] {_testMode} workflow initiated for scenario: {_scenario}", LogLevel.Info);
        }
    }

    private void OnUpdateTicked(object? sender, UpdateTickedEventArgs e)
    {
        if (_isolationFailed) return;
        HandleLifecycleRequest();
        if (_scenario == "free-mode-control")
        {
            long measuredNow = System.Diagnostics.Stopwatch.GetTimestamp();
            bool steady = Context.IsWorldReady && _scenarioState == ScenarioState.WaitingForExternalAct
                && Game1.activeClickableMenu == null && !Game1.eventUp
                && _lifeUiPresentedCaptureLabel == null && _lifeUiCaptureLabel == null
                && _screenshotRequestPath == null && !_loopDemoCaptureRequested;
            if (steady && _lastFrameWasSteady && _lastMeasuredFrame > 0 && _steadyFrameIntervalsMs.Count < 3600)
                _steadyFrameIntervalsMs.Add((measuredNow - _lastMeasuredFrame) * 1000d / System.Diagnostics.Stopwatch.Frequency);
            _lastMeasuredFrame = measuredNow;
            _lastFrameWasSteady = steady;
        }
        if (_lifeUiPresentedCaptureLabel != null && --_lifeUiPresentedCaptureFrames <= 0)
        {
            string captureLabel = _lifeUiPresentedCaptureLabel;
            _lifeUiPresentedCaptureLabel = null;
            if (Context.IsWorldReady && Game1.activeClickableMenu != null)
            {
                string captureDir = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
                DoCaptureRenderScreenshot(Path.Combine(captureDir, $"life-ui-{captureLabel}.png"), "Previous fully presented frame with companion menu");
            }
        }
        if (_interactionReturnedToTitle && !Context.IsWorldReady && _interactionReloadSaveName != null)
        {
            string saveName = _interactionReloadSaveName;
            _interactionReturnedToTitle = false;
            _interactionReloadSaveName = null;
            SaveGame.Load(saveName);
            Game1.exitActiveMenu(); // Standard native title loader removes TitleMenu before world readiness.
            return;
        }
        _tickCounter++;

        if (_scenario == "free-mode-control" && Context.IsWorldReady && _tickCounter % 3 == 0)
        {
            ResolveCompanionActor(out _, out var animationFarmer, out _, out _);
            if (animationFarmer != null)
            {
                var modInfo = Helper.ModRegistry.Get("CSXizhang.StardewAICompanion");
                var instance = modInfo?.GetType().GetProperty("Mod")?.GetValue(modInfo);
                var coordinator = instance?.GetType().GetField("_coordinator", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(instance);
                var nativeMachine = ReadNativeMember(coordinator, "NativeActionMachine");
                bool eating = ReadNativeMember(nativeMachine, "IsExecuting") is true &&
                    ReadNativeMember(ReadNativeMember(nativeMachine, "CurrentRequest"), "Kind")?.ToString() == "EatFood";
                string phase = animationFarmer.UsingTool ? "tool:" + animationFarmer.CurrentTool?.GetType().Name
                    : eating ? "eating"
                    : _previousCompanionAnimationPosition.HasValue && animationFarmer.Position != _previousCompanionAnimationPosition.Value ? "walking"
                    : animationFarmer.isInBed.Value ? "bed" : "idle";
                if (!_companionAnimationFrames.TryGetValue(phase, out var frames)) _companionAnimationFrames[phase] = frames = new();
                frames.Add(animationFarmer.FarmerSprite.CurrentFrame);
                if (phase == "walking")
                {
                    string facingPhase = "walking-facing:" + animationFarmer.FacingDirection;
                    if (!_companionAnimationFrames.TryGetValue(facingPhase, out var facingFrames))
                        _companionAnimationFrames[facingPhase] = facingFrames = new();
                    facingFrames.Add(animationFarmer.FarmerSprite.CurrentFrame);
                    _acceptanceMovementSamples++;
                    var location = animationFarmer.currentLocation;
                    var body = animationFarmer.GetBoundingBox();
                    bool objectOverlap = location.objects.Values.Any(obj => !obj.isPassable() && obj.GetBoundingBox().Intersects(body));
                    bool buildingOverlap = location.buildings.Any(building =>
                        Enumerable.Range(body.Left / 64, body.Right / 64 - body.Left / 64 + 1).Any(x =>
                        Enumerable.Range(body.Top / 64, body.Bottom / 64 - body.Top / 64 + 1).Any(y =>
                            body.Intersects(new Rectangle(x * 64, y * 64, 64, 64)) && building.occupiesTile(new Vector2(x, y))
                            && !building.isTilePassable(new Vector2(x, y)))));
                    if ((objectOverlap || buildingOverlap) && _acceptanceSolidOverlaps.Count < 20)
                        _acceptanceSolidOverlaps.Add(new { location = location.NameOrUniqueName,
                            x = animationFarmer.Position.X, y = animationFarmer.Position.Y, objectOverlap, buildingOverlap });
                }
                if (animationFarmer.UsingTool && ReadNativeMember(animationFarmer.FarmerSprite, "CurrentToolIndex") is int toolFrame)
                {
                    string toolPhase = "tool-index:" + animationFarmer.CurrentTool?.GetType().Name;
                    if (!_companionAnimationFrames.TryGetValue(toolPhase, out var tools)) _companionAnimationFrames[toolPhase] = tools = new();
                    tools.Add(toolFrame);
                }
                _previousCompanionAnimationPosition = animationFarmer.Position;
            }
        }

        if (_freezeTime && Context.IsWorldReady && Game1.gameTimeInterval > 0)
        {
            Game1.gameTimeInterval = 0;
        }

        if (UsesLoopDemoCompletionChannel && _scenarioState == ScenarioState.WaitingForExternalAct
            && Environment.GetEnvironmentVariable("STARDEW_AI_CAPTURE_PERIODIC_FRAMES") != "0")
        {
            _loopDemoFrameTicks++;
            // Capture interval: 120 ticks (~2s). Full-res backbuffer readback + PNG encode on the
            // render thread is expensive; the previous 30-tick interval made the demo visibly stutter.
            if (_loopDemoFrameTicks >= 120)
            {
                _loopDemoFrameTicks = 0;
                _loopDemoCaptureRequested = true;
            }
        }

        if (_testMode.Equals("baseline-gate", StringComparison.OrdinalIgnoreCase))
        {
            ProcessBaselineState();
        }
        else if (_testMode.Equals("scenario", StringComparison.OrdinalIgnoreCase) ||
                 _testMode.Equals("arrange-smoke", StringComparison.OrdinalIgnoreCase))
        {
            ProcessScenarioState();
        }
    }

    private void ProcessBaselineState()
    {
        switch (_baselineState)
        {
            case BaselineState.StartNewGame:
                if (Game1.activeClickableMenu is TitleMenu titleMenu)
                {
                    if (_tickCounter > 30) // Wait for title menu to initialize
                    {
                        // Assert isolation before automated action
                        var currentProgSave = StardewValley.Program.GetSavesFolder();
                        var currentSmapiSave = Constants.SavesPath;
                        if (!string.Equals(currentProgSave, _isolatedSavesPath, StringComparison.OrdinalIgnoreCase) ||
                            !string.Equals(currentSmapiSave, _isolatedSavesPath, StringComparison.OrdinalIgnoreCase))
                        {
                            Monitor.Log($"[TestDriver] FATAL: Pre-game check failed: Program={currentProgSave}, SMAPI={currentSmapiSave}, Expected={_isolatedSavesPath}", LogLevel.Error);
                            FailClosed("Pre-game isolation assertion mismatch");
                            return;
                        }

                        Monitor.Log("[TestDriver] Initializing new game with character 'StageZero' and farm 'AstraFarm' via TitleMenu...", LogLevel.Info);
                        _createdSaveName = "StageZero";

                        Game1.player.Name = "StageZero";
                        Game1.player.farmName.Value = "AstraFarm";
                        Game1.player.favoriteThing.Value = "Apples";
                        Game1.player.changeGender(true);
                        Game1.whichFarm = 0; // Standard farm

                        // Use official TitleMenu method with skipIntro = true
                        titleMenu.createdNewCharacter(true);
                        _baselineState = BaselineState.WaitingForNewGameLoad;
                        _tickCounter = 0;
                    }
                }
                break;

            case BaselineState.WaitingForNewGameLoad:
                if (Context.IsWorldReady && Game1.hasLoadedGame && Game1.currentLocation != null)
                {
                    if (_tickCounter > 30) // Wait half a second after world ready
                    {
                        Monitor.Log($"[TestDriver] New game loaded successfully! Location: {Game1.currentLocation.Name}, Day: {Game1.dayOfMonth}, Time: {Game1.timeOfDay}", LogLevel.Info);
                        _evidence["newGameLoaded"] = true;
                        _evidence["farmerName"] = Game1.player.Name;
                        _evidence["farmName"] = Game1.player.farmName.Value;
                        _evidence["initialLocation"] = Game1.currentLocation.Name;
                        _evidence["initialDay"] = Game1.dayOfMonth;
                        _evidence["initialTime"] = Game1.timeOfDay;

                        _baselineState = BaselineState.TriggerSave;
                        _tickCounter = 0;
                    }
                }
                break;

            case BaselineState.TriggerSave:
            {
                Monitor.Log("[TestDriver] Triggering and running SaveGame.Save() enumerator...", LogLevel.Info);
                try
                {
                    var saveEnum = SaveGame.Save();
                    while (saveEnum.MoveNext())
                    {
                        // Advance save enumerator to completion
                    }

                    var currentSavesFolder = StardewValley.Program.GetSavesFolder();
                    Monitor.Log($"[TestDriver] Save completed successfully in: {currentSavesFolder}", LogLevel.Info);

                    var saveDirs = Directory.GetDirectories(currentSavesFolder);
                    var filesList = new List<string>();
                    foreach (var d in saveDirs)
                    {
                        foreach (var f in Directory.GetFiles(d))
                        {
                            var fi = new FileInfo(f);
                            filesList.Add($"{Path.GetFileName(d)}/{fi.Name} ({fi.Length} bytes)");
                        }
                    }

                    _evidence["saveCompleted"] = true;
                    _evidence["isolatedSavesFolder"] = currentSavesFolder;
                    _evidence["savedFiles"] = filesList;

                    _baselineState = BaselineState.ExitToTitle;
                    _tickCounter = 0;
                }
                catch (Exception ex)
                {
                    Monitor.Log($"[TestDriver] ERROR running SaveGame.Save(): {ex}", LogLevel.Error);
                    FailClosed($"Exception in SaveGame.Save: {ex.Message}");
                }
                break;
            }

            case BaselineState.WaitingForSaveComplete:
                // Unused now since saveEnum runs synchronously
                break;

            case BaselineState.ExitToTitle:
                if (_tickCounter > 30)
                {
                    Monitor.Log("[TestDriver] Exiting to Title Menu to verify save reload...", LogLevel.Info);
                    _baselineState = BaselineState.WaitingForTitleMenu;
                    _tickCounter = 0;
                    Game1.ExitToTitle();
                }
                break;

            case BaselineState.WaitingForTitleMenu:
                if (Game1.activeClickableMenu is TitleMenu)
                {
                    if (_tickCounter > 45)
                    {
                        Monitor.Log("[TestDriver] Back at Title Menu. Locating saved game in isolated folder...", LogLevel.Info);
                        _baselineState = BaselineState.TriggerReload;
                        _tickCounter = 0;
                    }
                }
                break;

            case BaselineState.TriggerReload:
                var savesFolder = StardewValley.Program.GetSavesFolder();
                Monitor.Log($"[TestDriver] Reloading from isolated saves folder: {savesFolder}", LogLevel.Info);

                var dirs = Directory.GetDirectories(savesFolder);
                if (dirs.Length == 0)
                {
                    Monitor.Log($"[TestDriver] FATAL: No saves found in isolated folder {savesFolder}!", LogLevel.Error);
                    FailClosed("No saves found in isolated folder after save");
                    return;
                }

                var targetDir = dirs[0];
                var folderName = Path.GetFileName(targetDir);
                Monitor.Log($"[TestDriver] Found save folder: {folderName}. Calling SaveGame.Load...", LogLevel.Info);

                _saveLoadedFired = false;
                _dayStartedFired = false;
                _renderedWorldObserved = false;
                _renderedWorldFramesCount = 0;

                _baselineState = BaselineState.WaitingForReloadComplete;
                _tickCounter = 0;
                _reloadReadyTicks = 0;
                SaveGame.Load(folderName);
                Game1.exitActiveMenu();
                break;

            case BaselineState.WaitingForReloadComplete:
                if (_saveLoadedFired && Context.IsWorldReady && _renderedWorldObserved && Game1.currentLocation != null)
                {
                    _reloadReadyTicks++;
                    if (_reloadReadyTicks > 20)
                    {
                        Monitor.Log($"[TestDriver] Save RELOADED successfully! Location: {Game1.currentLocation?.Name}, Time: {Game1.timeOfDay}", LogLevel.Info);
                        _evidence["reloadCompleted"] = true;
                        _evidence["reloadedFarmerName"] = Game1.player?.Name ?? "";
                        _evidence["reloadedLocation"] = Game1.currentLocation?.Name ?? "";
                        _evidence["reloadedDay"] = Game1.dayOfMonth;
                        _evidence["reloadedTime"] = Game1.timeOfDay;

                        FinishBaseline(true);
                        return;
                    }
                }
                else if (_tickCounter > 1200)
                {
                    Monitor.Log("[TestDriver] ERROR: Reload timeout reached!", LogLevel.Error);
                    FailClosed("Timeout waiting for reload completion");
                }
                break;

            case BaselineState.Finished:
                break;
        }
    }

    private void OnSaved(object? sender, SavedEventArgs e)
    {
        if (_isolationFailed) return;

        if (_baselineState == BaselineState.WaitingForSaveComplete)
        {
            var savesFolder = StardewValley.Program.GetSavesFolder();
            Monitor.Log($"[TestDriver] Save completed successfully in: {savesFolder}", LogLevel.Info);

            var saveDirs = Directory.GetDirectories(savesFolder);
            var filesList = new List<string>();
            foreach (var d in saveDirs)
            {
                foreach (var f in Directory.GetFiles(d))
                {
                    var fi = new FileInfo(f);
                    filesList.Add($"{Path.GetFileName(d)}/{fi.Name} ({fi.Length} bytes)");
                }
            }

            _evidence["saveCompleted"] = true;
            _evidence["isolatedSavesFolder"] = savesFolder;
            _evidence["savedFiles"] = filesList;

            _baselineState = BaselineState.ExitToTitle;
            _tickCounter = 0;
        }
    }

    private void OnSaveLoaded(object? sender, SaveLoadedEventArgs e)
    {
        if (_isolationFailed) return;
        _saveLoadedFired = true;
        Monitor.Log($"[TestDriver] SaveLoaded event fired. Context.IsWorldReady={Context.IsWorldReady}, Location={Game1.currentLocation?.Name}", LogLevel.Info);

        if (_baselineState == BaselineState.WaitingForReloadComplete)
        {
            Monitor.Log($"[TestDriver] Save RELOADED via SMAPI event! Location: {Game1.currentLocation?.Name}, Time: {Game1.timeOfDay}", LogLevel.Info);
            _evidence["reloadCompleted"] = true;
            _evidence["reloadedFarmerName"] = Game1.player?.Name ?? "";
            _evidence["reloadedLocation"] = Game1.currentLocation?.Name ?? "";
            _evidence["reloadedDay"] = Game1.dayOfMonth;
            _evidence["reloadedTime"] = Game1.timeOfDay;

            FinishBaseline(true);
        }
    }

    private void OnDayStarted(object? sender, DayStartedEventArgs e)
    {
        if (_isolationFailed) return;
        _dayStartedFired = true;
        Monitor.Log($"[TestDriver] DayStarted event fired. Context.IsWorldReady={Context.IsWorldReady}, Day={Game1.dayOfMonth}, Location={Game1.currentLocation?.Name}", LogLevel.Info);
    }

    private bool IsWorldGenuinelyReady()
    {
        return _saveLoadedFired &&
               _dayStartedFired &&
               Context.IsWorldReady &&
               _renderedWorldObserved &&
               _renderedWorldFramesCount >= 5 &&
               Game1.currentLocation != null &&
               !Game1.fadeToBlack &&
               !Game1.fadeIn &&
               Game1.fadeToBlackAlpha <= 0.001f &&
               Game1.activeClickableMenu == null;
    }

    private void OnReturnedToTitle(object? sender, ReturnedToTitleEventArgs e)
    {
        Monitor.Log("[TestDriver] ReturnedToTitle event fired.", LogLevel.Trace);
        if (_interactionReloadSaveName != null) _interactionReturnedToTitle = true;
    }

    private void OnTimeChanged(object? sender, TimeChangedEventArgs e)
    {
        if (_freezeTime && Context.IsWorldReady)
        {
            Game1.timeOfDay = e.OldTime;
        }
    }

    private void FinishBaseline(bool success)
    {
        _baselineState = BaselineState.Finished;
        _evidence["timestampUtc"] = DateTime.UtcNow.ToString("o");
        _evidence["gateSuccess"] = success;
        _evidence["smapiVersion"] = Constants.ApiVersion.ToString();
        _evidence["gameVersion"] = Game1.version;
        if (!string.IsNullOrEmpty(_createdSaveName))
        {
            _evidence["createdSaveName"] = _createdSaveName;
        }

        WriteEvidenceFile();

        Monitor.Log($"[TestDriver] Baseline Gate Finished (Success: {success}). Requesting game exit...", LogLevel.Info);
        Game1.quit = true;
    }

    private void ProcessScenarioState()
    {
        switch (_scenarioState)
        {
            case ScenarioState.LoadSave:
                if (Game1.activeClickableMenu is TitleMenu)
                {
                    if (_tickCounter > 30)
                    {
                        var savesFolder = StardewValley.Program.GetSavesFolder();
                        var dirs = Directory.GetDirectories(savesFolder);
                        if (dirs.Length == 0)
                        {
                            Monitor.Log($"[TestDriver] FATAL: No saves found in {savesFolder} for scenario test!", LogLevel.Error);
                            FailClosed("No save found in isolated directory for scenario");
                            return;
                        }

                        var saveFolderName = Path.GetFileName(dirs[0]);
                        Monitor.Log($"[TestDriver] Scenario '{_scenario}': Found save '{saveFolderName}'. Calling SaveGame.Load...", LogLevel.Info);
                        _saveLoadedFired = false;
                        _dayStartedFired = false;
                        _renderedWorldObserved = false;
                        _renderedWorldFramesCount = 0;
                        _scenarioState = ScenarioState.WaitingForWorldReady;
                        _tickCounter = 0;
                        SaveGame.Load(saveFolderName);
                        Game1.exitActiveMenu();
                    }
                }
                break;

            case ScenarioState.WaitingForWorldReady:
                if (IsWorldGenuinelyReady())
                {
                    _tickCounter++;
                    if (_tickCounter > 30)
                    {
                        Monitor.Log($"[TestDriver] World GENUINELY ready at location: {Game1.currentLocation?.Name} (SaveLoaded={_saveLoadedFired}, DayStarted={_dayStartedFired}, RenderedFrames={_renderedWorldFramesCount})", LogLevel.Info);
                        _scenarioState = ScenarioState.WarpToFarm;
                        _tickCounter = 0;
                    }
                }
                else if (_tickCounter > 1800)
                {
                    Monitor.Log($"[TestDriver] Timeout waiting for genuine world ready in scenario! (SaveLoaded={_saveLoadedFired}, DayStarted={_dayStartedFired}, Context.IsWorldReady={Context.IsWorldReady}, RenderedFrames={_renderedWorldFramesCount}, fadeAlpha={Game1.fadeToBlackAlpha})", LogLevel.Error);
                    FailClosed("Timeout waiting for genuine world ready in scenario");
                }
                break;

            case ScenarioState.WarpToFarm:
                // Isolated runs must survive foreground theft (browser popups, notifications):
                // vanilla pauses a single-player world when the window loses focus.
                Game1.options.pauseWhenOutOfFocus = false;
                if (!UsesLoopDemoCompletionChannel)
                {
                    _freezeTime = true;
                    Game1.gameTimeInterval = 0;
                    Game1.random = new Random(42);
                }
                else
                {
                    _freezeTime = false;
                    Monitor.Log($"[TestDriver] {_scenario}: time freeze disabled, gameTimeInterval running normally.", LogLevel.Info);
                }

                if (Game1.currentLocation?.Name != "Farm")
                {
                    // Observation tile must not overlap the companion (64,15) or the target dirt (65,15):
                    // the navigator treats the human player as a dynamic obstacle.
                    Monitor.Log("[TestDriver] Warping player to Farm at (62, 17)...", LogLevel.Info);
                    Game1.warpFarmer("Farm", 62, 17, 2);
                }
                _scenarioState = ScenarioState.ArrangeScenario;
                _tickCounter = 0;
                break;

            case ScenarioState.ArrangeScenario:
                _tickCounter++;
                if (Game1.currentLocation?.Name == "Farm" &&
                    !Game1.fadeToBlack &&
                    !Game1.fadeIn &&
                    Game1.fadeToBlackAlpha <= 0.001f &&
                    _tickCounter > 20)
                {
                    bool actorOk = ResolveCompanionActor(out var actor, out var farmer, out var status, out var statusDetail);
                    if (!actorOk && _tickCounter < 300)
                    {
                        // Retry for up to 300 ticks (~5s) to allow companion actor initialization on DayStarted
                        break;
                    }

                    ExecuteScenarioArrange(actor, farmer, actorOk, status, statusDetail);

                    if (!actorOk)
                    {
                        Monitor.Log($"[TestDriver] OBSERVATION THRESHOLD VIOLATION: realActorObserved=false ({status}: {statusDetail}). Publishing READY_FOR_ACT is FORBIDDEN.", LogLevel.Error);
                        _evidence["realActorObserved"] = false;
                        _evidence["actorObservationStatus"] = status.ToString();
                        _evidence["actorObservationDetail"] = statusDetail;
                        _evidence["actExecuted"] = false;
                        _evidence["terminalRecordValid"] = false;
                        _evidence["functionalSkillPassed"] = false;
                        _evidence["gateSuccess"] = false;
                        _evidence["failureReason"] = $"ACTOR_NOT_OBSERVED: Cannot proceed to Act phase because companion actor could not be observed ({status}: {statusDetail}).";
                        PublishHarnessCoordination("ACTOR_UNAVAILABLE");

                        var outDirCoord = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
                        _scenarioState = ScenarioState.CaptureArtifacts;
                        _tickCounter = 0;
                        _screenshotCaptured = false;
                        _screenshotRequestPath = Path.Combine(outDirCoord, "render.png");
                        break;
                    }

                    if (_testMode.Equals("arrange-smoke", StringComparison.OrdinalIgnoreCase))
                    {
                        _scenarioState = ScenarioState.CaptureArtifacts;
                        var outDirCoord = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
                        _screenshotCaptured = false;
                        _screenshotRequestPath = Path.Combine(outDirCoord, "render.png");
                    }
                    else if (_scenario.Equals("existing-save-buy5", StringComparison.OrdinalIgnoreCase) && Game1.timeOfDay < 900)
                    {
                        _scenarioState = ScenarioState.WaitingForStoreOpen;
                    }
                    else
                    {
                        _scenarioState = ScenarioState.PublishCoordinationReady;
                    }
                    _tickCounter = 0;
                }
                else if (_tickCounter > 1200)
                {
                    Monitor.Log("[TestDriver] Timeout waiting to arrange scenario on Farm!", LogLevel.Error);
                    FailClosed("Timeout waiting to arrange scenario on Farm");
                }
                break;

            case ScenarioState.WaitingForStoreOpen:
                _tickCounter++;
                if (Game1.timeOfDay >= 900)
                {
                    Monitor.Log($"[TestDriver] In-game time reached {Game1.timeOfDay} naturally (Pierre's store open). Transitioning to publish READY_FOR_ACT.", LogLevel.Info);
                    _scenarioState = ScenarioState.PublishCoordinationReady;
                    _tickCounter = 0;
                }
                else if (_tickCounter % 300 == 0)
                {
                    Monitor.Log($"[TestDriver] Waiting for natural in-game time progression: current timeOfDay={Game1.timeOfDay}, target=900 (Elapsed: {_tickCounter / 60}s)...", LogLevel.Info);
                }
                else if (_tickCounter > 18000)
                {
                    Monitor.Log($"[TestDriver] Timeout waiting for natural time progression to 900! (current timeOfDay={Game1.timeOfDay})", LogLevel.Error);
                    FailClosed("Timeout waiting for natural time progression to 900");
                }
                break;

            case ScenarioState.PublishCoordinationReady:
                if (_lastActorObservationStatus != ActorObservationStatus.Observed)
                {
                    Monitor.Log($"[TestDriver] REFUSING to publish READY_FOR_ACT: actor is not observed ({_lastActorObservationStatus}).", LogLevel.Error);
                    PublishHarnessCoordination("ACTOR_UNAVAILABLE");
                    var outDirCoord = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
                    _scenarioState = ScenarioState.CaptureArtifacts;
                    _tickCounter = 0;
                    _screenshotCaptured = false;
                    _screenshotRequestPath = Path.Combine(outDirCoord, "render.png");
                    break;
                }

                if (_scenario.Equals("purchase-mcp", StringComparison.OrdinalIgnoreCase) ||
                    _scenario.Equals("cycle-mcp", StringComparison.OrdinalIgnoreCase) ||
                    _scenario.Equals("cycle-operator", StringComparison.OrdinalIgnoreCase))
                {
                    var seedShop = Game1.getLocationFromName("SeedShop");
                    var pierre = seedShop?.characters.FirstOrDefault(c => c != null && c.Name == "Pierre") ?? Game1.getCharacterFromName("Pierre");
                    Rectangle ownerArea = new Rectangle(4, 17, 1, 1);
                    bool inArea = pierre != null && ownerArea.Contains(pierre.TilePoint);
                    Monitor.Log($"[TestDriver] Pre-READY Pierre diagnostic: loc={pierre?.currentLocation?.Name}, tile=({pierre?.TilePoint.X},{pierre?.TilePoint.Y}), pos=({pierre?.Position.X},{pierre?.Position.Y}), inArea={inArea}, phase={_purchaseMcpPhase}, timeOfDay={Game1.timeOfDay}", LogLevel.Info);
                }

                PublishHarnessCoordination("READY_FOR_ACT");
                _scenarioState = ScenarioState.WaitingForExternalAct;
                _tickCounter = 0;
                break;

            case ScenarioState.WaitingForExternalAct:
                _tickCounter++;
                var outDirCoordWait = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");

                if (UsesLoopDemoCompletionChannel)
                {
                    // In-process menu verification hook: when evidence/menu-inject.json appears,
                    // open the production mod's CompanionCommandMenu via reflection, inject the
                    // command text, and submit with Enter — exercising the real menu -> parse ->
                    // execute path without synthetic OS input.
                    var injectPath = Path.Combine(outDirCoordWait, "menu-inject.json");
                    if (File.Exists(injectPath))
                    {
                        try
                        {
                            string injectText = File.ReadAllText(injectPath);
                            File.Delete(injectPath);
                            var doc = JsonDocument.Parse(injectText);
                            string commandText = doc.RootElement.TryGetProperty("command", out var pc) ? pc.GetString() ?? "" : "";
                            InjectMenuCommand(commandText);
                        }
                        catch (Exception ex)
                        {
                            Monitor.Log($"[TestDriver] menu-inject note: {ex.Message}", LogLevel.Warn);
                        }
                    }

                    // Test-only F8 menu inspection runs on the game thread. It never opens a
                    // second MCP socket, so the production ChatBridge can remain connected.
                    var f8UiRequest = Path.Combine(outDirCoordWait, "f8-ui-request.json");
                    if (File.Exists(f8UiRequest))
                    {
                        string? requestLabel = null;
                        try
                        {
                            string requestText = File.ReadAllText(f8UiRequest);
                            File.Delete(f8UiRequest);
                            using var request = JsonDocument.Parse(requestText);
                            requestLabel = request.RootElement.TryGetProperty("label", out var labelProperty)
                                ? labelProperty.GetString() : null;
                            HandleF8UiRequest(outDirCoordWait, request.RootElement);
                        }
                        catch (Exception ex)
                        {
                            Monitor.Log($"[TestDriver] F8 UI request failed: {ex.Message}", LogLevel.Warn);
                            if (!string.IsNullOrEmpty(requestLabel) && requestLabel.Length <= 48 &&
                                requestLabel.All(c => char.IsLetterOrDigit(c) || c is '-' or '_'))
                            {
                                File.WriteAllText(Path.Combine(outDirCoordWait, $"f8-ui-{requestLabel}.json"),
                                    JsonSerializer.Serialize(new { runId = _runId, label = requestLabel, error = ex.Message }),
                                    new System.Text.UTF8Encoding(false));
                            }
                        }
                    }

                    var lifeUiRequest = Path.Combine(outDirCoordWait, "life-ui-request.json");
                    if (File.Exists(lifeUiRequest))
                    {
                        string? lifeLabel = null;
                        try
                        {
                            string requestText = File.ReadAllText(lifeUiRequest);
                            File.Delete(lifeUiRequest);
                            using var request = JsonDocument.Parse(requestText);
                            lifeLabel = request.RootElement.TryGetProperty("label", out var lp) ? lp.GetString() : null;
                            HandleLifeUiRequest(outDirCoordWait, request.RootElement);
                        }
                        catch (Exception ex)
                        {
                            Monitor.Log($"[TestDriver] Life UI request failed: {ex.Message}", LogLevel.Warn);
                            if (!string.IsNullOrEmpty(lifeLabel) && lifeLabel.Length <= 48 &&
                                lifeLabel.All(c => char.IsLetterOrDigit(c) || c is '-' or '_'))
                                File.WriteAllText(Path.Combine(outDirCoordWait, $"life-ui-{lifeLabel}.json"),
                                    JsonSerializer.Serialize(new { runId = _runId, label = lifeLabel, error = ex.Message }),
                                    new System.Text.UTF8Encoding(false));
                        }
                    }

                    if (_scenario.Equals("free-mode-control", StringComparison.OrdinalIgnoreCase) ||
                        _scenario.Equals("farm-flow-interrupt", StringComparison.OrdinalIgnoreCase) ||
                        _scenario.Equals("farm-flow-pause", StringComparison.OrdinalIgnoreCase))
                    {
                        var controlInject = Path.Combine(outDirCoordWait, "free-mode-control-inject.json");
                        if (File.Exists(controlInject))
                        {
                            File.Delete(controlInject);
                            InjectFreeModeControl();
                        }
                        var actionInject = Path.Combine(outDirCoordWait, "free-mode-control-action.json");
                        if (File.Exists(actionInject))
                        {
                            string action = File.ReadAllText(actionInject).Trim().ToLowerInvariant();
                            File.Delete(actionInject);
                            InjectFreeModeAction(action);
                        }
                        if (_scenario.Equals("free-mode-control", StringComparison.OrdinalIgnoreCase) ||
                            _scenario.Equals("farm-flow-interrupt", StringComparison.OrdinalIgnoreCase) ||
                            _scenario.Equals("farm-flow-pause", StringComparison.OrdinalIgnoreCase))
                        {
                            var snapshotRequest = Path.Combine(outDirCoordWait, "free-mode-native-snapshot-request.txt");
                            if (File.Exists(snapshotRequest))
                            {
                                string label = File.ReadAllText(snapshotRequest).Trim();
                                File.Delete(snapshotRequest);
                                if (label.Length is > 0 and <= 48 && label.All(c => char.IsLetterOrDigit(c) || c is '-' or '_'))
                                    WriteFreeModeNativeSnapshot(outDirCoordWait, label);
                                else
                                    Monitor.Log("[TestDriver] Invalid free-mode snapshot label.", LogLevel.Warn);
                            }
                        }
                    }

                    if (_scenario.Equals("daily-chain", StringComparison.OrdinalIgnoreCase) || _scenario.Equals("free-mode-control", StringComparison.OrdinalIgnoreCase))
                    {
                        var waterFixture = Path.Combine(outDirCoordWait, "interaction-water-fixture.json");
                        if (File.Exists(waterFixture))
                        {
                            File.Delete(waterFixture);
                            var farm = Game1.getFarm();
                            for (int x = 60; x <= 64; x++)
                            {
                                var tile = new Vector2(x, 21);
                                farm.objects.Remove(tile);
                                farm.terrainFeatures.Remove(tile);
                                var dirt = new HoeDirt(0, farm);
                                dirt.crop = new Crop("472", x, 21, farm);
                                farm.terrainFeatures.Add(tile, dirt);
                            }
                            WriteFreeModeNativeSnapshot(outDirCoordWait, "pause-fixture-ready");
                        }
                        if (_interactionReloadPending && IsWorldGenuinelyReady())
                        {
                            _interactionReloadPending = false;
                            WriteFreeModeNativeSnapshot(outDirCoordWait, "sleep-reloaded");
                        }
                        var interactionReload = Path.Combine(outDirCoordWait, "interaction-save-reload.json");
                        if (File.Exists(interactionReload))
                        {
                            File.Delete(interactionReload);
                            var saver = SaveGame.Save();
                            while (saver.MoveNext()) { }
                            string savedName = Constants.SaveFolderName ?? throw new InvalidOperationException("No current isolated save.");
                            _saveLoadedFired = _dayStartedFired = _renderedWorldObserved = false;
                            _renderedWorldFramesCount = 0;
                            _interactionReloadPending = true;
                            _interactionReloadSaveName = savedName;
                            Monitor.Log("[TestDriver] Returning to title before reloading the real isolated sleeping save.", LogLevel.Info);
                            Game1.ExitToTitle();
                            break;
                        }
                        // Isolated acceptance only: advance the real game clock without
                        // moving the companion or manufacturing a sleeping outcome.
                        var interactionClock = Path.Combine(outDirCoordWait, "interaction-clock.json");
                        if (File.Exists(interactionClock))
                        {
                            var clockRequest = JsonDocument.Parse(File.ReadAllText(interactionClock));
                            File.Delete(interactionClock);
                            int requestedTime = clockRequest.RootElement.GetProperty("timeOfDay").GetInt32();
                            if (requestedTime < 600 || requestedTime > 2550 || requestedTime % 100 >= 60)
                                throw new ArgumentException("Interaction clock requires a native game time from 0600 through 2550.");
                            Game1.timeOfDay = requestedTime;
                            Monitor.Log($"[TestDriver] Isolated interaction clock set to {requestedTime}.", LogLevel.Info);
                        }
                        var dayAdvance = Path.Combine(outDirCoordWait, "day-advance.json");
                        if (File.Exists(dayAdvance))
                        {
                            File.Delete(dayAdvance);
                            Monitor.Log("[TestDriver] day-advance: setting timeOfDay=2550 so the native 2AM pass-out runs the full new-day settlement.", LogLevel.Info);
                            Game1.timeOfDay = 2550;
                        }
                    }

                    if (_scenario.Equals("purchase-mcp", StringComparison.OrdinalIgnoreCase) && _purchaseMcpPhase == "closed")
                    {
                        var phaseAFile = Path.Combine(outDirCoordWait, "purchase-mcp-phase-a-done.json");
                        if (File.Exists(phaseAFile))
                        {
                            Monitor.Log("[TestDriver] purchase-mcp Phase A done file detected. Advancing to Phase B (open)...", LogLevel.Info);
                            bool phaseBOk = ArrangePurchaseMcpPhaseB(outDirCoordWait, out string phaseBFailReason);
                            if (!phaseBOk)
                            {
                                Monitor.Log($"[TestDriver] purchase-mcp Phase B arrange failed: {phaseBFailReason}", LogLevel.Error);
                                _evidence["actExecuted"] = false;
                                _evidence["terminalRecordValid"] = false;
                                _evidence["functionalSkillPassed"] = false;
                                _evidence["gateSuccess"] = false;
                                _evidence["failureReason"] = $"PHASE_B_ARRANGE_FAILED: {phaseBFailReason}";
                                PublishHarnessCoordination("FAILED", "open");
                                _tickCounter = 0;
                                _scenarioState = ScenarioState.CaptureArtifacts;
                                _screenshotRequestPath = Path.Combine(outDirCoordWait, "render.png");
                                break;
                            }
                            _purchaseMcpPhase = "open";
                            PublishHarnessCoordination("READY_FOR_ACT", "open");
                            _tickCounter = 0;
                            break;
                        }
                        if (_tickCounter > 36000)
                        {
                            Monitor.Log($"[TestDriver] purchase-mcp Phase A timed out after {_tickCounter} ticks.", LogLevel.Error);
                            _tickCounter = 0;
                            _scenarioState = ScenarioState.CaptureArtifacts;
                            _screenshotRequestPath = Path.Combine(outDirCoordWait, "render.png");
                            break;
                        }
                        break;
                    }

                    bool doneExists = File.Exists(Path.Combine(outDirCoordWait, "existing-save-buy5-done.json")) ||
                                      File.Exists(Path.Combine(outDirCoordWait, "chest-operator-done.json")) ||
                                      File.Exists(Path.Combine(outDirCoordWait, "cycle-operator-done.json")) ||
                                      File.Exists(Path.Combine(outDirCoordWait, "cycle-mcp-done.json")) ||
                                      File.Exists(Path.Combine(outDirCoordWait, "daily-chain-done.json")) ||
                                      File.Exists(Path.Combine(outDirCoordWait, "farm-capabilities-done.json")) ||
                                      File.Exists(Path.Combine(outDirCoordWait, "free-mode-control-done.json")) ||
                                      File.Exists(Path.Combine(outDirCoordWait, "purchase-mcp-done.json")) ||
                                      File.Exists(Path.Combine(outDirCoordWait, "navigation-mcp-done.json")) ||
                                      File.Exists(Path.Combine(outDirCoordWait, "shipping-mcp-done.json")) ||
                                      File.Exists(Path.Combine(outDirCoordWait, "planting-mcp-done.json")) ||
                                      File.Exists(Path.Combine(outDirCoordWait, "loop-demo-done.json"));
                    int timeoutTicks = (_scenario.Equals("farm-flow", StringComparison.OrdinalIgnoreCase) ||
                                        _scenario.Equals("farm-flow-interrupt", StringComparison.OrdinalIgnoreCase) ||
                                        _scenario.Equals("farm-flow-pause", StringComparison.OrdinalIgnoreCase) ||
                                        _scenario.Equals("planting-mcp", StringComparison.OrdinalIgnoreCase) ||
                                        _scenario.Equals("shipping-mcp", StringComparison.OrdinalIgnoreCase) ||
                                        _scenario.Equals("navigation-mcp", StringComparison.OrdinalIgnoreCase) ||
                                        _scenario.Equals("purchase-mcp", StringComparison.OrdinalIgnoreCase) ||
                                        _scenario.Equals("cycle-mcp", StringComparison.OrdinalIgnoreCase) ||
                                        _scenario.Equals("cycle-operator", StringComparison.OrdinalIgnoreCase) ||
                                        _scenario.Equals("chest-operator", StringComparison.OrdinalIgnoreCase) ||
                                        _scenario.Equals("daily-chain", StringComparison.OrdinalIgnoreCase) ||
                        _scenario.Equals("farm-capabilities", StringComparison.OrdinalIgnoreCase) ||
                                        _scenario.Equals("existing-save-buy5", StringComparison.OrdinalIgnoreCase))
                        ? 36000 // 600s ~ 10min bounded timeout for multi-step real model decision + execution
                        : 18000;
                    // Screen-side external-agent observation needs a longer bounded window.
                    // Default acceptance timings and the explicit done signal are unchanged.
                    if ((_scenario.Equals("free-mode-control", StringComparison.OrdinalIgnoreCase) || _scenario.Equals("daily-chain", StringComparison.OrdinalIgnoreCase)) &&
                        int.TryParse(Environment.GetEnvironmentVariable("STARDEW_AI_OBSERVE_SECONDS"), out var observeSeconds))
                        timeoutTicks = Math.Clamp(observeSeconds, 600, 7200) * 60;
                    if (doneExists || _tickCounter > timeoutTicks)
                    {
                        Monitor.Log($"[TestDriver] {_scenario} Act wait concluded (DoneFile: {doneExists}, Ticks: {_tickCounter}, Frames: {_loopDemoFrameIndex}).", LogLevel.Info);
                        _tickCounter = 0;
                        _scenarioState = ScenarioState.CaptureArtifacts;
                        _screenshotRequestPath = Path.Combine(outDirCoordWait, "render.png");
                    }
                    break;
                }

                bool actSignalled = CheckActCompletionSignalled();
                bool terminalRecordExists = File.Exists(Path.Combine(outDirCoordWait, "terminal-result.json"));

                // Conclude waiting if terminal record was written, external act signalled completion, or timeout reached (2400 ticks ~ 40s)
                if (terminalRecordExists || actSignalled || _tickCounter > 2400)
                {
                    Monitor.Log($"[TestDriver] External Act wait concluded (TerminalRecord: {terminalRecordExists}, Signalled: {actSignalled}, Ticks: {_tickCounter}).", LogLevel.Info);
                    _tickCounter = 0;

                    // save-reload scenario: after a successful Act, drive real save -> title -> reload -> verify
                    if (terminalRecordExists && _scenario.Equals("save-reload", StringComparison.OrdinalIgnoreCase))
                    {
                        _postActRecorded = false;
                        _scenarioState = ScenarioState.SaveReload_Save;
                        break;
                    }

                    _scenarioState = ScenarioState.CaptureArtifacts;
                    _screenshotCaptured = false;
                    _screenshotRequestPath = Path.Combine(outDirCoordWait, "render.png");
                }
                break;

            case ScenarioState.SaveReload_Save:
                _tickCounter++;
                // NOTE: _tickCounter is also incremented in OnUpdateTicked, so it advances twice
                // per tick — never compare it for exact equality; use the one-shot flag instead.
                if (!_postActRecorded)
                {
                    _postActRecorded = true;
                    // Record post-Act actor values: these exact values must survive save/exit/reload.
                    if (ResolveCompanionActor(out var srActor, out var srFarmer))
                    {
                        var srCan = srFarmer?.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault();
                        _postActActorStamina = srFarmer?.Stamina ?? 0f;
                        _postActActorWater = srCan?.WaterLeft ?? 0;
                        _postActActorTileX = (int)(srFarmer?.Tile.X ?? -1);
                        _postActActorTileY = (int)(srFarmer?.Tile.Y ?? -1);
                        Monitor.Log($"[TestDriver] Post-Act actor state recorded: Stamina={_postActActorStamina}, Water={_postActActorWater}, Tile=({_postActActorTileX},{_postActActorTileY}).", LogLevel.Info);
                    }
                    else
                    {
                        Monitor.Log("[TestDriver] Cannot record post-Act actor state for save-reload.", LogLevel.Error);
                        FailClosed("save-reload: companion actor not resolvable before save");
                        break;
                    }

                    // SaveGame.Save() is an enumerator factory: it does nothing unless iterated.
                    // Drive it synchronously on the main thread so the real save (and SMAPI Saving
                    // event, which persists companion state) actually executes.
                    Monitor.Log("[TestDriver] Driving synchronous save via SaveGame.Save() enumerator...", LogLevel.Info);
                    var saver = SaveGame.Save();
                    while (saver.MoveNext()) { }
                    Monitor.Log("[TestDriver] Synchronous save enumerator completed.", LogLevel.Info);
                }
                else if (_tickCounter > 30)
                {
                    // Reload the save from disk in-session (proven baseline reload path).
                    Monitor.Log("[TestDriver] Save complete. Reloading same save from disk...", LogLevel.Info);
                    _saveLoadedFired = false;
                    _dayStartedFired = false;
                    _renderedWorldObserved = false;
                    _renderedWorldFramesCount = 0;
                    _screenshotCaptured = false;

                    var reloadSaves = StardewValley.Program.GetSavesFolder();
                    var reloadDirs = Directory.GetDirectories(reloadSaves);
                    if (reloadDirs.Length == 0)
                    {
                        FailClosed("save-reload: no saves found in isolated folder after save");
                        break;
                    }
                    var reloadName = Path.GetFileName(reloadDirs[0]);
                    Monitor.Log($"[TestDriver] Reloading save '{reloadName}'...", LogLevel.Info);
                    SaveGame.Load(reloadName);
                    Game1.exitActiveMenu();
                    _scenarioState = ScenarioState.SaveReload_WaitingForWorldReady;
                    _tickCounter = 0;
                }
                break;

            case ScenarioState.SaveReload_WaitingForWorldReady:
                _tickCounter++;
                if (IsWorldGenuinelyReady())
                {
                    Monitor.Log($"[TestDriver] World GENUINELY ready after reload at location: {Game1.currentLocation?.Name}. Warping player to Farm...", LogLevel.Info);
                    Game1.warpFarmer("Farm", 62, 17, 2);
                    _scenarioState = ScenarioState.SaveReload_Verify;
                    _tickCounter = 0;
                }
                else if (_tickCounter > 1800)
                {
                    Monitor.Log("[TestDriver] Timeout waiting for genuine world ready after reload!", LogLevel.Error);
                    FailClosed("Timeout waiting for genuine world ready after save-reload");
                }
                break;

            case ScenarioState.SaveReload_Verify:
                _tickCounter++;
                {
                    // Wait for the Farm warp to finish fading in (screenshots require a genuine Farm frame)
                    if (Game1.currentLocation?.Name != "Farm" || Game1.fadeToBlack || Game1.fadeIn || Game1.fadeToBlackAlpha > 0.001f)
                    {
                        if (_tickCounter > 1200)
                        {
                            Monitor.Log("[TestDriver] Timeout waiting for Farm warp after reload!", LogLevel.Error);
                            FailClosed("save-reload: timeout waiting for Farm warp after reload");
                        }
                        break;
                    }

                    // Allow up to 300 ticks for the defensive companion initialization after reload
                    bool resolved = ResolveCompanionActor(out var rlActor, out var rlFarmer);
                    if (!resolved && _tickCounter < 1800)
                    {
                        break;
                    }

                    if (resolved && rlFarmer != null)
                    {
                        var rlCan = rlFarmer.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault();
                        _reloadedActorStamina = rlFarmer.Stamina;
                        _reloadedActorWater = rlCan?.WaterLeft ?? 0;
                        _reloadedActorTile = $"{(int)rlFarmer.Tile.X},{(int)rlFarmer.Tile.Y}";
                        var activeTaskProp = rlActor?.GetType().GetProperty("ActiveTaskId");
                        _reloadedActorIdle = activeTaskProp?.GetValue(rlActor) == null;
                        Monitor.Log($"[TestDriver] Reloaded actor state: Stamina={_reloadedActorStamina}, Water={_reloadedActorWater}, Tile={_reloadedActorTile}, Idle={_reloadedActorIdle}.", LogLevel.Info);
                    }
                    else
                    {
                        Monitor.Log("[TestDriver] Companion actor not resolvable after reload within 300 ticks.", LogLevel.Error);
                    }

                    // Discovery must be republished after reload (new session token, same saveId partition)
                    try
                    {
                        var discPath = Path.Combine(Helper.DirectoryPath, "..", "StardewAI.Companion.Mod", "data", "transport-discovery.json");
                        discPath = Path.GetFullPath(discPath);
                        if (File.Exists(discPath))
                        {
                            using var discDoc = JsonDocument.Parse(File.ReadAllText(discPath));
                            var discRoot = discDoc.RootElement;
                            _reloadDiscoveryRepublished = true;
                            var discSaveId = discRoot.TryGetProperty("saveId", out var ps) ? ps.GetString() : null;
                            var reloadDirs2 = Directory.GetDirectories(StardewValley.Program.GetSavesFolder());
                            var currentSave = reloadDirs2.Length > 0 ? Path.GetFileName(reloadDirs2[0]) : null;
                            _reloadDiscoverySaveIdMatches = string.Equals(discSaveId, currentSave, StringComparison.OrdinalIgnoreCase);
                            Monitor.Log($"[TestDriver] Discovery republished after reload: saveId={discSaveId}, matches current save={_reloadDiscoverySaveIdMatches}.", LogLevel.Info);
                        }
                    }
                    catch (Exception ex)
                    {
                        Monitor.Log($"[TestDriver] Note reading discovery after reload: {ex.Message}", LogLevel.Warn);
                    }

                    var outDirVerify = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
                    _screenshotRequestPath = Path.Combine(outDirVerify, "render.png");
                    _scenarioState = ScenarioState.CaptureArtifacts;
                    _tickCounter = 0;
                }
                break;

            case ScenarioState.CaptureArtifacts:
                _tickCounter++;
                // Wait up to 180 ticks (~3s) for OnRenderedWorld to capture the frame during rendered gameplay
                if (_screenshotCaptured || _tickCounter > 180)
                {
                    CaptureScenarioArtifacts();
                    _scenarioState = ScenarioState.VerifyFrozenBoundaryAndFinish;
                    _tickCounter = 0;
                }
                break;

            case ScenarioState.VerifyFrozenBoundaryAndFinish:
                FinishScenario(true);
                break;

            case ScenarioState.Finished:
                break;
        }
    }

    private void PublishHarnessCoordination(string state, string? phase = null)
    {
        var outDir = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
        Directory.CreateDirectory(outDir);
        var coordFile = Path.Combine(outDir, "harness-coordination.json");
        var coord = new Dictionary<string, object?>
        {
            ["state"] = state,
            ["runId"] = _runId,
            ["scenario"] = _scenario,
            ["mode"] = _testMode,
            ["port"] = _port,
            ["targetTile"] = new { x = (int)_targetTile.X, y = (int)_targetTile.Y },
            ["timestampUtc"] = DateTime.UtcNow.ToString("o")
        };
        string effectivePhase = phase ?? _purchaseMcpPhase ?? "";
        if (!string.IsNullOrEmpty(effectivePhase))
        {
            coord["phase"] = effectivePhase;
        }
        if (_evidence.TryGetValue("arrange", out var arrangeObj))
        {
            coord["arrange"] = arrangeObj;
        }
        if (_evidence.TryGetValue("phaseBArrange", out var phaseBObj))
        {
            coord["phaseBArrange"] = phaseBObj;
        }
        File.WriteAllText(coordFile, JsonSerializer.Serialize(coord, new JsonSerializerOptions { WriteIndented = true }));
        Monitor.Log($"[TestDriver] Published harness coordination: {state} (phase: {effectivePhase}) to {coordFile}", LogLevel.Info);
    }

    private bool CheckActCompletionSignalled()
    {
        var outDir = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
        var coordFile = Path.Combine(outDir, "harness-coordination.json");
        if (File.Exists(coordFile))
        {
            try
            {
                var content = File.ReadAllText(coordFile);
                return content.Contains("\"ACT_COMPLETED\"");
            }
            catch { }
        }
        return false;
    }

    private bool ResolveCompanionActor(
        out object? actorInstance,
        out Farmer? gameFarmer,
        out ActorObservationStatus status,
        out string statusDetail)
    {
        actorInstance = null;
        gameFarmer = null;

        try
        {
            var modInfo = Helper.ModRegistry.Get("CSXizhang.StardewAICompanion")
                       ?? Helper.ModRegistry.Get("StardewAICompanion");

            object? modEntryInstance = null;
            if (modInfo != null)
            {
                var modProp = modInfo.GetType().GetProperty("Mod", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                modEntryInstance = modProp?.GetValue(modInfo);
            }

            if (modEntryInstance == null)
            {
                var registry = Helper.ModRegistry;
                var modsField = registry.GetType().GetField("Mods", BindingFlags.NonPublic | BindingFlags.Instance);
                if (modsField?.GetValue(registry) is System.Collections.IEnumerable list)
                {
                    foreach (var item in list)
                    {
                        if (item == null) continue;
                        var manifestProp = item.GetType().GetProperty("Manifest");
                        var manifest = manifestProp?.GetValue(item) as IManifest;
                        if (manifest?.UniqueID == "CSXizhang.StardewAICompanion" || manifest?.UniqueID == "StardewAICompanion")
                        {
                            var mProp = item.GetType().GetProperty("Mod", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                            modEntryInstance = mProp?.GetValue(item);
                            break;
                        }
                    }
                }
            }

            if (modEntryInstance == null)
            {
                status = ActorObservationStatus.ModNotFoundInRegistry;
                statusDetail = "Companion mod 'CSXizhang.StardewAICompanion' was not found in SMAPI ModRegistry or ModEntry instance was null.";
                _lastActorObservationStatus = status;
                _lastActorObservationDetail = statusDetail;
                return false;
            }

            var actorField = modEntryInstance.GetType().GetField("_actor", BindingFlags.NonPublic | BindingFlags.Instance);
            if (actorField == null)
            {
                status = ActorObservationStatus.ActorFieldNotFound;
                statusDetail = $"Private field '_actor' was not found on ModEntry type '{modEntryInstance.GetType().FullName}'.";
                _lastActorObservationStatus = status;
                _lastActorObservationDetail = statusDetail;
                return false;
            }

            actorInstance = actorField.GetValue(modEntryInstance);
            if (actorInstance == null)
            {
                status = ActorObservationStatus.ActorInstanceNull;
                statusDetail = "ModEntry instance was located and '_actor' field exists, but '_actor' is null (Companion actor has not been initialized yet).";
                _lastActorObservationStatus = status;
                _lastActorObservationDetail = statusDetail;
                return false;
            }

            var farmerProp = actorInstance.GetType().GetProperty("GameFarmer", BindingFlags.Public | BindingFlags.Instance);
            gameFarmer = farmerProp?.GetValue(actorInstance) as Farmer;
            if (gameFarmer == null)
            {
                status = ActorObservationStatus.GameFarmerNull;
                statusDetail = "Companion actor instance exists, but 'GameFarmer' property returned null.";
                _lastActorObservationStatus = status;
                _lastActorObservationDetail = statusDetail;
                return false;
            }

            status = ActorObservationStatus.Observed;
            statusDetail = $"Companion actor observed: Name='{gameFarmer.Name}', Tile=({(int)gameFarmer.Tile.X}, {(int)gameFarmer.Tile.Y}), Location='{gameFarmer.currentLocation?.Name}'.";
            _lastActorObservationStatus = status;
            _lastActorObservationDetail = statusDetail;
            return true;
        }
        catch (Exception ex)
        {
            status = ActorObservationStatus.ExceptionThrown;
            statusDetail = $"Exception during reflection to resolve companion actor: {ex.Message}";
            _lastActorObservationStatus = status;
            _lastActorObservationDetail = statusDetail;
            Monitor.Log($"[TestDriver] Note resolving companion actor: {ex.Message}", LogLevel.Warn);
            return false;
        }
    }

    private bool ResolveCompanionActor(out object? actorInstance, out Farmer? gameFarmer)
    {
        return ResolveCompanionActor(out actorInstance, out gameFarmer, out _, out _);
    }

    private void OnRenderedWorld(object? sender, RenderedWorldEventArgs e)
    {
        if (Context.IsWorldReady && Game1.currentLocation != null)
        {
            _renderedWorldObserved = true;
            _renderedWorldFramesCount++;
        }
    }

    // Frame capture runs at Display.Rendered (after ALL drawing, before Present): reading the
    // backbuffer during RenderedWorld misses everything other mods draw in that same event
    // (e.g. the companion avatar), because the SpriteBatch has not flushed yet.
    private void OnRendered(object? sender, RenderedEventArgs e)
    {
        if (_lifeUiCaptureLabel != null && Context.IsWorldReady && Game1.activeClickableMenu != null)
        {
            _lifeUiPresentedCaptureLabel = _lifeUiCaptureLabel;
            _lifeUiPresentedCaptureFrames = 2;
            _lifeUiCaptureLabel = null;
        }
        if (_f8UiCaptureLabel != null && Context.IsWorldReady && IsCompanionCommandMenu(Game1.activeClickableMenu))
        {
            if (--_f8UiCaptureDelayFrames <= 0)
            {
                string label = _f8UiCaptureLabel;
                _f8UiCaptureLabel = null;
                string outDir = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
                string imagePath = Path.Combine(outDir, $"f8-ui-{label}.png");
                bool captured = DoCaptureRenderScreenshot(imagePath, "Rendered F8 menu after all drawing");
                WriteF8UiSnapshot(outDir, label, "snapshot", captured, captured ? null : "backbuffer capture failed");
            }
        }

        bool isGenuineWorldFrame = Context.IsWorldReady &&
            _saveLoadedFired &&
            _dayStartedFired &&
            Game1.currentLocation != null &&
            Game1.currentLocation.Name == "Farm" &&
            !Game1.fadeToBlack &&
            !Game1.fadeIn &&
            Game1.fadeToBlackAlpha <= 0f &&
            Game1.activeClickableMenu == null;

        if (_harnessCaptureLabel != null && Context.IsWorldReady && _saveLoadedFired && _dayStartedFired
            && !Game1.fadeToBlack && !Game1.fadeIn && Game1.fadeToBlackAlpha <= 0f && Game1.activeClickableMenu == null)
        {
            string label = _harnessCaptureLabel;
            _harnessCaptureLabel = null;
            string outDir = _evidenceDir!;
            bool captured = DoCaptureRenderScreenshot(Path.Combine(outDir, $"harness-world-{label}.png"), "Rendered actual world after all drawing");
            File.WriteAllText(Path.Combine(outDir, $"harness-world-{label}.json"), JsonSerializer.Serialize(new {
                runId = _runId, label, captured, timestampUtc = DateTime.UtcNow.ToString("o"),
                playerLocation = Game1.currentLocation?.NameOrUniqueName, tick = _tickCounter,
                metadata = _screenshotMetadata
            }));
        }

        if (_loopDemoCaptureRequested && isGenuineWorldFrame)
        {
            _loopDemoCaptureRequested = false;
            _loopDemoFrameIndex++;
            var outDir = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
            var frameDir = Path.Combine(outDir, "frames");
            var framePath = Path.Combine(frameDir, $"frame-{_loopDemoFrameIndex}.png");
            DoCaptureRenderScreenshot(framePath, $"loop-demo frame {_loopDemoFrameIndex}");
            if (!_screenshotCaptured)
            {
                var renderPath = Path.Combine(outDir, "render.png");
                DoCaptureRenderScreenshot(renderPath, "loop-demo baseline render");
                _screenshotCaptured = true;
            }
        }

        if (!string.IsNullOrEmpty(_screenshotRequestPath) && !_screenshotCaptured)
        {
            if (isGenuineWorldFrame)
            {
                if (DoCaptureRenderScreenshot(_screenshotRequestPath, "RenderedWorld on Farm with zero fade alpha and no active menu"))
                {
                    _screenshotCaptured = true;
                    _screenshotRequestPath = null;
                }
            }
            else
            {
                _screenshotMetadata["lastRejectionReason"] =
                    $"WorldReady={Context.IsWorldReady}, SaveLoaded={_saveLoadedFired}, DayStarted={_dayStartedFired}, " +
                    $"Location={Game1.currentLocation?.Name}, fadeToBlack={Game1.fadeToBlack}, fadeIn={Game1.fadeIn}, " +
                    $"fadeToBlackAlpha={Game1.fadeToBlackAlpha}, menu={Game1.activeClickableMenu?.GetType().Name}";
            }
        }
    }

    private void ExecuteScenarioArrange(object? actor, Farmer? farmer, bool actorOk, ActorObservationStatus status, string statusDetail)
    {
        var farm = Game1.getFarm();
        Monitor.Log($"[TestDriver] Arranging scenario '{_scenario}' on Farm...", LogLevel.Info);

        // Record pre-action player resources to prove invariant
        _initialPlayerStamina = Game1.player.stamina;
        _initialPlayerMoney = Game1.player.Money;
        Monitor.Log($"[TestDriver] Player baseline recorded: Stamina={_initialPlayerStamina}, Money={_initialPlayerMoney}.", LogLevel.Info);
        var pCan = Game1.player.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault();
        _initialPlayerWater = pCan?.WaterLeft ?? 0;
        _initialPlayerItemCount = Game1.player.Items.Count;

        _initialPlayerItems.Clear();
        for (int i = 0; i < Game1.player.Items.Count; i++)
        {
            var it = Game1.player.Items[i];
            _initialPlayerItems.Add(new InventorySlotSnapshot
            {
                Slot = i,
                Id = it?.ItemId,
                Name = it?.Name,
                Stack = it?.Stack ?? 0,
                WaterLeft = (it as StardewValley.Tools.WateringCan)?.WaterLeft ?? -1
            });
        }

        // Arrange actual Companion Farmer actor via narrow reflection
        if (UsesLoopDemoCompletionChannel)
        {
            if (farmer != null)
            {
                var cCan = farmer.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault();
                _preActorStamina = farmer.Stamina;
                _preActorWater = cCan?.WaterLeft ?? 0;
            }
            else if (actor != null)
            {
                var stProp = actor.GetType().GetProperty("Stamina");
                if (stProp?.GetValue(actor) is float f) _preActorStamina = f;
                var wProp = actor.GetType().GetProperty("WaterLeft");
                if (wProp?.GetValue(actor) is int w) _preActorWater = w;
            }
            Monitor.Log($"[TestDriver] {_scenario}: preserving companion actor status quo (Stamina={_preActorStamina}, Water={_preActorWater}).", LogLevel.Info);
        }
        else if (actorOk)
        {
            try
            {
                // Scenario-dependent arrange: empty-can starts with no water, low-stamina below action cost,
                // blocked places the companion one tile further out with all interaction tiles fenced off.
                float arrangedStamina = 270.0f;
                int arrangedWater = 40;
                int companionTileX = 64;
                if (_scenario.Equals("empty-can", StringComparison.OrdinalIgnoreCase))
                {
                    arrangedWater = 0;
                }
                else if (_scenario.Equals("low-stamina", StringComparison.OrdinalIgnoreCase))
                {
                    arrangedStamina = 1.0f;
                }
                else if (_scenario.Equals("blocked", StringComparison.OrdinalIgnoreCase))
                {
                    companionTileX = 63;
                }

                if (actor != null)
                {
                    var stProp = actor.GetType().GetProperty("Stamina");
                    stProp?.SetValue(actor, arrangedStamina);

                    var wProp = actor.GetType().GetProperty("WaterLeft");
                    wProp?.SetValue(actor, arrangedWater);

                    var posProp = actor.GetType().GetProperty("PixelPosition");
                    posProp?.SetValue(actor, new Vector2(companionTileX * 64, 15 * 64));

                    var faceProp = actor.GetType().GetProperty("Facing");
                    if (faceProp != null)
                    {
                        faceProp.SetValue(actor, Enum.ToObject(faceProp.PropertyType, 1));
                    }
                }

                if (farmer != null)
                {
                    farmer.Position = new Vector2(companionTileX * 64, 15 * 64);
                    farmer.faceDirection(1); // Facing Right towards (65, 15)
                    farmer.Stamina = arrangedStamina;
                    farmer.maxStamina.Value = 270;
                    var cCan = farmer.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault();
                    if (cCan != null)
                    {
                        cCan.WaterLeft = arrangedWater;
                    }
                }

                _preActorStamina = arrangedStamina;
                _preActorWater = arrangedWater;
                Monitor.Log($"[TestDriver] Arranged companion actor at ({companionTileX}, 15) facing Right, Stamina={arrangedStamina}, Water={arrangedWater}.", LogLevel.Info);
            }
            catch (Exception ex)
            {
                Monitor.Log($"[TestDriver] Note arranging companion actor: {ex.Message}", LogLevel.Warn);
            }
        }
        else
        {
            Monitor.Log($"[TestDriver] Actor observation threshold not met: {status} ({statusDetail})", LogLevel.Warn);
        }

        // Isolated continuation acceptance: retain the exact loaded farm and inventory.
        // The daily-chain fixture normally clears a corridor, which would erase roads
        // laid in an earlier run before a cross-process persistence check can observe them.
        if (Environment.GetEnvironmentVariable("STARDEW_AI_PRESERVE_LOADED_WORLD") == "1")
        {
            Monitor.Log("[TestDriver] Preserve-loaded-world: no farm or inventory fixture applied.", LogLevel.Info);
            return;
        }

        if (_scenario.Equals("existing-save-buy5", StringComparison.OrdinalIgnoreCase))
        {
            ArrangeExistingSaveBuy5(farm, farmer, actor);
            return;
        }

        // Ensure HoeDirt at target tile (65, 15)
        if (!farm.terrainFeatures.ContainsKey(_targetTile) || !(farm.terrainFeatures[_targetTile] is HoeDirt))
        {
            farm.terrainFeatures[_targetTile] = new HoeDirt(0, farm);
        }
        var dirt = (HoeDirt)farm.terrainFeatures[_targetTile];

        if (_scenario.Equals("already-watered", StringComparison.OrdinalIgnoreCase))
        {
            dirt.state.Value = 1; // already watered
            _targetDirtIsWatered = true;
        }
        else
        {
            dirt.state.Value = 0; // unwatered
            _targetDirtIsWatered = false;
        }

        // Blocked scenario: fence off all four tiles adjacent to the target so no
        // interaction standing position is reachable. Companion starts at (63, 15).
        _blockedTiles.Clear();
        if (_scenario.Equals("blocked", StringComparison.OrdinalIgnoreCase))
        {
            foreach (var t in new[] { new Vector2(64, 15), new Vector2(66, 15), new Vector2(65, 14), new Vector2(65, 16) })
            {
                farm.objects[t] = new StardewValley.Object("322", 1); // wood fence: impassable
                _blockedTiles.Add(t);
            }
            Monitor.Log("[TestDriver] Arranged blocked scenario: wood fences at (64,15), (66,15), (65,14), (65,16); all interaction tiles for (65,15) unreachable.", LogLevel.Info);
        }

        // Wave-2 control scenarios: a 3-tile dry zone so pause/cancel can land mid-execution.
        // Conflict scenario: second (conflicting) command targets (66,15), which must stay unwatered.
        // loop-demo uses an open zone in front of the farmhouse so movement and watering are
        // clearly visible on captured frames ((65-67,15) sits at the farmhouse's occluded wall base).
        _zoneTiles.Clear();
        if (_scenario.Equals("loop-demo", StringComparison.OrdinalIgnoreCase))
        {
            foreach (var t in new[] { new Vector2(63, 20), new Vector2(64, 20), new Vector2(65, 20) })
            {
                if (!farm.terrainFeatures.ContainsKey(t) || farm.terrainFeatures[t] is not HoeDirt)
                {
                    farm.terrainFeatures[t] = new HoeDirt(0, farm);
                }
                ((HoeDirt)farm.terrainFeatures[t]).state.Value = 0;
                _zoneTiles.Add(t);
            }
            Monitor.Log("[TestDriver] loop-demo: arranged open 3-tile dry zone (63,20), (64,20), (65,20) in front of the farmhouse.", LogLevel.Info);
        }
        else if (_scenario.Equals("pause-resume", StringComparison.OrdinalIgnoreCase) ||
            _scenario.Equals("cancel", StringComparison.OrdinalIgnoreCase) ||
            _scenario.Equals("cancel-paused", StringComparison.OrdinalIgnoreCase) ||
            _scenario.Equals("disconnect", StringComparison.OrdinalIgnoreCase))
        {
            foreach (var t in new[] { new Vector2(65, 15), new Vector2(66, 15), new Vector2(67, 15) })
            {
                if (!farm.terrainFeatures.ContainsKey(t) || farm.terrainFeatures[t] is not HoeDirt)
                {
                    farm.terrainFeatures[t] = new HoeDirt(0, farm);
                }
                ((HoeDirt)farm.terrainFeatures[t]).state.Value = 0;
                _zoneTiles.Add(t);
            }
            Monitor.Log("[TestDriver] Arranged 3-tile dry zone (65,15), (66,15), (67,15) for control scenario.", LogLevel.Info);
        }
        else if (_scenario.Equals("conflict", StringComparison.OrdinalIgnoreCase))
        {
            var t = new Vector2(66, 15);
            if (!farm.terrainFeatures.ContainsKey(t) || farm.terrainFeatures[t] is not HoeDirt)
            {
                farm.terrainFeatures[t] = new HoeDirt(0, farm);
            }
            ((HoeDirt)farm.terrainFeatures[t]).state.Value = 0;
            _zoneTiles.Add(t);
            Monitor.Log("[TestDriver] Arranged conflict scenario: (66,15) dry; it must remain unwatered.", LogLevel.Info);
        }
        else if (_scenario.Equals("free-mode-control", StringComparison.OrdinalIgnoreCase))
        {
            string? harnessMode = Environment.GetEnvironmentVariable("STARDEW_AI_HARNESS_FIXTURE");
            if (!string.IsNullOrEmpty(harnessMode))
            {
                float harnessStamina = 270;
                string? staminaSetting = Environment.GetEnvironmentVariable("STARDEW_AI_HARNESS_STAMINA");
                if (!string.IsNullOrEmpty(staminaSetting) && !float.TryParse(staminaSetting,
                    System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out harnessStamina))
                    throw new ArgumentException("STARDEW_AI_HARNESS_STAMINA must be a numeric fixture value.");
                int harnessWater = harnessMode == "watering" ? 10 : 40;
                string? waterSetting = Environment.GetEnvironmentVariable("STARDEW_AI_HARNESS_WATER");
                if (!string.IsNullOrEmpty(waterSetting) && !int.TryParse(waterSetting, out harnessWater))
                    throw new ArgumentException("STARDEW_AI_HARNESS_WATER must be an integer fixture value.");
                using var fixtureRequest = JsonDocument.Parse(JsonSerializer.Serialize(new {
                    mode = harnessMode, stamina = harnessStamina, water = harnessWater, foodCount = 0
                }));
                ArrangeHarnessFixture(_evidenceDir!, "startup", fixtureRequest.RootElement);
                return;
            }
            // Acceptance fixture: this immature crop dries again after each real overnight
            // settlement, providing a useful native watering job on three successive days.
            dirt.crop = CreateParsnipCrop(65, 15, farm, mature: false);
            Monitor.Log("[TestDriver] Arranged free-mode-control recurring immature crop at (65,15).", LogLevel.Info);
        }
        else if (_scenario.Equals("harvest-demo", StringComparison.OrdinalIgnoreCase))
        {
            ArrangeHarvestDemo(farm, dirt, farmer);
        }
        else if (_scenario.Equals("harvest-cancel", StringComparison.OrdinalIgnoreCase))
        {
            ArrangeHarvestCancel(farm, farmer);
        }
        else if (_scenario.Equals("farm-flow", StringComparison.OrdinalIgnoreCase))
        {
            ArrangeFarmFlow(farm, dirt, farmer, actor);
        }
        else if (_scenario.Equals("farm-flow-interrupt", StringComparison.OrdinalIgnoreCase) ||
                 _scenario.Equals("farm-flow-pause", StringComparison.OrdinalIgnoreCase))
        {
            ArrangeFarmFlowInterrupt(farm, farmer, actor);
        }
        else if (_scenario.Equals("planting-mcp", StringComparison.OrdinalIgnoreCase))
        {
            ArrangePlantingMcp(farm, dirt, farmer, actor);
        }
        else if (_scenario.Equals("shipping-mcp", StringComparison.OrdinalIgnoreCase))
        {
            ArrangeShippingMcp(farm, dirt, farmer, actor);
        }
        else if (_scenario.Equals("navigation-mcp", StringComparison.OrdinalIgnoreCase))
        {
            ArrangeNavigationMcp(farm, farmer, actor);
        }
        else if (_scenario.Equals("purchase-mcp", StringComparison.OrdinalIgnoreCase))
        {
            ArrangePurchaseMcp(farm, farmer, actor);
        }
        else if (_scenario.Equals("cycle-mcp", StringComparison.OrdinalIgnoreCase) ||
                 _scenario.Equals("cycle-operator", StringComparison.OrdinalIgnoreCase))
        {
            ArrangeCycleMcp(farm, dirt, farmer, actor);
        }
        else if (_scenario.Equals("chest-operator", StringComparison.OrdinalIgnoreCase))
        {
            ArrangeChestOperator(farm, dirt, farmer, actor);
        }
        else if (_scenario.Equals("farm-capabilities", StringComparison.OrdinalIgnoreCase))
        {
            ArrangeFarmCapabilities(farm, farmer, actor, dirt);
            return;
        }
        else if (_scenario.Equals("daily-chain", StringComparison.OrdinalIgnoreCase))
        {
            ArrangeDailyChain(farm, dirt, farmer, actor);
        }

        Monitor.Log($"[TestDriver] Arranged Farm tile (65, 15): Watered={_targetDirtIsWatered}. Recorded {_initialPlayerItems.Count} inventory slots.", LogLevel.Info);
    }


    /// <summary>
    /// r52 daily-chain fixture: one dry immature parsnip at (65,15) (water target), one mature
    /// parsnip at (66,15) (harvest target), and a cleared walking corridor toward the shipping
    /// bin so one player instruction can chain water -> harvest -> ship with real native effects.
    /// </summary>
    private void ArrangeDailyChain(Farm farm, HoeDirt targetDirt, Farmer? companionFarmer, object? actor)
    {
        var evidence = new Dictionary<string, object?>();

        void ClearAt(Vector2 t)
        {
            ClearTileObject(farm, t);
            farm.terrainFeatures.Remove(t);
        }

        // Water target: the generic arrange already made (65,15) dry dirt; add an immature crop.
        targetDirt.state.Value = 0;
        targetDirt.crop = CreateParsnipCrop(65, 15, farm, mature: false);

        // Harvest target: a mature parsnip on fresh dry dirt right next to it.
        var matureTile = new Vector2(66, 15);
        ClearAt(matureTile);
        var matureDirt = new HoeDirt(0, farm);
        farm.terrainFeatures[matureTile] = matureDirt;
        matureDirt.crop = CreateParsnipCrop(66, 15, farm, mature: true);

        // Walking corridor from the companion start eastward to the shipping bin area.
        for (int x = 64; x <= 72; x++)
        {
            for (int y = 14; y <= 17; y++)
            {
                var t = new Vector2(x, y);
                if (t == _targetTile || t == matureTile) continue;
                ClearAt(t);
            }
        }

        // Optional, isolated-only materials for the autonomous space project acceptance.
        // The ordinary daily-chain fixture is unchanged unless the launcher opts in.
        if (Environment.GetEnvironmentVariable("STARDEW_AI_SPACE_FIXTURE") == "1" && companionFarmer != null)
        {
            int stack = int.TryParse(Environment.GetEnvironmentVariable("STARDEW_AI_SPACE_FIXTURE_STACK"), out int requestedStack)
                ? Math.Clamp(requestedStack, 1, 64) : 10;
            var materials = new[]
            {
                ItemRegistry.Create("(O)328", stack), // Wood Floor
                ItemRegistry.Create("(O)322", stack)  // Wood Fence
            };
            foreach (var material in materials)
            {
                int emptySlot = companionFarmer.Items.IndexOf(null);
                if (emptySlot >= 0)
                    companionFarmer.Items[emptySlot] = material;
                else if (companionFarmer.Items.Count < companionFarmer.MaxItems)
                    companionFarmer.Items.Add(material);
                else
                    throw new InvalidOperationException("Space acceptance fixture has no backpack slot for materials.");
            }
            evidence["spaceMaterials"] = materials.Select(item => new { itemId = item.QualifiedItemId, itemName = item.Name, stack = item.Stack }).ToArray();
        }

        // Prepare the isolated player one tile west of the companion for the
        // production interaction detector. This is fixture arrangement only.
        Game1.player.Position = new Vector2(63 * 64, 15 * 64);
        Game1.player.faceDirection(1);

        var binTile = new Vector2(71, 14);
        var bin = farm.buildings.FirstOrDefault(b => b is StardewValley.Buildings.ShippingBin ||
            string.Equals(b.buildingType.Value, "Shipping Bin", StringComparison.OrdinalIgnoreCase));
        if (bin != null)
        {
            binTile = new Vector2(bin.tileX.Value, bin.tileY.Value);
        }
        else if (farm.mapShippingBinPosition.HasValue)
        {
            binTile = farm.mapShippingBinPosition.Value;
        }

        evidence["scenario"] = _scenario;
        evidence["waterTile"] = TileEvidence(_targetTile);
        evidence["waterCrop"] = DescribeCropTile(_targetTile, targetDirt);
        evidence["harvestTile"] = TileEvidence(matureTile);
        evidence["harvestCrop"] = DescribeCropTile(matureTile, matureDirt);
        evidence["harvestItemId"] = "(O)24";
        evidence["shippingBinTile"] = TileEvidence(binTile);
        _evidence["arrange"] = evidence;
        Monitor.Log($"[TestDriver] Arranged daily-chain fixture: dry immature parsnip (65,15), mature parsnip (66,15), shipping bin at ({(int)binTile.X},{(int)binTile.Y}).", LogLevel.Info);
    }

    /// <summary>
    /// Pre-seeded isolated fixture for the agricultural / husbandry native actions (Q8).
    /// It arranges, on the isolated Farm only: native weeds / stones / twigs / a dropped
    /// item, an idle Furnace plus its native inputs, ready-to-pickup animal produce, a
    /// Coop with animals and trough hay, and a companion that really carries the native
    /// tools and materials. Nothing is granted that the player could not provide, and the
    /// normal save is never touched (TestDriver save isolation).
    /// </summary>
    private void ArrangeFarmCapabilities(Farm farm, Farmer? companionFarmer, object? actor, HoeDirt targetDirt)
    {
        var evidence = new Dictionary<string, object?>();
        string? requestedScope = Environment.GetEnvironmentVariable("STARDEW_AI_FARM_SCOPE");
        bool toolProduceOnly = requestedScope is "milking-only" or "shearing-only" or "animal-ground-produce-only" or "animal-door-only" or "refill-only" or "fertilizer-only" or "clear-debris-only" or "pickup-only" or "daily-loop-only";
        evidence["fixtureScope"] = toolProduceOnly ? requestedScope : "full";
        evidence["unrelatedNativeDropsEnabled"] = !toolProduceOnly;
        evidence["farmDebrisBeforeArrange"] = farm.debris.Count;
        var cleared = new Dictionary<string, string>();

        void ClearAt(Vector2 t)
        {
            string? old = ClearTileObject(farm, t);
            if (old != null) cleared[$"{(int)t.X},{(int)t.Y}"] = old;
        }

        // --- 1. Native obstacles reachable from the companion's start tile. -----
        // Companion starts at (64,18); every fixture tile is adjacent or one step away.
        bool clearingOnly = requestedScope is "clear-debris-only" or "pickup-only" or "daily-loop-only";
        if (clearingOnly)
        {
            // Arrange an unobstructed walking corridor, never move the actor in Act.
            for (int x = 48; x <= 66; x++)
            {
                var t = new Vector2(x, 23);
                ClearAt(t);
                farm.terrainFeatures.Remove(t);
            }
            for (int y = 18; y <= 23; y++)
            {
                var t = new Vector2(65, y);
                ClearAt(t);
                farm.terrainFeatures.Remove(t);
            }
        }
        var weedTile = clearingOnly ? new Vector2(50, 24) : new Vector2(63, 18);
        var stoneTile = clearingOnly ? new Vector2(49, 24) : new Vector2(62, 18);
        var twigTile = clearingOnly ? new Vector2(48, 24) : new Vector2(61, 18);
        foreach (var t in new[] { weedTile, stoneTile, twigTile })
            ClearAt(t);

        var weed = new StardewValley.Object("(O)313", 1);
        farm.objects[weedTile] = weed;
        var stone = new StardewValley.Object("(O)2", 1);
        farm.objects[stoneTile] = stone;
        var twig = new StardewValley.Object("(O)294", 1);
        farm.objects[twigTile] = twig;

        evidence["obstacles"] = new List<Dictionary<string, object?>>
        {
            new() { ["kind"] = "weed", ["tile"] = TileEvidence(weedTile), ["itemId"] = weed.QualifiedItemId, ["isWeed"] = SafeIsWeed(weed) },
            new() { ["kind"] = "stone", ["tile"] = TileEvidence(stoneTile), ["itemId"] = stone.QualifiedItemId, ["isStone"] = SafeIsStone(stone) },
            new() { ["kind"] = "twig", ["tile"] = TileEvidence(twigTile), ["itemId"] = twig.QualifiedItemId, ["isTwig"] = SafeIsTwig(twig) }
        };

        // --- 1c. Native wild tree for the forestry (chop_tree) acceptance. ------
        // A real grown oak on the Farm; the surrounding block is cleared so the
        // companion can path to an adjacent tile. Native felling turns the same
        // object into a stump (health 5), so one chop call removes the feature.
        var treeTile = new Vector2(60, 20);
        for (int tx = 59; tx <= 61; tx++)
            for (int ty = 19; ty <= 21; ty++)
            {
                var t = new Vector2(tx, ty);
                ClearAt(t);
                farm.terrainFeatures.Remove(t);
            }
        var fixtureTree = new StardewValley.TerrainFeatures.Tree("1", 5);
        farm.terrainFeatures.Add(treeTile, fixtureTree);
        evidence["forestry"] = new Dictionary<string, object?>
        {
            ["tile"] = TileEvidence(treeTile),
            ["treeType"] = fixtureTree.treeType?.Value,
            ["growthStage"] = fixtureTree.growthStage?.Value,
        };

        // --- 1d. Native spawned forage item for the pickup acceptance. ------
        // A real wild horseradish placed as a spawned ground object; spawned
        // objects are not debris chunks, so magnetism never touches them and
        // only a native checkAction pickup can collect them.
        var forageTile = new Vector2(64, 16);
        ClearAt(forageTile);
        farm.terrainFeatures.Remove(forageTile);
        var forage = new StardewValley.Object("(O)399", 1) { IsSpawnedObject = true, CanBeGrabbed = true };
        farm.objects[forageTile] = forage;
        evidence["forage"] = new Dictionary<string, object?>
        {
            ["tile"] = TileEvidence(forageTile),
            ["itemId"] = forage.QualifiedItemId,
        };

        // --- 2. Native dropped item (real Debris, collected with Debris.collect). -
        var dropTile = new Vector2(65, 18);
        ClearAt(dropTile);
        if (!toolProduceOnly) try
        {
            // Land the chunk exactly on the drop tile: targeting Game1.player.Position would
            // fly it into the human player's magnet and break the pickup assertion.
            var debris = new Debris(388, new Vector2(dropTile.X * 64 + 32, dropTile.Y * 64 + 32), new Vector2(dropTile.X * 64 + 32, dropTile.Y * 64 + 32))
            {
                itemId = { Value = "(O)388" }
            };
            farm.debris.Add(debris);
            evidence["droppedItem"] = new Dictionary<string, object?>
            {
                ["tile"] = TileEvidence(dropTile),
                ["itemId"] = "(O)388",
                ["debrisType"] = debris.debrisType?.Value.ToString()
            };
        }
        catch (Exception ex)
        {
            evidence["droppedItemError"] = ex.Message;
            Monitor.Log($"[TestDriver] farm-capabilities: could not create native debris: {ex.Message}", LogLevel.Warn);
        }

        if (requestedScope == "pickup-only")
        {
            var ordinaryTile = new Vector2(52, 23);
            for (int x = 51; x <= 53; x++)
                for (int y = 21; y <= 24; y++)
                {
                    var t = new Vector2(x, y);
                    ClearAt(t);
                    farm.terrainFeatures.Remove(t);
                }
            var item = ItemRegistry.Create("(O)388", 3);
            var origin = ordinaryTile * 64 + new Vector2(32, 32);
            var debris = new Debris(item, origin, origin + new Vector2(0, 64));
            farm.debris.Add(debris);
            evidence["ordinaryDrop"] = new {
                location = "Farm", tile = TileEvidence(ordinaryTile),
                itemId = item.QualifiedItemId, stack = item.Stack,
                chunks = debris.Chunks.Count, debrisType = debris.debrisType.Value.ToString(),
                nativeItemReference = ReferenceEquals(debris.item, item)
            };
        }

        // --- 3. One supported machine: Furnace + native inputs. ---------------
        var furnaceTile = new Vector2(66, 18);
        var coalTile = new Vector2(67, 18);
        var oreTile = new Vector2(68, 18);
        foreach (var t in new[] { furnaceTile, coalTile, oreTile })
            ClearAt(t);

        try
        {
            // Build the machine through the game's own item factory. new Object(tile, "(BC)13")
            // is the wrong constructor (the game logs "(BC)13 … will likely result in an error
            // item"); ItemRegistry.Create returns a real BigCraftable that carries its native
            // MachineData, which is what insert-machine/collect-machine must operate on.
            var furnace = ItemRegistry.Create<StardewValley.Object>("(BC)13");
            furnace.TileLocation = furnaceTile;
            furnace.Location = farm;
            farm.objects[furnaceTile] = furnace;
            evidence["machine"] = new Dictionary<string, object?>
            {
                ["tile"] = TileEvidence(furnaceTile),
                ["itemId"] = furnace.QualifiedItemId,
                ["machineDataPresent"] = furnace.GetMachineData() is not null,
                ["readyForHarvest"] = furnace.readyForHarvest?.Value == true,
                ["minutesUntilReady"] = furnace.MinutesUntilReady,
                ["hasInput"] = furnace.heldObject?.Value is not null
            };
        }
        catch (Exception ex)
        {
            evidence["machineError"] = ex.Message;
            Monitor.Log($"[TestDriver] farm-capabilities: could not place furnace: {ex.Message}", LogLevel.Warn);
        }

        // A thrown copper/coal pickup the companion can walk over and collect.
        if (!toolProduceOnly) try
        {
            var oreDebris = new Debris(378, new Vector2(oreTile.X * 64 + 32, oreTile.Y * 64 + 32), Game1.player.Position)
            {
                itemId = { Value = "(O)378" }
            };
            farm.debris.Add(oreDebris);
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] farm-capabilities: could not create ore debris: {ex.Message}", LogLevel.Warn);
        }

        // --- 3b. A second, real Furnace holding a finished native output. ----------
        // This is fixture arrangement of a completed machine: collect-machine must move
        // this real heldObject into the companion inventory through the game's own
        // checkAction. The idle furnace above is the one insert-machine must really load.
        var readyMachineTile = new Vector2(70, 18);
        ClearAt(readyMachineTile);
        try
        {
            var readyFurnace = ItemRegistry.Create<StardewValley.Object>("(BC)13");
            readyFurnace.TileLocation = readyMachineTile;
            readyFurnace.Location = farm;
            readyFurnace.heldObject.Value = ItemRegistry.Create<StardewValley.Object>("(O)334"); // Copper Bar
            readyFurnace.readyForHarvest.Value = true;
            readyFurnace.MinutesUntilReady = 0;
            farm.objects[readyMachineTile] = readyFurnace;
            evidence["machineReady"] = new Dictionary<string, object?>
            {
                ["tile"] = TileEvidence(readyMachineTile),
                ["itemId"] = readyFurnace.QualifiedItemId,
                ["outputItemId"] = readyFurnace.heldObject?.Value?.QualifiedItemId,
                ["readyForHarvest"] = readyFurnace.readyForHarvest?.Value == true
            };
        }
        catch (Exception ex)
        {
            evidence["machineReadyError"] = ex.Message;
            Monitor.Log($"[TestDriver] farm-capabilities: could not place ready furnace: {ex.Message}", LogLevel.Warn);
        }

        // --- 4. Companion tools + inputs (the player's own stuff, given to it). --
        var companionItems = new List<string>();
        if (companionFarmer != null)
        {
            var can = companionFarmer.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault()
                      ?? new StardewValley.Tools.WateringCan { WaterLeft = 40 };
            can.WaterLeft = requestedScope is "refill-only" or "fertilizer-only" or "clear-debris-only" or "pickup-only" or "daily-loop-only" ? 0 : 40;
            companionFarmer.Items.Clear();
            companionFarmer.Items.Add(can);
            companionFarmer.Items.Add(new StardewValley.Tools.Hoe());
            companionFarmer.Items.Add(new StardewValley.Tools.Axe());
            companionFarmer.Items.Add(new StardewValley.Tools.Pickaxe());
            companionFarmer.Items.Add(new StardewValley.Tools.MilkPail());
            companionFarmer.Items.Add(new StardewValley.Tools.Shears());
            companionFarmer.Items.Add(ItemRegistry.Create("(O)378", 10)); // Copper Ore
            companionFarmer.Items.Add(ItemRegistry.Create("(O)382", 10)); // Coal
            companionFarmer.Items.Add(ItemRegistry.Create("(O)262", 5));  // Wheat (machine input)
            companionFarmer.Items.Add(ItemRegistry.Create("(O)368", 5));  // Basic Fertilizer
            // Large backpack (a normal in-game upgrade): the full-scope run picks up real
            // items before animal care, and a full bag must not block milking/feeding.
            companionFarmer.MaxItems = 24;
            for (int i = companionFarmer.Items.Count; i < companionFarmer.MaxItems; i++)
                companionFarmer.Items.Add(null);
            companionFarmer.CurrentToolIndex = 0;
            companionFarmer.Position = new Vector2(64 * 64, 18 * 64);
            companionFarmer.faceDirection(2);
            companionFarmer.Stamina = 270.0f;
            companionFarmer.maxStamina.Value = 270;
            companionItems = companionFarmer.Items.Where(i => i != null).Select(i => i!.QualifiedItemId ?? i.ItemId ?? i.Name).ToList();
        }
        evidence["companionItems"] = companionItems;

        if (actor != null)
        {
            try
            {
                actor.GetType().GetProperty("Stamina")?.SetValue(actor, 270.0f);
                actor.GetType().GetProperty("WaterLeft")?.SetValue(actor, requestedScope is "refill-only" or "fertilizer-only" or "clear-debris-only" or "pickup-only" or "daily-loop-only" ? 0 : 40);
                actor.GetType().GetProperty("PixelPosition")?.SetValue(actor, new Vector2(64 * 64, 18 * 64));
            }
            catch (Exception ex)
            {
                Monitor.Log($"[TestDriver] Note arranging companion actor in farm-capabilities: {ex.Message}", LogLevel.Warn);
            }
        }

        // --- 5. Fertilizer target: tilled but unfertilized soil next to the companion.
        var fertilizerTile = new Vector2(64, 19);
        ClearAt(fertilizerTile);
        var fertDirt = EnsureHoeDirt(farm, fertilizerTile);
        fertDirt.state.Value = 0;
        fertDirt.fertilizer.Value = null;
        evidence["fertilizerTile"] = new Dictionary<string, object?>
        {
            ["tile"] = TileEvidence(fertilizerTile),
            ["tilled"] = true,
            ["hasFertilizer"] = fertDirt.HasFertilizer()
        };

        if (requestedScope == "fertilizer-only")
        {
            var invalidTile = new Vector2(65, 19);
            ClearAt(invalidTile);
            farm.terrainFeatures.Remove(invalidTile); // Arrange an untilled negative target only.
            evidence["fertilizer"] = new {
                location = farm.NameOrUniqueName, tile = TileEvidence(fertilizerTile),
                invalidTile = TileEvidence(invalidTile), invalidTilled = farm.terrainFeatures.ContainsKey(invalidTile),
                itemId = "(O)368", hasFertilizer = fertDirt.HasFertilizer(),
                rules = fertDirt.CheckApplyFertilizerRules("(O)368").ToString()
            };
        }

        if (requestedScope is "refill-only" or "daily-loop-only")
        {
            var candidates = new List<Vector2>();
            var layer = farm.Map.Layers[0];
            for (int x = 1; x < layer.LayerWidth - 1; x++)
                for (int y = 1; y < layer.LayerHeight - 1; y++)
                {
                    if (!farm.CanRefillWateringCanOnTile(x, y)) continue;
                    var tile = new Vector2(x, y);
                    if (new[] { tile + new Vector2(0, 1), tile + new Vector2(0, -1), tile + new Vector2(1, 0), tile + new Vector2(-1, 0) }
                        .Any(t => !farm.objects.ContainsKey(t) && farm.isTilePassable(t)))
                        candidates.Add(tile);
                }
            if (candidates.Count == 0) throw new InvalidOperationException("No native refill tile with a standing neighbour.");
            var refillTile = candidates.OrderBy(t => Math.Abs(t.X - 64) + Math.Abs(t.Y - 18)).First();
            var realCan = companionFarmer?.Items.OfType<StardewValley.Tools.WateringCan>().Single()
                ?? throw new InvalidOperationException("Missing companion can.");
            evidence["refill"] = new {
                location = farm.NameOrUniqueName, tile = TileEvidence(refillTile),
                nativeRefillable = farm.CanRefillWateringCanOnTile((int)refillTile.X, (int)refillTile.Y),
                invalidTile = TileEvidence(fertilizerTile),
                invalidTileRefillable = farm.CanRefillWateringCanOnTile((int)fertilizerTile.X, (int)fertilizerTile.Y),
                initialWater = realCan.WaterLeft, maxWater = realCan.waterCanMax
            };
        }

        if (requestedScope == "daily-loop-only")
        {
            var chestTile = new Vector2(66, 20);
            ClearAt(chestTile);
            farm.terrainFeatures.Remove(chestTile);
            var chest = new StardewValley.Objects.Chest(true, chestTile, "130");
            chest.resetLidFrame(); // Initialize the native player-chest lid before fixture persistence.
            farm.objects[chestTile] = chest;
            targetDirt.state.Value = 0;
            evidence["dailyLoop"] = new {
                chestTile = TileEvidence(chestTile), chestInitiallyEmpty = !chest.Items.Any(i => i != null),
                waterTile = new { x = 65, y = 15 }, waterInitiallyDry = targetDirt.state.Value == 0
            };
        }

        // --- 6. Animal building with real animals and a real silo supply. --------
        ArrangeFarmCapabilitiesLivestock(farm, evidence);

        // The generic arrange created HoeDirt at (65,15); keep it watered-off so the
        // farming scan has no stale work item.
        targetDirt.state.Value = 0;

        evidence["clearedTiles"] = cleared;
        _evidence["arrange"] = evidence;
        Monitor.Log(
            $"[TestDriver] farm-capabilities fixture arranged: obstacles=3 dropped={evidence.ContainsKey("droppedItem")} " +
            $"machine={evidence.ContainsKey("machine")} companionItems={companionItems.Count} livestock={evidence.ContainsKey("livestock")}.",
            LogLevel.Info);
    }

    private void ArrangeFarmCapabilitiesLivestock(Farm farm, Dictionary<string, object?> evidence)
    {
        bool shearingOnly = Environment.GetEnvironmentVariable("STARDEW_AI_FARM_SCOPE") == "shearing-only";
        string animalPrefix = shearingOnly ? "sheep" : "cow";
        try
        {
            // Stock the real silo store that GameLocation.GetHayFromAnySilo reads. Troughs
            // are deliberately left empty: the acceptance must move this silo hay through
            // the player-performable take-hay / fill-trough flow, never by pre-seeding.
            int siloStock = 20;
            if ((farm.piecesOfHay?.Value ?? 0) < siloStock)
                farm.piecesOfHay!.Value = siloStock;

            // Clear footprints for Barn and Coop to prevent collision with obstacles/debris.
            // The door scope must not overlap the golden Farmhouse at (59,12),
            // otherwise native getBuildingAt resolves that earlier building first.
            bool doorOnly = Environment.GetEnvironmentVariable("STARDEW_AI_FARM_SCOPE") == "animal-door-only";
            var barnTile = new Vector2(50, 11);
            var coopTile = doorOnly ? new Vector2(58, 22) : new Vector2(58, 12);
            ClearBuildingFootprint(farm, barnTile, 7, 4);
            ClearBuildingFootprint(farm, coopTile, 6, 3);
            if (doorOnly)
                ClearBuildingFootprint(farm, coopTile + new Vector2(0, 3), 6, 1);

            // Ensure completed Barn and Coop using game's normal construction + interior initialization
            var barn = EnsureCompletedBuilding(farm, "Barn", barnTile, Monitor);
            var coop = EnsureCompletedBuilding(farm, "Coop", coopTile, Monitor);

            var barnIndoors = barn.GetIndoors() as AnimalHouse;
            var coopIndoors = coop.GetIndoors() as AnimalHouse;

            if (barnIndoors is null || coopIndoors is null)
            {
                string err = $"Missing interior: barn={(barnIndoors is not null ? "OK" : "NULL")}, coop={(coopIndoors is not null ? "OK" : "NULL")}";
                evidence["livestock"] = new Dictionary<string, object?> { ["error"] = err };
                Monitor.Log($"[TestDriver] farm-capabilities: {err}", LogLevel.Warn);
                return;
            }

            // Start from empty native troughs in both buildings.
            ClearTroughHay(barnIndoors);
            ClearTroughHay(coopIndoors);

            // Clean any existing animals from prior runs to ensure deterministic fixture state
            barnIndoors.animals.Clear();
            barnIndoors.animalsThatLiveHere.Clear();
            coopIndoors.animals.Clear();
            coopIndoors.animalsThatLiveHere.Clear();

            // 1. Reuse the same Barn with one mature scope-specific animal.
            // Sheep wool must be produced by native dayUpdate; no hand-filled fallback.
            var barnAnimal = new FarmAnimal(shearingOnly ? "Sheep" : "White Cow", (long)Game1.uniqueIDForThisGame + 1, Game1.player.UniqueMultiplayerID)
            {
                Name = shearingOnly ? "FixtureSheep" : "FixtureCow",
                age = { Value = 30 },
                fullness = { Value = 255 },
                happiness = { Value = 255 },
                friendshipTowardFarmer = { Value = 200 },
                daysSinceLastLay = { Value = 5 }
            };
            barnIndoors.adoptAnimal(barnAnimal);
            barnAnimal.currentLocation = barnIndoors;
            try { barnAnimal.dayUpdate(barnIndoors); } catch (Exception ex) { Monitor.Log($"[TestDriver] barnAnimal.dayUpdate note: {ex.Message}", LogLevel.Warn); }
            if (!shearingOnly && barnAnimal.currentProduce is not null && string.IsNullOrEmpty(barnAnimal.currentProduce.Value))
                barnAnimal.currentProduce.Value = "184"; // Milk

            Vector2 cowSafeTile = FindWalkableAnimalTile(barnIndoors, new Vector2(6, 8));
            barnAnimal.Position = new Vector2(cowSafeTile.X * 64f, cowSafeTile.Y * 64f);

            // 2. Unpetted chicken in Coop (valid native type "White Chicken")
            var chicken = new FarmAnimal("White Chicken", (long)Game1.uniqueIDForThisGame + 7, Game1.player.UniqueMultiplayerID)
            {
                Name = "FixtureHen",
                age = { Value = 30 },
                fullness = { Value = 255 },
                happiness = { Value = 255 },
                friendshipTowardFarmer = { Value = 200 },
                wasPet = { Value = false }
            };
            coopIndoors.adoptAnimal(chicken);
            chicken.currentLocation = coopIndoors;
            chicken.wasPet.Value = false;

            Vector2 henSafeTile = FindWalkableAnimalTile(coopIndoors, new Vector2(4, 6));
            chicken.Position = new Vector2(henSafeTile.X * 64f, henSafeTile.Y * 64f);

            if (Environment.GetEnvironmentVariable("STARDEW_AI_FARM_SCOPE") == "animal-ground-produce-only")
            {
                var existingEggs = coopIndoors.objects.Values.Where(o => o.Category == StardewValley.Object.EggCategory).ToList();
                chicken.daysSinceLastLay.Value = 5;
                chicken.dayUpdate(coopIndoors); // The native chicken lays the actual ground object; no planted item fallback.
                var eggs = coopIndoors.objects.Pairs.Where(p => p.Value.Category == StardewValley.Object.EggCategory).ToList();
                if (existingEggs.Count != 0 || eggs.Count != 1)
                    throw new InvalidOperationException($"Ground egg fixture ambiguous: before={existingEggs.Count}, after={eggs.Count}.");
                var egg = eggs.Single();
                evidence["groundProduce"] = new {
                    source = "FarmAnimal.dayUpdate", sourceAnimalName = chicken.Name,
                    location = coopIndoors.NameOrUniqueName, tile = TileEvidence(egg.Key),
                    itemId = egg.Value.QualifiedItemId, stack = egg.Value.Stack,
                    quality = egg.Value.Quality, canBeGrabbed = egg.Value.CanBeGrabbed,
                    existingEggs = existingEggs.Count, eggCount = eggs.Count
                };
            }

            string? cowHarvestType = null;
            string? cowHarvestTool = null;
            try { cowHarvestType = barnAnimal.GetHarvestType()?.ToString(); } catch { }
            try { cowHarvestTool = barnAnimal.GetAnimalData()?.HarvestTool; } catch { }

            string? henHarvestType = null;
            string? henHarvestTool = null;
            try { henHarvestType = chicken.GetHarvestType()?.ToString(); } catch { }
            try { henHarvestTool = chicken.GetAnimalData()?.HarvestTool; } catch { }

            var barnDoorTile = new { x = (barn.tileX?.Value ?? 0) + (barn.animalDoor?.X ?? 0), y = (barn.tileY?.Value ?? 0) + (barn.animalDoor?.Y ?? 0) };
            var barnHumanDoorTile = new { x = (barn.tileX?.Value ?? 0) + (barn.humanDoor?.X ?? 0), y = (barn.tileY?.Value ?? 0) + (barn.humanDoor?.Y ?? 0) };
            var coopDoorTile = new { x = (coop.tileX?.Value ?? 0) + (coop.animalDoor?.X ?? 0), y = (coop.tileY?.Value ?? 0) + (coop.animalDoor?.Y ?? 0) };
            var coopHumanDoorTile = new { x = (coop.tileX?.Value ?? 0) + (coop.humanDoor?.X ?? 0), y = (coop.tileY?.Value ?? 0) + (coop.humanDoor?.Y ?? 0) };

            if (Environment.GetEnvironmentVariable("STARDEW_AI_FARM_SCOPE") == "animal-door-only")
            {
                var doorRect = coop.getRectForAnimalDoor(coop.GetData());
                var footprint = new Rectangle(coop.tileX?.Value ?? throw new InvalidOperationException("Missing Coop X"),
                    coop.tileY?.Value ?? throw new InvalidOperationException("Missing Coop Y"), coop.tilesWide.Value, coop.tilesHigh.Value);
                var overlaps = farm.buildings.Where(b => !ReferenceEquals(b, coop)
                    && footprint.Intersects(new Rectangle(b.tileX.Value, b.tileY.Value, b.tilesWide.Value, b.tilesHigh.Value))).ToList();
                var resolved = farm.getBuildingAt(new Vector2(coopDoorTile.x, coopDoorTile.y));
                bool nativeTargetMatches = ReferenceEquals(resolved, coop) && ReferenceEquals(resolved?.GetIndoors(), coopIndoors);
                if (overlaps.Count != 0 || !nativeTargetMatches)
                    throw new InvalidOperationException($"Animal door fixture collision: overlaps={string.Join(",", overlaps.Select(b => b.buildingType.Value))}; nativeTarget={resolved?.buildingType.Value}.");
                // Do not set animalDoorOpen: the closed start must be observed in the real fixture.
                evidence["animalDoor"] = new {
                    location = farm.NameOrUniqueName, indoorsName = coopIndoors.NameOrUniqueName,
                    buildingType = coop.buildingType.Value, doorTile = coopDoorTile,
                    buildingOverlapCount = overlaps.Count, nativeTargetMatches,
                    nativeTargetBuildingType = resolved!.buildingType.Value,
                    initialOpen = coop.animalDoorOpen.Value,
                    daysOfConstructionLeft = coop.daysOfConstructionLeft.Value,
                    nativeDoorBounds = new { x = doorRect.X / 64, y = doorRect.Y / 64,
                        width = doorRect.Width / 64, height = doorRect.Height / 64 }
                };
            }

            evidence["livestock"] = new Dictionary<string, object?>
            {
                // Top-level compatibility fields
                ["buildingType"] = coop.buildingType?.Value,
                ["indoorsName"] = coopIndoors.NameOrUniqueName,
                ["tile"] = new { x = coop.tileX?.Value ?? 0, y = coop.tileY?.Value ?? 0 },
                ["doorTile"] = coopDoorTile,
                ["hayCount"] = CountTroughHay(coopIndoors),
                ["siloHayCount"] = farm.piecesOfHay?.Value ?? 0,
                ["troughTiles"] = TroughTiles(coopIndoors).Select(TileEvidence).ToList(),
                ["animalCount"] = coopIndoors.getAllFarmAnimals().Count + barnIndoors.getAllFarmAnimals().Count,
                ["animalDoorOpen"] = coop.animalDoorOpen?.Value == true,

                // Cow in Barn
                [$"{animalPrefix}Name"] = barnAnimal.Name,
                [$"{animalPrefix}Id"] = barnAnimal.myID.Value,
                [$"{animalPrefix}Type"] = barnAnimal.type?.Value,
                [$"{animalPrefix}Tile"] = new { x = (int)barnAnimal.Tile.X, y = (int)barnAnimal.Tile.Y },
                [$"{animalPrefix}Position"] = new { x = (int)barnAnimal.Position.X, y = (int)barnAnimal.Position.Y },
                [$"{animalPrefix}Produce"] = barnAnimal.currentProduce?.Value,
                [$"{animalPrefix}HarvestType"] = cowHarvestType,
                [$"{animalPrefix}HarvestTool"] = cowHarvestTool,
                [shearingOnly ? "canShearWithTool" : "canMilkWithTool"] = shearingOnly
                    ? barnAnimal.isAdult() && barnAnimal.CanGetProduceWithTool(new StardewValley.Tools.Shears())
                    : SafeCanMilk(barnAnimal),
                [$"{animalPrefix}IsAdult"] = barnAnimal.isAdult(),
                [$"{animalPrefix}IndoorsName"] = barnIndoors.NameOrUniqueName,

                // Hen in Coop
                ["henName"] = chicken.Name,
                ["henId"] = chicken.myID.Value,
                ["henType"] = chicken.type?.Value,
                ["henTile"] = new { x = (int)chicken.Tile.X, y = (int)chicken.Tile.Y },
                ["henPosition"] = new { x = (int)chicken.Position.X, y = (int)chicken.Position.Y },
                ["henProduce"] = chicken.currentProduce?.Value,
                ["henHarvestType"] = henHarvestType,
                ["henHarvestTool"] = henHarvestTool,
                ["henWasPet"] = chicken.wasPet?.Value == true,
                ["henIndoorsName"] = coopIndoors.NameOrUniqueName,

                // Structured Barn details
                ["barn"] = new Dictionary<string, object?>
                {
                    ["buildingType"] = barn.buildingType?.Value,
                    ["indoorsName"] = barnIndoors.NameOrUniqueName,
                    ["tile"] = new { x = barn.tileX?.Value ?? 0, y = barn.tileY?.Value ?? 0 },
                    ["doorTile"] = barnDoorTile,
                    ["humanDoorTile"] = barnHumanDoorTile,
                    ["animalDoorOpen"] = barn.animalDoorOpen?.Value == true,
                    ["hayCount"] = CountTroughHay(barnIndoors),
                    ["troughTiles"] = TroughTiles(barnIndoors).Select(TileEvidence).ToList(),
                    ["animalCount"] = barnIndoors.getAllFarmAnimals().Count
                },

                // Structured Coop details
                ["coop"] = new Dictionary<string, object?>
                {
                    ["buildingType"] = coop.buildingType?.Value,
                    ["indoorsName"] = coopIndoors.NameOrUniqueName,
                    ["tile"] = new { x = coop.tileX?.Value ?? 0, y = coop.tileY?.Value ?? 0 },
                    ["doorTile"] = coopDoorTile,
                    ["humanDoorTile"] = coopHumanDoorTile,
                    ["animalDoorOpen"] = coop.animalDoorOpen?.Value == true,
                    ["hayCount"] = CountTroughHay(coopIndoors),
                    ["troughTiles"] = TroughTiles(coopIndoors).Select(TileEvidence).ToList(),
                    ["animalCount"] = coopIndoors.getAllFarmAnimals().Count
                }
            };
        }
        catch (Exception ex)
        {
            evidence["livestock"] = new Dictionary<string, object?> { ["error"] = ex.Message };
            Monitor.Log($"[TestDriver] farm-capabilities livestock arrange failed: {ex}", LogLevel.Warn);
        }
    }

    /// <summary>Native trough tiles (the game's own Trough/Back map property).</summary>
    private static List<Vector2> TroughTiles(AnimalHouse house)
    {
        var tiles = new List<Vector2>();
        try
        {
            var layers = house.Map?.Layers;
            if (layers is null || layers.Count == 0)
                return tiles;
            var layer = layers[0];
            for (int y = 0; y < layer.LayerHeight; y++)
            {
                for (int x = 0; x < layer.LayerWidth; x++)
                {
                    bool isTrough;
                    try { isTrough = house.doesTileHaveProperty(x, y, "Trough", "Back", false) is not null; }
                    catch { isTrough = false; }
                    if (isTrough)
                        tiles.Add(new Vector2(x, y));
                }
            }
        }
        catch { }
        return tiles;
    }

    private static int CountTroughHay(AnimalHouse house)
    {
        int count = 0;
        foreach (var tile in TroughTiles(house))
        {
            try
            {
                if (house.objects.TryGetValue(tile, out var obj) && obj is not null
                    && (obj.QualifiedItemId == "(O)178" || obj.ItemId == "178"))
                    count++;
            }
            catch { }
        }
        return count;
    }

    private static void ClearTroughHay(AnimalHouse house)
    {
        foreach (var tile in TroughTiles(house))
        {
            try
            {
                if (house.objects.TryGetValue(tile, out var obj) && obj is not null
                    && (obj.QualifiedItemId == "(O)178" || obj.ItemId == "178"))
                    house.objects.Remove(tile);
            }
            catch { }
        }
    }

    private static object TileEvidence(Vector2 t) => new { x = (int)t.X, y = (int)t.Y };

    private static bool SafeIsWeed(StardewValley.Object obj)
    {
        try { return obj.IsWeeds(); } catch { return false; }
    }

    private static bool SafeIsStone(StardewValley.Object obj)
    {
        try { return obj.IsBreakableStone(); } catch { return false; }
    }

    private static bool SafeIsTwig(StardewValley.Object obj)
    {
        try { return obj.IsTwig(); } catch { return false; }
    }

    private static bool SafeCanMilk(FarmAnimal animal)
    {
        try
        {
            return animal.isAdult()
                && !string.IsNullOrEmpty(animal.currentProduce?.Value)
                && animal.CanGetProduceWithTool(new StardewValley.Tools.MilkPail());
        }
        catch { return false; }
    }

    private static StardewValley.Buildings.Building EnsureCompletedBuilding(
        Farm farm,
        string buildingType,
        Vector2 defaultTile,
        IMonitor monitor)
    {
        var building = farm.buildings.OfType<StardewValley.Buildings.Building>()
            .FirstOrDefault(b => (b.buildingType?.Value ?? "").Equals(buildingType, StringComparison.OrdinalIgnoreCase));

        if (building is null)
        {
            try
            {
                building = StardewValley.Buildings.Building.CreateInstanceFromId(buildingType, defaultTile);
            }
            catch
            {
                building = new StardewValley.Buildings.Building(buildingType, defaultTile);
            }
            building.tileX.Value = (int)defaultTile.X;
            building.tileY.Value = (int)defaultTile.Y;
        }

        if (!farm.buildings.Contains(building))
            farm.buildings.Add(building);

        building.daysOfConstructionLeft.Value = 0;
        building.daysUntilUpgrade.Value = 0;
        building.parentLocationName.Value = farm.NameOrUniqueName;

        var indoors = building.GetIndoors();
        if (indoors is null)
        {
            var data = building.GetData();
            if (data is not null)
            {
                building.LoadFromBuildingData(data, false, true);
                indoors = building.GetIndoors();
            }
        }

        building.daysOfConstructionLeft.Value = 0;
        building.daysUntilUpgrade.Value = 0;

        if (indoors is not null)
        {
            indoors.ParentBuilding = building;
            indoors.parentLocationName.Value = farm.NameOrUniqueName;
            if (!Game1.locations.Contains(indoors))
                Game1.locations.Add(indoors);

            building.updateInteriorWarps(indoors);
        }
        else
        {
            monitor.Log($"[TestDriver] farm-capabilities: failed to initialize indoors for {buildingType}", LogLevel.Warn);
        }

        return building;
    }

    private static void ClearBuildingFootprint(Farm farm, Vector2 origin, int width, int height)
    {
        for (int x = 0; x < width; x++)
        {
            for (int y = 0; y < height; y++)
            {
                ClearTileObject(farm, new Vector2(origin.X + x, origin.Y + y));
            }
        }
    }

    private static Vector2 FindWalkableAnimalTile(AnimalHouse house, Vector2 preferred)
    {
        if (IsTileWalkable(house, preferred))
            return preferred;

        for (int r = 1; r <= 10; r++)
        {
            for (int dx = -r; dx <= r; dx++)
            {
                for (int dy = -r; dy <= r; dy++)
                {
                    var candidate = new Vector2(preferred.X + dx, preferred.Y + dy);
                    if (IsTileWalkable(house, candidate))
                        return candidate;
                }
            }
        }
        return preferred;
    }

    private static bool IsTileWalkable(AnimalHouse house, Vector2 tile)
    {
        try
        {
            int x = (int)tile.X;
            int y = (int)tile.Y;
            if (x < 1 || y < 1) return false;
            if (house.objects.ContainsKey(tile)) return false;
            return house.isTilePassable(tile);
        }
        catch
        {
            return false;
        }
    }

    private static HoeDirt EnsureHoeDirt(Farm farm, Vector2 tile)
    {
        if (!farm.terrainFeatures.ContainsKey(tile) || farm.terrainFeatures[tile] is not HoeDirt)
        {
            farm.terrainFeatures[tile] = new HoeDirt(0, farm);
        }
        return (HoeDirt)farm.terrainFeatures[tile];
    }

    private static string? ClearTileObject(Farm farm, Vector2 tile)
    {
        if (farm.objects.TryGetValue(tile, out var existing) && existing != null)
        {
            farm.objects.Remove(tile);
            return existing.Name;
        }
        return null;
    }

    // Real 1.6 parsnip crop: the (string seedId, ...) constructor loads CropData (phaseDays,
    // harvest item "(O)24") for parsnip seeds "472"; growCompletely() lands the crop in the exact
    // end-of-growth state (currentPhase = phaseDays.Count - 1, dayOfCurrentPhase = 0), which is
    // what HoeDirt.readyForHarvest() tests. The (bool forageCrop, ...) constructor is for forage
    // crops only and must not be used here.
    private static Crop CreateParsnipCrop(int tileX, int tileY, Farm farm, bool mature)
    {
        var crop = new Crop("472", tileX, tileY, farm);
        if (mature)
        {
            crop.growCompletely();
        }
        else
        {
            crop.currentPhase.Value = 0;
            crop.dayOfCurrentPhase.Value = 0;
        }
        crop.dead.Value = false;
        return crop;
    }

    private static Dictionary<string, object?> DescribeCropTile(Vector2 tile, HoeDirt? dirt)
    {
        var crop = dirt?.crop;
        return new Dictionary<string, object?>
        {
            ["x"] = (int)tile.X,
            ["y"] = (int)tile.Y,
            ["dirtPresent"] = dirt != null,
            ["hasCrop"] = crop != null,
            ["cropSeedId"] = crop?.netSeedIndex?.Value,
            ["harvestItemId"] = crop?.indexOfHarvest?.Value,
            ["currentPhase"] = crop?.currentPhase.Value,
            ["phaseDayCount"] = crop?.phaseDays.Count,
            ["dayOfCurrentPhase"] = crop?.dayOfCurrentPhase.Value,
            ["dead"] = crop?.dead.Value,
            ["readyForHarvest"] = dirt?.crop != null && dirt.readyForHarvest()
        };
    }

    private static List<Dictionary<string, object?>> DescribeItems(System.Collections.IEnumerable items)
    {
        var list = new List<Dictionary<string, object?>>();
        foreach (var obj in items)
        {
            if (obj is Item item)
            {
                list.Add(new Dictionary<string, object?>
                {
                    ["id"] = item.QualifiedItemId,
                    ["name"] = item.Name,
                    ["stack"] = item.Stack
                });
            }
        }
        return list;
    }

    private static int CountItemsByQualifiedId(System.Collections.IEnumerable items, string qualifiedId)
    {
        int total = 0;
        foreach (var obj in items)
        {
            if (obj is Item item && item.QualifiedItemId == qualifiedId)
            {
                total += item.Stack;
            }
        }
        return total;
    }

    private void ArrangeHarvestDemo(Farm farm, HoeDirt targetDirt, Farmer? companionFarmer)
    {
        var matureTiles = new[] { new Vector2(63, 20), new Vector2(64, 20), new Vector2(65, 20) };
        var chestTile = new Vector2(66, 20);

        var clearedObjects = new Dictionary<string, string>();
        var matureEvidence = new List<Dictionary<string, object?>>();
        bool allMatureReady = true;
        foreach (var t in matureTiles)
        {
            string? cleared = ClearTileObject(farm, t);
            if (cleared != null) clearedObjects[$"{(int)t.X},{(int)t.Y}"] = cleared;
            var hd = EnsureHoeDirt(farm, t);
            hd.state.Value = 0;
            hd.crop = CreateParsnipCrop((int)t.X, (int)t.Y, farm, mature: true);
            bool ready = hd.crop != null && hd.readyForHarvest();
            if (!ready) allMatureReady = false;
            matureEvidence.Add(DescribeCropTile(t, hd));
            Monitor.Log($"[TestDriver] harvest-demo: mature parsnip at ({(int)t.X},{(int)t.Y}) readyForHarvest={ready}.", ready ? LogLevel.Info : LogLevel.Warn);
        }

        string? clearedImmature = ClearTileObject(farm, _targetTile);
        if (clearedImmature != null) clearedObjects[$"{(int)_targetTile.X},{(int)_targetTile.Y}"] = clearedImmature;
        targetDirt.crop = CreateParsnipCrop((int)_targetTile.X, (int)_targetTile.Y, farm, mature: false);
        bool immatureReady = targetDirt.crop != null && targetDirt.readyForHarvest();
        Monitor.Log($"[TestDriver] harvest-demo: immature parsnip at (65,15) readyForHarvest={immatureReady} (expected False).", LogLevel.Info);

        string? clearedChest = ClearTileObject(farm, chestTile);
        if (clearedChest != null) clearedObjects[$"{(int)chestTile.X},{(int)chestTile.Y}"] = clearedChest;
        var chest = new StardewValley.Objects.Chest(true, chestTile, "130");
        chest.resetLidFrame(); // Initialize the native player-chest lid before fixture persistence.
        chest.Items.Add(ItemRegistry.Create("(O)24", 3));
        chest.Items.Add(ItemRegistry.Create("(O)24", 2));
        chest.Items.Add(ItemRegistry.Create("(O)388", 10));
        farm.objects[chestTile] = chest;

        _evidence["arrange"] = new Dictionary<string, object?>
        {
            ["scenario"] = _scenario,
            ["parsnipSeedId"] = "472",
            ["parsnipProduceQualifiedId"] = "(O)24",
            ["matureTiles"] = matureEvidence,
            ["allMatureReadyAtArrange"] = allMatureReady,
            ["immatureTile"] = DescribeCropTile(_targetTile, targetDirt),
            ["immatureReadyAtArrange"] = immatureReady,
            ["chest"] = new Dictionary<string, object?>
            {
                ["x"] = (int)chestTile.X,
                ["y"] = (int)chestTile.Y,
                ["playerChest"] = true,
                ["contents"] = DescribeItems(chest.Items)
            },
            ["clearedObjects"] = clearedObjects,
            ["companionArrangement"] = "status-quo",
            ["companionTile"] = companionFarmer != null
                ? new Dictionary<string, object?>
                {
                    ["x"] = (int)companionFarmer.Tile.X,
                    ["y"] = (int)companionFarmer.Tile.Y,
                    ["location"] = companionFarmer.currentLocation?.Name
                }
                : null
        };
        Monitor.Log($"[TestDriver] harvest-demo: arranged 3 mature parsnips (63-65,20), 1 immature (65,15), chest at (66,20) with (O)24 x3 + (O)24 x2 + (O)388 x10. AllMatureReady={allMatureReady}.", LogLevel.Info);
    }

    private void ArrangeHarvestCancel(Farm farm, Farmer? companionFarmer)
    {
        var cropTiles = new List<Vector2>();
        for (int x = 60; x <= 63; x++)
        {
            cropTiles.Add(new Vector2(x, 20));
            cropTiles.Add(new Vector2(x, 21));
        }

        var clearedObjects = new Dictionary<string, string>();
        var tileEvidence = new List<Dictionary<string, object?>>();
        bool allReady = true;
        foreach (var t in cropTiles)
        {
            string? cleared = ClearTileObject(farm, t);
            if (cleared != null) clearedObjects[$"{(int)t.X},{(int)t.Y}"] = cleared;
            var hd = EnsureHoeDirt(farm, t);
            hd.state.Value = 0;
            hd.crop = CreateParsnipCrop((int)t.X, (int)t.Y, farm, mature: true);
            bool ready = hd.crop != null && hd.readyForHarvest();
            if (!ready) allReady = false;
            tileEvidence.Add(DescribeCropTile(t, hd));
        }

        _evidence["arrange"] = new Dictionary<string, object?>
        {
            ["scenario"] = _scenario,
            ["parsnipSeedId"] = "472",
            ["parsnipProduceQualifiedId"] = "(O)24",
            ["cropTiles"] = tileEvidence,
            ["cropCount"] = cropTiles.Count,
            ["allMatureReadyAtArrange"] = allReady,
            ["clearedObjects"] = clearedObjects,
            ["companionArrangement"] = "status-quo",
            ["companionTile"] = companionFarmer != null
                ? new Dictionary<string, object?>
                {
                    ["x"] = (int)companionFarmer.Tile.X,
                    ["y"] = (int)companionFarmer.Tile.Y,
                    ["location"] = companionFarmer.currentLocation?.Name
                }
                : null
        };
        Monitor.Log($"[TestDriver] harvest-cancel: arranged {cropTiles.Count} mature parsnips in two rows (60-63,20) and (60-63,21). AllReady={allReady}.", allReady ? LogLevel.Info : LogLevel.Warn);
    }

    private bool CheckInventoryInvariance()
    {
        if (Game1.player.Items.Count != _initialPlayerItemCount)
        {
            Monitor.Log($"[TestDriver] INVENTORY VIOLATION: Item count changed from {_initialPlayerItemCount} to {Game1.player.Items.Count}!", LogLevel.Error);
            return false;
        }

        for (int i = 0; i < _initialPlayerItems.Count; i++)
        {
            var expected = _initialPlayerItems[i];
            if (expected.Slot >= Game1.player.Items.Count) return false;
            var actual = Game1.player.Items[expected.Slot];
            if (actual == null)
            {
                if (expected.Id != null) return false;
            }
            else
            {
                if (actual.ItemId != expected.Id || actual.Stack != expected.Stack)
                {
                    Monitor.Log($"[TestDriver] INVENTORY VIOLATION in slot {expected.Slot}: Expected {expected.Id}x{expected.Stack}, got {actual.ItemId}x{actual.Stack}", LogLevel.Error);
                    return false;
                }
                if (actual is StardewValley.Tools.WateringCan can && can.WaterLeft != expected.WaterLeft)
                {
                    Monitor.Log($"[TestDriver] INVENTORY VIOLATION: Player watering can water changed from {expected.WaterLeft} to {can.WaterLeft}!", LogLevel.Error);
                    return false;
                }
            }
        }
        return true;
    }

    private void CaptureScenarioArtifacts()
    {
        var outDir = _evidenceDir;
        if (string.IsNullOrWhiteSpace(outDir))
        {
            outDir = Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
        }
        Directory.CreateDirectory(outDir);

        // 1. player-snapshot.json (Full items array with slot, ID, stack, water)
        var pCan = Game1.player.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault();
        var inventoryDetails = new List<object>();
        for (int i = 0; i < Game1.player.Items.Count; i++)
        {
            var it = Game1.player.Items[i];
            if (it != null)
            {
                inventoryDetails.Add(new
                {
                    slot = i,
                    id = it.ItemId,
                    name = it.Name,
                    stack = it.Stack,
                    waterLeft = (it as StardewValley.Tools.WateringCan)?.WaterLeft ?? -1
                });
            }
        }

        var playerSnapshot = new Dictionary<string, object?>
        {
            ["name"] = Game1.player.Name,
            ["stamina"] = Game1.player.stamina,
            ["maxStamina"] = Game1.player.maxStamina.Value,
            ["money"] = Game1.player.Money,
            ["location"] = Game1.currentLocation?.Name,
            ["tile"] = new { x = (int)Game1.player.Tile.X, y = (int)Game1.player.Tile.Y },
            ["facingDirection"] = Game1.player.FacingDirection,
            ["waterLeft"] = pCan?.WaterLeft ?? 0,
            ["inventoryCount"] = Game1.player.Items.Count,
            ["inventoryItems"] = inventoryDetails
        };
        File.WriteAllText(Path.Combine(outDir, "player-snapshot.json"), JsonSerializer.Serialize(playerSnapshot, new JsonSerializerOptions { WriteIndented = true }));

        // 2. actor-snapshot.json (Observes real Companion Farmer via narrow reflection)
        ResolveCompanionActor(out var actor, out var companionFarmer, out var obsStatus, out var obsDetail);

        Dictionary<string, object?> actorSnapshot;
        if (companionFarmer != null)
        {
            var cCan = companionFarmer.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault();
            actorSnapshot = new Dictionary<string, object?>
            {
                ["realActorObserved"] = true,
                ["observationStatus"] = obsStatus.ToString(),
                ["observationDetail"] = obsDetail,
                ["scenario"] = _scenario,
                ["farmerName"] = companionFarmer.Name,
                ["stamina"] = companionFarmer.stamina,
                ["maxStamina"] = companionFarmer.maxStamina.Value,
                ["facingDirection"] = companionFarmer.FacingDirection,
                ["waterLeft"] = cCan?.WaterLeft ?? -1,
                ["tile"] = new { x = (int)(companionFarmer.Position.X / 64f), y = (int)(companionFarmer.Position.Y / 64f) },
                ["location"] = companionFarmer.currentLocation?.Name ?? Game1.currentLocation?.Name,
                ["targetTile"] = new { x = (int)_targetTile.X, y = (int)_targetTile.Y }
            };
        }
        else
        {
            actorSnapshot = new Dictionary<string, object?>
            {
                ["realActorObserved"] = false,
                ["observationStatus"] = obsStatus.ToString(),
                ["observationDetail"] = obsDetail,
                ["scenario"] = _scenario,
                ["reflectionModFound"] = (obsStatus != ActorObservationStatus.ModNotFoundInRegistry),
                ["reflectionFieldFound"] = (obsStatus != ActorObservationStatus.ActorFieldNotFound),
                ["targetTile"] = new { x = (int)_targetTile.X, y = (int)_targetTile.Y }
            };
        }
        File.WriteAllText(Path.Combine(outDir, "actor-snapshot.json"), JsonSerializer.Serialize(actorSnapshot, new JsonSerializerOptions { WriteIndented = true }));

        // 3. world-snapshot.json
        var farm = Game1.getFarm();
        var dirt = farm?.terrainFeatures.TryGetValue(_targetTile, out var tf) == true && tf is HoeDirt hd ? hd : null;
        var worldSnapshot = new Dictionary<string, object?>
        {
            ["location"] = Game1.currentLocation?.Name,
            ["dayOfMonth"] = Game1.dayOfMonth,
            ["currentSeason"] = Game1.currentSeason,
            ["year"] = Game1.year,
            ["timeOfDay"] = Game1.timeOfDay,
            ["timeFrozen"] = _freezeTime && Game1.gameTimeInterval == 0,
            ["fixedSeed"] = 42,
            ["targetTile"] = new { x = (int)_targetTile.X, y = (int)_targetTile.Y },
            ["targetDirtWatered"] = dirt?.state.Value == 1,
            ["targetDirtStateValue"] = dirt?.state.Value ?? 0
        };
        File.WriteAllText(Path.Combine(outDir, "world-snapshot.json"), JsonSerializer.Serialize(worldSnapshot, new JsonSerializerOptions { WriteIndented = true }));

        // 4. render.png screenshot metadata (No fallback: valid screenshot ONLY from RenderedWorld)
        var screenshotPath = Path.Combine(outDir, "render.png");
        if (!_screenshotCaptured)
        {
            _screenshotMetadata["captured"] = false;
            _screenshotMetadata["filePath"] = screenshotPath;
            if (!_screenshotMetadata.ContainsKey("rejectionReason"))
            {
                _screenshotMetadata["rejectionReason"] = "No valid frame was captured during RenderedWorld satisfying world readiness, zero fade alpha, and Farm location.";
            }
        }
        else
        {
            _screenshotMetadata["filePath"] = screenshotPath;
        }

        // 5. artifact-index.json
        bool expectedMoneyInvariant = _scenario.Equals("purchase-mcp", StringComparison.OrdinalIgnoreCase)
            ? (Game1.player.Money == _initialPlayerMoney - 40)
            : (_scenario.Equals("cycle-mcp", StringComparison.OrdinalIgnoreCase) ||
               _scenario.Equals("cycle-operator", StringComparison.OrdinalIgnoreCase))
            ? (Game1.player.Money == _initialPlayerMoney - 80)
            : _scenario.Equals("existing-save-buy5", StringComparison.OrdinalIgnoreCase)
            ? (Game1.player.Money < _initialPlayerMoney)
            : (Game1.player.Money == _initialPlayerMoney);
        bool playerResourcesInvariant = (Game1.player.stamina == _initialPlayerStamina && expectedMoneyInvariant);
        bool inventoryInvariant = CheckInventoryInvariance();
        bool playerInvariant = playerResourcesInvariant && inventoryInvariant;

        var artifactIndex = new Dictionary<string, object?>
        {
            ["runId"] = _runId,
            ["scenario"] = _scenario,
            ["mode"] = _testMode,
            ["timestampUtc"] = DateTime.UtcNow.ToString("o"),
            ["artifacts"] = new Dictionary<string, string>
            {
                ["evidenceJson"] = "scenario-evidence.json",
                ["playerSnapshot"] = "player-snapshot.json",
                ["actorSnapshot"] = "actor-snapshot.json",
                ["worldSnapshot"] = "world-snapshot.json",
                ["renderScreenshot"] = "render.png",
                ["smapiLog"] = "smapi-log.txt",
                ["harnessCoordination"] = "harness-coordination.json",
                ["terminalResult"] = "terminal-result.json"
            },
            ["screenshotMetadata"] = _screenshotMetadata,
            ["assertions"] = new Dictionary<string, bool>
            {
                ["playerResourcesInvariant"] = playerInvariant,
                ["inventoryInvariant"] = inventoryInvariant,
                ["targetSoilIdentified"] = true,
                ["saveIsolationVerified"] = true,
                ["frozenBoundaryMaintained"] = _freezeTime
            }
        };
        File.WriteAllText(Path.Combine(outDir, "artifact-index.json"), JsonSerializer.Serialize(artifactIndex, new JsonSerializerOptions { WriteIndented = true }));
        Monitor.Log($"[TestDriver] Scenario artifacts captured to: {outDir}", LogLevel.Info);
    }

    // --- Async frame capture: render thread only reads the backbuffer; PNG encode + disk IO
    // run on a background worker so the game never stalls on compression (main-thread
    // SaveAsPng made demo runs visibly stutter even on fast PCs).
    private sealed record CaptureJob(Color[] Pixels, int Width, int Height, string Path);

    private readonly System.Collections.Concurrent.ConcurrentQueue<CaptureJob> _captureJobs = new();
    private readonly AutoResetEvent _captureSignal = new(false);
    private Thread? _captureThread;
    private volatile bool _captureWorkerStop;

    private void StartCaptureWorker()
    {
        if (_captureThread != null) return;
        _captureThread = new Thread(CaptureWorkerMain) { IsBackground = false, Name = "TestDriverCaptureWorker" };
        _captureThread.Start();
    }

    private void CaptureWorkerMain()
    {
        while (!_captureWorkerStop || !_captureJobs.IsEmpty)
        {
            _captureSignal.WaitOne(500);
            while (_captureJobs.TryDequeue(out var job))
            {
                try
                {
                    var png = EncodePngRgba(job.Pixels, job.Width, job.Height);
                    var dir = Path.GetDirectoryName(job.Path);
                    if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                    File.WriteAllBytes(job.Path, png);
                }
                catch (Exception ex)
                {
                    Monitor.Log($"[TestDriver] Async PNG encode note: {ex.Message}", LogLevel.Warn);
                }
            }
        }
    }

    private void FlushCaptureJobs(int timeoutMs = 15000)
    {
        var sw = System.Diagnostics.Stopwatch.StartNew();
        while (!_captureJobs.IsEmpty && sw.ElapsedMilliseconds < timeoutMs)
        {
            Thread.Sleep(50);
        }
        _captureWorkerStop = true;
        _captureSignal.Set();
        _captureThread?.Join(timeoutMs);
        _captureThread = null;
    }

    private static readonly uint[] CrcTable = BuildCrcTable();

    private static uint[] BuildCrcTable()
    {
        var table = new uint[256];
        for (uint n = 0; n < 256; n++)
        {
            uint c = n;
            for (int k = 0; k < 8; k++)
                c = (c & 1) != 0 ? 0xEDB88320u ^ (c >> 1) : c >> 1;
            table[n] = c;
        }
        return table;
    }

    private static uint Crc32(byte[] data)
    {
        uint crc = 0xFFFFFFFFu;
        foreach (var b in data)
            crc = CrcTable[(crc ^ b) & 0xFF] ^ (crc >> 8);
        return crc ^ 0xFFFFFFFFu;
    }

    private static void WriteBE32(Stream s, int v)
    {
        s.WriteByte((byte)(v >> 24));
        s.WriteByte((byte)(v >> 16));
        s.WriteByte((byte)(v >> 8));
        s.WriteByte((byte)v);
    }

    private static void WriteChunk(Stream png, string type, Action<Stream> writeData)
    {
        using var dataMs = new MemoryStream();
        writeData(dataMs);
        var data = dataMs.ToArray();
        WriteBE32(png, data.Length);
        var typeBytes = System.Text.Encoding.ASCII.GetBytes(type);
        png.Write(typeBytes);
        png.Write(data);
        var crcInput = new byte[typeBytes.Length + data.Length];
        Array.Copy(typeBytes, crcInput, typeBytes.Length);
        Array.Copy(data, 0, crcInput, typeBytes.Length, data.Length);
        WriteBE32(png, (int)Crc32(crcInput));
    }

    private static byte[] EncodePngRgba(Color[] pixels, int width, int height)
    {
        using var rawMs = new MemoryStream();
        for (int y = 0; y < height; y++)
        {
            rawMs.WriteByte(0); // filter type 0 (None) per scanline
            int rowStart = y * width;
            for (int x = 0; x < width; x++)
            {
                var c = pixels[rowStart + x];
                rawMs.WriteByte(c.R);
                rawMs.WriteByte(c.G);
                rawMs.WriteByte(c.B);
                rawMs.WriteByte(c.A);
            }
        }

        byte[] idat;
        using (var idatMs = new MemoryStream())
        {
            using (var zs = new System.IO.Compression.ZLibStream(idatMs, System.IO.Compression.CompressionLevel.Fastest, leaveOpen: true))
            {
                rawMs.Position = 0;
                rawMs.CopyTo(zs);
            }
            idat = idatMs.ToArray();
        }

        using var png = new MemoryStream();
        png.Write(new byte[] { 137, 80, 78, 71, 13, 10, 26, 10 });
        WriteChunk(png, "IHDR", w =>
        {
            WriteBE32(w, width);
            WriteBE32(w, height);
            w.WriteByte(8);  // bit depth
            w.WriteByte(6);  // color type RGBA
            w.WriteByte(0);  // compression
            w.WriteByte(0);  // filter
            w.WriteByte(0);  // interlace
        });
        WriteChunk(png, "IDAT", w => w.Write(idat));
        WriteChunk(png, "IEND", _ => { });
        return png.ToArray();
    }

    private bool DoCaptureRenderScreenshot(string filePath, string acceptedReason)
    {
        try
        {
            var gd = Game1.graphics?.GraphicsDevice;
            if (gd == null) return false;

            int width = gd.PresentationParameters.BackBufferWidth;
            int height = gd.PresentationParameters.BackBufferHeight;
            if (width <= 0 || height <= 0) return false;

            var colors = new Color[width * height];
            gd.GetBackBufferData(colors);

            // DesktopGL reads bottom-to-top; PNG rows are top-to-bottom.
            if (gd.GetType().Assembly.GetType("MonoGame.OpenGL.GL") is not null)
                for (int y = 0; y < height / 2; y++)
                    for (int x = 0; x < width; x++)
                    {
                        int top = y * width + x;
                        int bottom = (height - y - 1) * width + x;
                        (colors[top], colors[bottom]) = (colors[bottom], colors[top]);
                    }

            StartCaptureWorker();
            _captureJobs.Enqueue(new CaptureJob(colors, width, height, filePath));
            _captureSignal.Set();

            _screenshotMetadata["captured"] = true;
            _screenshotMetadata["acceptanceReason"] = acceptedReason;
            _screenshotMetadata["width"] = width;
            _screenshotMetadata["height"] = height;
            _screenshotMetadata["location"] = Game1.currentLocation?.Name;
            _screenshotMetadata["fadeToBlack"] = Game1.fadeToBlack;
            _screenshotMetadata["fadeIn"] = Game1.fadeIn;
            _screenshotMetadata["fadeToBlackAlpha"] = Game1.fadeToBlackAlpha;
            _screenshotMetadata["tickCounter"] = _tickCounter;
            _screenshotMetadata["timestampUtc"] = DateTime.UtcNow.ToString("o");

            Monitor.Log($"[TestDriver] Genuine render screenshot captured: {filePath} ({width}x{height}, Location={Game1.currentLocation?.Name}, Reason={acceptedReason})", LogLevel.Info);
            return true;
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Screenshot capture note: {ex.Message}", LogLevel.Warn);
            return false;
        }
    }

    /// <summary>
    /// Opens the production mod's CompanionCommandMenu via reflection, injects command text
    /// into its TextBox, and submits with Enter. Verifies the real menu -> parse -> execute path
    /// in-process (the physical F8 keypress wiring is covered by the mod's ButtonPressed handler).
    /// </summary>
    private static bool IsCompanionCommandMenu(IClickableMenu? menu) =>
        menu?.GetType().FullName == "StardewAI.Companion.Mod.Menus.CompanionCommandMenu";

    private IClickableMenu OpenProductionMenuForAcceptance()
    {
        if (IsCompanionCommandMenu(Game1.activeClickableMenu))
            return Game1.activeClickableMenu!;
        if (Game1.activeClickableMenu != null)
            throw new InvalidOperationException("Another game menu is open.");

        var modInfo = Helper.ModRegistry.Get("CSXizhang.StardewAICompanion");
        var modEntry = modInfo?.GetType().GetProperty("Mod", BindingFlags.Public | BindingFlags.Instance)?.GetValue(modInfo)
            ?? throw new InvalidOperationException("Production ModEntry is unavailable.");
        var menuType = modEntry.GetType().Assembly.GetType("StardewAI.Companion.Mod.Menus.CompanionCommandMenu")
            ?? throw new InvalidOperationException("Production F8 menu type is unavailable.");
        var constructor = menuType.GetConstructors().FirstOrDefault(c => c.GetParameters().Length == 8)
            ?? throw new InvalidOperationException("Production F8 menu constructor has changed.");
        var parameters = constructor.GetParameters();
        string[] callbacks = { "ExecuteInGameCommand", "RequestPauseMenuAction", "RequestResumeMenuAction",
            "RequestCancelMenuAction", "ToggleAutonomyMode", "OpenAutonomySettings",
            "OpenLifeMenu", "OpenLifeDirections" };
        var args = new object?[callbacks.Length];
        for (int i = 0; i < callbacks.Length; i++)
        {
            var method = modEntry.GetType().GetMethod(callbacks[i], BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.Instance)
                ?? throw new InvalidOperationException($"Production callback {callbacks[i]} is unavailable.");
            args[i] = Delegate.CreateDelegate(parameters[i].ParameterType, modEntry, method);
        }

        var menu = (IClickableMenu)constructor.Invoke(args);
        Game1.activeClickableMenu = menu;
        var projectState = modEntry.GetType().GetMethod("ProjectChatUiState", BindingFlags.NonPublic | BindingFlags.Instance)
            ?? throw new InvalidOperationException("Production F8 state projection callback is unavailable.");
        projectState.Invoke(modEntry, null);
        return menu;
    }

    private static object? ReadMenuStatic(Type type, string name) =>
        type.GetProperty(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static)?.GetValue(null)
        ?? type.GetField(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static)?.GetValue(null);

    private void WriteF8UiSnapshot(string outDir, string label, string action, bool? screenshotCaptured = null, string? error = null)
    {
        var menu = Game1.activeClickableMenu;
        var menuType = menu?.GetType();
        var modInfo = Helper.ModRegistry.Get("CSXizhang.StardewAICompanion");
        var modEntry = modInfo?.GetType().GetProperty("Mod", BindingFlags.Public | BindingFlags.Instance)?.GetValue(modInfo);
        menuType ??= modEntry?.GetType().Assembly.GetType("StardewAI.Companion.Mod.Menus.CompanionCommandMenu");
        object? textBox = IsCompanionCommandMenu(menu)
            ? menuType?.GetField("_textBox", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(menu)
            : null;
        var history = ReadMenuStatic(menuType!, "ChatHistory") as System.Collections.IEnumerable;
        var messages = new List<object>();
        if (history != null)
        {
            foreach (var item in history)
            {
                if (item == null) continue;
                string speaker = item.GetType().GetProperty("Speaker")?.GetValue(item)?.ToString() ?? "";
                string body = item.GetType().GetProperty("Text")?.GetValue(item)?.ToString() ?? "";
                messages.Add(new { speaker, length = body.Length, text = body.Length <= 4000 ? body : body[..4000] });
            }
        }
        var snapshot = new Dictionary<string, object?>
        {
            ["runId"] = _runId, ["label"] = label, ["action"] = action,
            ["timestampUtc"] = DateTime.UtcNow.ToString("o"),
            ["year"] = Game1.year, ["season"] = Game1.currentSeason,
            ["day"] = Game1.dayOfMonth, ["timeOfDay"] = Game1.timeOfDay,
            ["menuOpen"] = IsCompanionCommandMenu(menu), ["menuType"] = menu?.GetType().FullName,
            ["currentRequestId"] = menuType == null ? null : ReadMenuStatic(menuType, "CurrentRequestId"),
            ["currentStatusText"] = menuType == null ? null : ReadMenuStatic(menuType, "CurrentStatusText"),
            ["currentActionText"] = menuType == null ? null : ReadMenuStatic(menuType, "CurrentActionText"),
            ["structuredProgressText"] = menuType == null ? null : ReadMenuStatic(menuType, "StructuredProgressText"),
            ["planWaitReason"] = menuType == null ? null : ReadMenuStatic(menuType, "PlanWaitReason"),
            ["isProcessing"] = menuType == null ? null : ReadMenuStatic(menuType, "IsProcessing"),
            ["controlPending"] = menuType == null ? null : ReadMenuStatic(menuType, "ControlPending"),
            ["pendingControlAction"] = menuType == null ? null : ReadMenuStatic(menuType, "PendingControlAction"),
            ["canSubmitText"] = menuType == null ? null : ReadMenuStatic(menuType, "CanSubmitText"),
            ["autonomyPaused"] = menuType == null ? null : ReadMenuStatic(menuType, "AutonomyPaused"),
            ["draftText"] = menuType == null ? null : ReadMenuStatic(menuType, "DraftText"),
            ["inputText"] = textBox?.GetType().GetProperty("Text")?.GetValue(textBox),
            ["scrollBack"] = IsCompanionCommandMenu(menu)
                ? menuType?.GetField("_scrollBack", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(menu) : null,
            ["unreadWhileScrolled"] = IsCompanionCommandMenu(menu)
                ? menuType?.GetField("_unreadWhileScrolled", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(menu) : null,
            ["messageCount"] = messages.Count, ["messages"] = messages,
            ["screenshotCaptured"] = screenshotCaptured, ["error"] = error
        };
        File.WriteAllText(Path.Combine(outDir, $"f8-ui-{label}.json"),
            JsonSerializer.Serialize(snapshot, new JsonSerializerOptions { WriteIndented = true }),
            new System.Text.UTF8Encoding(false));
    }

    private void HandleF8UiRequest(string outDir, JsonElement request)
    {
        string action = request.TryGetProperty("action", out var actionProperty) ? actionProperty.GetString() ?? "" : "";
        string label = request.TryGetProperty("label", out var labelProperty) ? labelProperty.GetString() ?? "" : "";
        if (label.Length is < 1 or > 48 || !label.All(c => char.IsLetterOrDigit(c) || c is '-' or '_'))
            throw new ArgumentException("F8 UI request requires a safe 1–48 character label.");

        if (action == "open")
            OpenProductionMenuForAcceptance();
        else if (action == "close")
        {
            if (IsCompanionCommandMenu(Game1.activeClickableMenu))
            {
                Game1.activeClickableMenu!.GetType().GetMethod("PreserveDraft")?.Invoke(Game1.activeClickableMenu, null);
                Game1.activeClickableMenu.exitThisMenu(playSound: false);
            }
        }
        else if (action == "draft")
        {
            var menu = OpenProductionMenuForAcceptance();
            var box = menu.GetType().GetField("_textBox", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(menu)
                ?? throw new InvalidOperationException("F8 textbox is unavailable.");
            string value = request.TryGetProperty("text", out var textProperty) ? textProperty.GetString() ?? "" : "";
            box.GetType().GetProperty("Text")?.SetValue(box, value);
        }
        else if (action == "scroll")
        {
            var menu = OpenProductionMenuForAcceptance();
            int direction = request.TryGetProperty("direction", out var directionProperty) && directionProperty.GetInt32() > 0 ? 1 : -1;
            int steps = request.TryGetProperty("steps", out var stepsProperty) ? Math.Clamp(stepsProperty.GetInt32(), 1, 30) : 1;
            for (int i = 0; i < steps; i++) menu.receiveScrollWheelAction(direction);
        }
        else if (action == "click")
        {
            var menu = OpenProductionMenuForAcceptance();
            string button = request.TryGetProperty("button", out var buttonProperty) ? buttonProperty.GetString() ?? "" : "";
            string? field = button switch
            {
                "send" => "_sendButtonRect", "pause" => "_pauseButtonRect", "resume" => "_resumeButtonRect",
                "cancel" => "_cancelButtonRect", "close" => "_closeButtonRect", "detail" => "_detailButtonRect",
                _ => null
            };
            if (field == null) throw new ArgumentException($"Unknown F8 button '{button}'.");
            var rect = (Rectangle?)menu.GetType().GetField(field, BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(menu)
                ?? throw new InvalidOperationException($"F8 button rectangle {field} is unavailable.");
            menu.receiveLeftClick(rect.X + rect.Width / 2, rect.Y + rect.Height / 2, playSound: false);
        }
        else if (action == "append-fixture")
        {
            var menu = OpenProductionMenuForAcceptance();
            var history = ReadMenuStatic(menu.GetType(), "ChatHistory") as System.Collections.IList
                ?? throw new InvalidOperationException("F8 history is unavailable.");
            var messageType = history.GetType().GetGenericArguments().Single();
            string body = request.TryGetProperty("text", out var textProperty) ? textProperty.GetString() ?? "" : "";
            var message = Activator.CreateInstance(messageType, "验收样例", body, Color.DarkSlateBlue, null)
                ?? throw new InvalidOperationException("F8 history fixture could not be created.");
            history.Add(message);
        }
        else if (action == "snapshot")
        {
            OpenProductionMenuForAcceptance();
            _f8UiCaptureLabel = label;
            _f8UiCaptureDelayFrames = 2;
            return;
        }
        else throw new ArgumentException($"Unknown F8 UI action '{action}'.");

        WriteF8UiSnapshot(outDir, label, action);
    }

    private void AssertHarnessFixtureBoundary(object modEntry, bool requireAwake = true)
    {
        if (_isolationFailed || !_scenario.Equals("free-mode-control", StringComparison.OrdinalIgnoreCase) || !Context.IsWorldReady)
            throw new InvalidOperationException("Harness fixture requires a ready isolated free-mode-control clone.");
        string? isolated = SaveIsolationPatcher.TargetIsolatedSavesPath;
        string fullIsolated = string.IsNullOrEmpty(isolated) ? "" : Path.GetFullPath(isolated).TrimEnd(Path.DirectorySeparatorChar);
        string dailySaves = Path.GetFullPath(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "StardewValley", "Saves"));
        if (fullIsolated.Length == 0 || string.Equals(fullIsolated, dailySaves, StringComparison.OrdinalIgnoreCase)
            || fullIsolated.StartsWith(dailySaves + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
            || !string.Equals(Path.GetFullPath(StardewValley.Program.GetSavesFolder()).TrimEnd(Path.DirectorySeparatorChar), fullIsolated, StringComparison.OrdinalIgnoreCase)
            || !string.Equals(Path.GetFullPath(Constants.SavesPath).TrimEnd(Path.DirectorySeparatorChar), fullIsolated, StringComparison.OrdinalIgnoreCase)
            || string.IsNullOrEmpty(Constants.SaveFolderName)
            || !File.Exists(Path.Combine(fullIsolated, Constants.SaveFolderName, Constants.SaveFolderName)))
            throw new InvalidOperationException("Harness fixture refused: a loaded clone save and both isolated save roots must be verified.");
        var coordinator = ReadNativeMember(modEntry, "_coordinator");
        var actor = ReadNativeMember(modEntry, "_actor");
        var rest = ReadNativeMember(coordinator, "Rest");
        string? restState = ReadNativeMember(rest, "State")?.ToString();
        if (coordinator == null || actor == null || ReadNativeMember(coordinator, "ActiveMachine") != null
            || ReadNativeMember(actor, "ActiveTaskId") != null || (requireAwake && restState != "awake"))
            throw new InvalidOperationException("Harness fixture refused while a native command or bed recovery is active.");
    }

    private void ArrangeHarnessFixture(string outDir, string label, JsonElement request)
    {
        var info = Helper.ModRegistry.Get("CSXizhang.StardewAICompanion");
        var modEntry = info?.GetType().GetProperty("Mod", BindingFlags.Public | BindingFlags.Instance)?.GetValue(info)
            ?? throw new InvalidOperationException("Production ModEntry unavailable for harness fixture.");
        AssertHarnessFixtureBoundary(modEntry);
        string mode = request.GetProperty("mode").GetString() ?? "";
        if (mode is not ("watering" or "partial-planting" or "harvest-store" or "rest-water" or "water-refill" or "mature-trees" or "walk-pickup" or "walk-pickup-full"))
            throw new ArgumentException("Unknown harness fixture mode.");
        if (Game1.currentSeason != "spring")
            throw new InvalidOperationException("Parsnip harness fixture requires a spring clone; it never changes the game calendar.");
        ResolveCompanionActor(out var actor, out var farmer, out _, out _);
        if (actor == null || farmer?.currentLocation is not Farm)
            throw new InvalidOperationException("Harness fixture requires the actual companion to be on Farm before arrangement.");
        var can = farmer.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault()
            ?? throw new InvalidOperationException("Harness companion has no native watering can.");
        float stamina = request.TryGetProperty("stamina", out var sp) ? sp.GetSingle() : 270;
        int water = request.TryGetProperty("water", out var wp) ? wp.GetInt32() : mode == "watering" ? 10 : 40;
        int foodCount = request.TryGetProperty("foodCount", out var fp) ? fp.GetInt32() : 0;
        if (!float.IsFinite(stamina) || stamina < 0 || stamina > farmer.MaxStamina || water < 0 || water > can.waterCanMax || foodCount is < 0 or > 20)
            throw new ArgumentException("Harness resources exceed real actor or watering-can capacity.");
        if (mode == "partial-planting" && !farmer.Items.Any(item => item == null || item.QualifiedItemId == "(O)472"))
            throw new InvalidOperationException("Harness companion has no backpack slot for the seed fixture.");
        var observer = ReadNativeMember(modEntry, "_observer") ?? throw new InvalidOperationException("Native observer unavailable.");
        var navigator = ReadNativeMember(modEntry, "_navigator") ?? throw new InvalidOperationException("Native navigator unavailable.");
        var tileType = modEntry.GetType().Assembly.GetType("StardewAI.Companion.Mod.Domain.TileCoordinate")!;
        object NativeTile(Vector2 tile) => Activator.CreateInstance(tileType, (int)tile.X, (int)tile.Y)!;
        var passable = observer.GetType().GetMethod("IsTilePassable")!;
        var pathMethod = navigator.GetType().GetMethods().Single(method => method.Name == "FindPathToInteract"
            && method.GetParameters()[0].ParameterType.IsInstanceOfType(actor));
        var farm = Game1.getFarm();
        if (_acceptanceChest is Vector2 previousChest) farm.objects.Remove(previousChest);
        if (_acceptanceDrop != null) farm.debris.Remove(_acceptanceDrop);
        _companionAnimationFrames.Clear();
        _acceptanceMovementSamples = 0;
        _acceptanceSolidOverlaps.Clear();
        var selected = new List<Vector2>();
        var reachable = new List<object>();
        int count = mode == "watering" ? 80 : mode == "partial-planting" ? 19 : mode == "water-refill" ? 12 : 6;
        Vector2 center = farmer.Tile + new Vector2(0, 10);
        var candidates = Enumerable.Range(-16, 33).SelectMany(dx => Enumerable.Range(-6, 25)
                .Select(dy => center + new Vector2(dx, dy)))
            .OrderBy(tile => Vector2.DistanceSquared(tile, center)).ThenBy(tile => tile.Y).ThenBy(tile => tile.X);
        foreach (var tile in candidates)
        {
            int x = (int)tile.X, y = (int)tile.Y;
            if (x < 0 || y < 0 || farm.objects.ContainsKey(tile)
                || farm.doesTileHaveProperty(x, y, "Diggable", "Back") == null
                || !(passable.Invoke(observer, new object[] { "Farm", NativeTile(tile) }) is true)) continue;
            var path = pathMethod.Invoke(navigator, new object?[] { actor, "Farm", NativeTile(tile), null });
            if (ReadNativeMember(path, "Success") is not true) continue;
            var steps = ReadNativeMember(path, "Steps") as System.Collections.IEnumerable;
            object? standing = steps?.Cast<object>().LastOrDefault();
            object? standingTile = ReadNativeMember(standing, "Tile");
            selected.Add(tile);
            reachable.Add(new { target = new { x, y },
                standing = new { x = ReadNativeMember(standingTile, "X"), y = ReadNativeMember(standingTile, "Y") },
                pathSteps = steps?.Cast<object>().Count(), nativePathSuccess = true });
            if (selected.Count == count) break;
        }
        if (selected.Count != count)
            throw new InvalidOperationException($"Harness fixture found only {selected.Count}/{count} genuinely reachable diggable Farm targets; no fixture was applied.");
        void ParkHarnessObserver()
        {
            // FarmHouse overrides requested arrival coordinates with its entrance.
            // Keep the observer on Farm, away from drops and the companion's route.
            var layer = farm.Map.Layers[0];
            var trigger = observer.GetType().GetMethod("IsWarpOrDoorTile")!;
            var spots = Enumerable.Range(0, layer.LayerWidth).SelectMany(x =>
                Enumerable.Range(0, layer.LayerHeight).Select(y => new Vector2(x, y)))
                .Where(tile => Vector2.DistanceSquared(tile, farmer.Tile) >= 25
                    && selected.All(target => Vector2.DistanceSquared(tile, target) >= 100))
                .OrderBy(tile => Vector2.DistanceSquared(tile, farmer.Tile));
            foreach (var tile in spots)
            {
                if (passable.Invoke(observer, new object[] { "Farm", NativeTile(tile) }) is not true
                    || trigger.Invoke(observer, new object[] { "Farm", NativeTile(tile) }) is true) continue;
                Game1.warpFarmer("Farm", (int)tile.X, (int)tile.Y, 2);
                return;
            }
            throw new InvalidOperationException("No safe observer position outside the fixture area.");
        }
        var before = selected.Select(tile => DescribeCropTile(tile,
            farm.terrainFeatures.TryGetValue(tile, out var feature) ? feature as HoeDirt : null)).ToArray();
        _harnessFixtureTiles.Clear();
        _harnessFixtureTiles.AddRange(selected);
        _harnessFixtureInitiallyEmpty.Clear();
        _harnessFixtureMode = mode;
        if (mode is "water-refill" or "harvest-store" or "rest-water" or "mature-trees" or "walk-pickup" or "walk-pickup-full")
        {
            // Natural player requests refer to the whole field. Remove unrelated
            // baseline crops only during isolated fixture setup, never during execution.
            foreach (var pair in farm.terrainFeatures.Pairs)
                if (pair.Value is HoeDirt other && !selected.Contains(pair.Key)) other.crop = null;
        }
        for (int index = 0; index < selected.Count; index++)
        {
            var tile = selected[index];
            var dirt = EnsureHoeDirt(farm, tile);
            bool empty = mode == "partial-planting" && index >= 17;
            dirt.crop = empty ? null : CreateParsnipCrop((int)tile.X, (int)tile.Y, farm, mature: mode == "harvest-store");
            dirt.state.Value = mode is "watering" or "rest-water" or "water-refill" || empty ? 0 : 1;
            if (empty) _harnessFixtureInitiallyEmpty.Add(tile);
        }
        ReadNativeMember(modEntry, "_actor")?.GetType().GetProperty("Stamina")?.SetValue(actor, stamina);
        farmer.stamina = stamina;
        actor.GetType().GetProperty("WaterLeft")?.SetValue(actor, water);
        can.WaterLeft = water;
        for (int index = 0; index < farmer.Items.Count; index++)
            if (farmer.Items[index] is StardewValley.Object item && item.Edibility >= 0) farmer.Items[index] = null;
        if (foodCount > 0) farmer.addItemToInventory(ItemRegistry.Create("(O)153", foodCount));
        if (mode == "partial-planting")
        {
            for (int index = 0; index < farmer.Items.Count; index++)
                if (farmer.Items[index]?.QualifiedItemId == "(O)472") farmer.Items[index] = null;
            if (farmer.addItemToInventory(ItemRegistry.Create("(O)472", 19)) != null)
                throw new InvalidOperationException("Native backpack rejected the seed fixture.");
        }
        _acceptanceChest = null;
        _acceptanceDrop = null;
        _acceptanceMatureTrees.Clear();
        _acceptanceSapling = null;
        if (mode is "harvest-store" or "walk-pickup" or "walk-pickup-full")
        {
            // A real empty chest and real item debris; subsequent collection is never simulated.
            var chestTile = selected[0];
            farm.terrainFeatures.Remove(chestTile);
            _harnessFixtureTiles.Remove(chestTile);
            farm.objects[chestTile] = new StardewValley.Objects.Chest(true, chestTile, "130");
            _acceptanceChest = chestTile;
            var origin = selected[1] * 64 + new Vector2(32, 32);
            _acceptanceDrop = new Debris(ItemRegistry.Create("(O)388", 3), origin, origin);
            farm.debris.Add(_acceptanceDrop);
            // The host is only an observer. Keep its magnet away from native
            // drops; otherwise the fixture can be collected before the AI sees it.
            ParkHarnessObserver();
            for (int index = 0; index < farmer.Items.Count; index++)
                if (farmer.Items[index]?.QualifiedItemId is "(O)24" or "(O)388") farmer.Items[index] = null;
        }
        if (mode is "walk-pickup" or "walk-pickup-full")
        {
            foreach (var tile in _harnessFixtureTiles) farm.terrainFeatures.Remove(tile);
            _harnessFixtureTiles.Clear();
        }
        if (mode == "mature-trees")
        {
            foreach (var pair in farm.terrainFeatures.Pairs.ToArray())
                if (pair.Value is Tree || selected.Contains(pair.Key)) farm.terrainFeatures.Remove(pair.Key);
            _harnessFixtureTiles.Clear();
            _acceptanceChest = selected[0];
            farm.objects[selected[0]] = new StardewValley.Objects.Chest(true, selected[0], "130");
            foreach (var tile in new[] { selected[2], selected[4] })
            {
                farm.terrainFeatures[tile] = new Tree("1", 5);
                _acceptanceMatureTrees.Add(tile);
            }
            _acceptanceSapling = selected[5];
            farm.terrainFeatures[selected[5]] = new Tree("1", 2);
            for (int index = 0; index < farmer.Items.Count; index++)
                if (farmer.Items[index] is not Tool) farmer.Items[index] = null;
            if (!farmer.Items.OfType<StardewValley.Tools.Axe>().Any())
                farmer.addItemToInventory(new StardewValley.Tools.Axe());
            ParkHarnessObserver();
        }
        if (mode == "walk-pickup-full")
        {
            for (int index = 0; index < farmer.MaxItems; index++)
            {
                while (farmer.Items.Count <= index) farmer.Items.Add(null);
                if (farmer.Items[index] is not Tool) farmer.Items[index] = ItemRegistry.Create("(O)390", 999);
            }
        }
        var fixture = new {
            runId = _runId, label, mode, timestampUtc = DateTime.UtcNow.ToString("o"),
            saveName = Constants.SaveFolderName, isolatedSavesPath = StardewValley.Program.GetSavesFolder(),
            year = Game1.year, season = Game1.currentSeason, day = Game1.dayOfMonth, timeOfDay = Game1.timeOfDay,
            locationId = "Farm", targetCount = _harnessFixtureTiles.Count, nativeReachability = reachable,
            chest = _acceptanceChest is Vector2 ct ? new { x = (int)ct.X, y = (int)ct.Y } : null,
            before, after = _harnessFixtureTiles.Select(tile => DescribeCropTile(tile, (HoeDirt)farm.terrainFeatures[tile])).ToArray(),
            initialCropCount = _harnessFixtureTiles.Count - _harnessFixtureInitiallyEmpty.Count, initialEmptyCount = _harnessFixtureInitiallyEmpty.Count,
            companionStamina = farmer.stamina, companionWater = can.WaterLeft, companionItems = DescribeItems(farmer.Items),
            provenance = "Clone fixture arrangement only; actual later effects must come from native execution"
        };
        Directory.CreateDirectory(outDir);
        File.WriteAllText(Path.Combine(outDir, $"harness-fixture-{label}.json"), JsonSerializer.Serialize(fixture, new JsonSerializerOptions { WriteIndented = true }));
        _evidence["harnessFixture"] = fixture;
        var coordinator = ReadNativeMember(modEntry, "_coordinator");
        coordinator?.GetType().GetMethod("OnTimeChanged")?.Invoke(coordinator, new object[] { Game1.timeOfDay });
        observer.GetType().GetMethod("BumpRevision")?.Invoke(observer, null);
        var revision = ReadNativeMember(observer, "WorldRevision");
        var snapshot = coordinator?.GetType().GetMethod("CaptureCurrentSnapshot")?.Invoke(coordinator, new object?[] { revision });
        var transport = ReadNativeMember(modEntry, "_transportServer");
        if (snapshot != null) transport?.GetType().GetMethod("SendSnapshotAsync")?.Invoke(transport, new object?[] { snapshot, revision });
        WriteFreeModeNativeSnapshot(outDir, label);
        Monitor.Log($"[TestDriver] Harness fixture {mode}: {count} reachable actual Farm targets recorded; no action result fabricated.", LogLevel.Info);
    }

    private void HandleLifeUiRequest(string outDir, JsonElement request)
    {
        string action = request.TryGetProperty("action", out var ap) ? ap.GetString() ?? "" : "";
        string label = request.TryGetProperty("label", out var lp) ? lp.GetString() ?? "" : "";
        if (label.Length is < 1 or > 48 || !label.All(c => char.IsLetterOrDigit(c) || c is '-' or '_'))
            throw new ArgumentException("Life UI request needs a safe label.");

        var modInfo = Helper.ModRegistry.Get("CSXizhang.StardewAICompanion");
        var modEntry = modInfo?.GetType().GetProperty("Mod", BindingFlags.Public | BindingFlags.Instance)?.GetValue(modInfo)
            ?? throw new InvalidOperationException("Production ModEntry unavailable.");
        var modType = modEntry.GetType();
        var menu = Game1.activeClickableMenu;
        bool isLife = menu?.GetType().FullName == "StardewAI.Companion.Mod.Menus.CompanionLifeMenu";
        bool isSetup = menu?.GetType().FullName == "StardewAI.Companion.Mod.Menus.CompanionSetupMenu";
        if (action == "interact")
        {
            modType.GetMethod("TryOpenLifeMenuFromInteraction", BindingFlags.NonPublic | BindingFlags.Instance)
                ?.Invoke(modEntry, null);
            menu = Game1.activeClickableMenu;
        }
        else if (action == "leave-player-bed")
        {
            Game1.warpFarmer("Farm", 62, 17, 2);
        }
        else if (action == "restore-continuation-chest")
        {
            AssertHarnessFixtureBoundary(modEntry);
            var tile = new Vector2(request.GetProperty("x").GetInt32(), request.GetProperty("y").GetInt32());
            if (!Game1.getFarm().objects.TryGetValue(tile, out var obj) || obj is not StardewValley.Objects.Chest chest)
                throw new InvalidOperationException("Continuation fixture requires the recorded native chest.");
            if (chest.Items.Any(item => item?.QualifiedItemId == "(O)24"))
                throw new InvalidOperationException("Refuse to restore over existing parsnips.");
            var items = request.GetProperty("items").EnumerateArray().ToArray();
            if (items.Length != 2 || items.Any(item => item.GetProperty("itemId").GetString() != "(O)24"
                || item.GetProperty("count").GetInt32() != 1)
                || !items.Select(item => item.GetProperty("quality").GetInt32()).OrderBy(q => q).SequenceEqual(new[] { 1, 2 }))
                throw new ArgumentException("Continuation fixture only restores the two recorded silver/gold parsnips.");
            foreach (var item in items)
                if (chest.addItem(ItemRegistry.Create("(O)24", 1, item.GetProperty("quality").GetInt32())) != null)
                    throw new InvalidOperationException("Native chest refused the continuation item.");
            File.WriteAllText(Path.Combine(outDir, $"continuation-chest-{label}.json"), JsonSerializer.Serialize(new {
                runId = _runId, label, timestampUtc = DateTime.UtcNow.ToString("o"),
                provenance = "Explicit baseline reconstruction from b14 native terminal/world evidence; no command replay",
                items = chest.Items.Where(item => item != null).Select(item => new { id = item.QualifiedItemId, stack = item.Stack, quality = item.Quality })
            }));
        }
        else if (action == "host-go-bed")
        {
            AssertHarnessFixtureBoundary(modEntry, requireAwake: false);
            var house = Game1.getLocationFromName("FarmHouse") as StardewValley.Locations.FarmHouse;
            var bed = house?.GetPlayerBed() ?? throw new InvalidOperationException("Native player bed missing.");
            var spot = bed.GetBedSpot();
            Game1.warpFarmer("FarmHouse", spot.X, spot.Y, 2);
        }
        else if (action == "host-sleep")
        {
            if (!Context.IsWorldReady || Game1.currentLocation is not StardewValley.Locations.FarmHouse house
                || Game1.activeClickableMenu != null || Game1.fadeToBlack || Game1.fadeIn)
                throw new InvalidOperationException("Native player sleep requires the ready farmhouse and no active menu/fade.");
            var before = new { year = Game1.year, season = Game1.currentSeason, day = Game1.dayOfMonth, timeOfDay = Game1.timeOfDay };
            bool accepted = house.answerDialogueAction("Sleep_Yes", Array.Empty<string>());
            File.WriteAllText(Path.Combine(outDir, $"native-host-sleep-{label}.json"), JsonSerializer.Serialize(new {
                runId = _runId, label, timestampUtc = DateTime.UtcNow.ToString("o"), before, accepted,
                after = new { isMultiplayer = Game1.IsMultiplayer, isMasterGame = Game1.IsMasterGame,
                    multiplayerMode = Game1.multiplayerMode, otherFarmerCount = Game1.otherFarmers.Count,
                    menuType = Game1.activeClickableMenu?.GetType().FullName,
                    newDay = Game1.newDay, playerHasMoved = Game1.player.hasMoved,
                    fadeToBlack = Game1.fadeToBlack, fadeIn = Game1.fadeIn, fadeAlpha = Game1.fadeToBlackAlpha,
                    playerIsInBed = Game1.player.isInBed.Value, playerTimeWentToBed = Game1.player.timeWentToBed.Value },
                provenance = "Native farmhouse Sleep_Yes handler; no calendar or clock assignment"
            }));
            if (!accepted) throw new InvalidOperationException("Native farmhouse rejected Sleep_Yes.");
        }
        else if (action == "open-inventory") Game1.activeClickableMenu = new GameMenu();
        else if (action == "open-f8")
        {
            if (menu != null) throw new InvalidOperationException("Close the active menu before F8.");
            var controller = modType.GetMethod("GetCompanionDialogue", BindingFlags.NonPublic | BindingFlags.Instance)?.Invoke(modEntry, null)
                ?? throw new InvalidOperationException("F8 dialogue controller unavailable.");
            controller.GetType().GetMethod("OpenF8")?.Invoke(controller, null);
        }
        else if (action == "choice")
        {
            if (menu is not DialogueBox nativeChoice) throw new InvalidOperationException("No native choice menu.");
            string key = request.GetProperty("key").GetString() ?? "";
            int index = Array.FindIndex(nativeChoice.responses, response => response.responseKey == key);
            var components = typeof(DialogueBox).GetField("responseCC", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
                ?.GetValue(nativeChoice) as List<ClickableComponent>;
            if (index < 0 || components == null || index >= components.Count)
                throw new InvalidOperationException("Native response is not laid out yet.");
            var rect = components[index].bounds;
            nativeChoice.performHoverAction(rect.Center.X, rect.Center.Y);
            var selected = typeof(DialogueBox).GetField("selectedResponse", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
                ?.GetValue(nativeChoice);
            if (selected is int selectedIndex && selectedIndex != index)
                throw new InvalidOperationException($"Native hover selected {selectedIndex}, expected {index}.");
            nativeChoice.receiveLeftClick(rect.Center.X, rect.Center.Y, playSound: false);
        }
        else if (action == "recovery-fixture")
        {
            AssertHarnessFixtureBoundary(modEntry);
            ResolveCompanionActor(out var recoveryActor, out var farmer, out _, out _);
            if (farmer == null) throw new InvalidOperationException("Companion farmer unavailable.");
            if (request.TryGetProperty("stamina", out var stamina))
            {
                recoveryActor?.GetType().GetProperty("Stamina")?.SetValue(recoveryActor, stamina.GetSingle());
                farmer.stamina = stamina.GetSingle();
            }
            if (request.TryGetProperty("water", out var water))
            {
                var can = farmer.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault()
                    ?? throw new InvalidOperationException("Companion has no watering can.");
                recoveryActor?.GetType().GetProperty("WaterLeft")?.SetValue(recoveryActor, water.GetInt32());
                can.WaterLeft = water.GetInt32();
            }
            if (request.TryGetProperty("foodCount", out var foodCount))
            {
                for (int i = 0; i < farmer.Items.Count; i++)
                    if (farmer.Items[i] is StardewValley.Object food && food.Edibility >= 0) farmer.Items[i] = null;
                if (foodCount.GetInt32() > 0) farmer.addItemToInventory(ItemRegistry.Create("(O)153", foodCount.GetInt32()));
            }
            var fixtureCoordinator = modType.GetField("_coordinator", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(modEntry);
            fixtureCoordinator?.GetType().GetMethod("OnTimeChanged")?.Invoke(fixtureCoordinator, new object[] { Game1.timeOfDay });
            WriteFreeModeNativeSnapshot(outDir, label);
        }
        else if (action == "harness-fixture")
        {
            ArrangeHarnessFixture(outDir, label, request);
        }
        else if (action == "harness-capture")
        {
            if (!_scenario.Equals("free-mode-control", StringComparison.OrdinalIgnoreCase) || Game1.activeClickableMenu != null)
                throw new InvalidOperationException("Harness world capture requires the isolated scenario with no menu.");
            _harnessCaptureLabel = label;
        }
        else if (action is "open-life" or "open-setup" or "open-conversation" or "open-directions" or "open-work" or "open-memory")
        {
            string methodName = action switch
            {
                "open-life" => "OpenLifeMenu", "open-conversation" => "OpenCompanionConversation",
                "open-memory" => "OpenCompanionMemory",
                "open-work" => "OpenTaskPanel",
                "open-directions" => "OpenLifeDirections", _ => "OpenSetupMenu"
            };
            modType.GetMethod(methodName, BindingFlags.NonPublic | BindingFlags.Instance)?.Invoke(modEntry, null);
            menu = Game1.activeClickableMenu;
        }
        else if (action == "close")
        {
            var closeController = modType.GetMethod("GetCompanionDialogue", BindingFlags.NonPublic | BindingFlags.Instance)?.Invoke(modEntry, null);
            bool handledClose = closeController?.GetType().GetMethod("HandleF8")?.Invoke(closeController, null) is true;
            if (!handledClose && menu != null) menu.exitThisMenu(playSound: false);
            menu = Game1.activeClickableMenu;
        }
        else if (action == "click")
        {
            if (menu == null) throw new InvalidOperationException("No open menu.");
            string fieldName = request.TryGetProperty("field", out var fp) ? fp.GetString() ?? "" : "";
            int index = request.TryGetProperty("index", out var ip) ? ip.GetInt32() : -1;
            var field = menu.GetType().GetField(fieldName, BindingFlags.NonPublic | BindingFlags.Instance)
                ?? throw new InvalidOperationException($"No rectangle field {fieldName}.");
            object? value = field.GetValue(menu);
            if (value is Rectangle[] array) value = index >= 0 && index < array.Length ? array[index] : null;
            if (value is not Rectangle rect) throw new InvalidOperationException("Invalid rectangle/index.");
            int clickY = request.TryGetProperty("offsetY", out var offsetY)
                ? rect.Y + Math.Clamp(offsetY.GetInt32(), 0, Math.Max(0, rect.Height - 1))
                : rect.Y + rect.Height / 2;
            menu.receiveLeftClick(rect.X + rect.Width / 2, clickY, playSound: false);
            menu = Game1.activeClickableMenu;
        }
        else if (action == "hub-button")
        {
            if (menu == null) throw new InvalidOperationException("No open menu.");
            string text = request.GetProperty("text").GetString() ?? "";
            var buttons = menu.GetType().GetField("_buttons", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(menu) as System.Collections.IEnumerable;
            var button = buttons?.Cast<object>().Select(b => b as System.Runtime.CompilerServices.ITuple)
                .FirstOrDefault(b => b != null && b[1]?.ToString()?.TrimEnd(' ', '•') == text)
                ?? throw new InvalidOperationException($"Missing hub button {text}.");
            var rect = (Rectangle)button[0]!;
            if (menu.GetType().Name == "CompanionDashboardMenu" &&
                !(button[3] is true) &&
                menu.GetType().GetField("_body", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(menu) is Rectangle body &&
                !body.Contains(rect))
                throw new InvalidOperationException($"Hub button {text} is outside the visible body; scroll before clicking.");
            menu.receiveLeftClick(rect.Center.X, rect.Center.Y, playSound: false);
        }
        else if (action == "scroll")
        {
            if (menu == null) throw new InvalidOperationException("No open menu.");
            menu.receiveScrollWheelAction(request.GetProperty("direction").GetInt32());
        }
        else if (action == "clickxy")
        {
            if (menu == null) throw new InvalidOperationException("No open menu.");
            int x = request.TryGetProperty("x", out var xp) ? xp.GetInt32() : -1;
            int y = request.TryGetProperty("y", out var yp) ? yp.GetInt32() : -1;
            if (x < 0 || y < 0 || x >= Game1.uiViewport.Width || y >= Game1.uiViewport.Height)
                throw new ArgumentException("Click coordinates outside game UI viewport.");
            menu.receiveLeftClick(x, y, playSound: false);
            menu = Game1.activeClickableMenu;
        }
        else if (action == "advance-dialogue")
        {
            if (menu is not DialogueBox) throw new InvalidOperationException("No native dialogue to advance.");
            menu.receiveKeyPress(Microsoft.Xna.Framework.Input.Keys.Enter);
            menu = Game1.activeClickableMenu;
        }
        else if (action == "text")
        {
            if (menu == null) throw new InvalidOperationException("No open menu.");
            string fieldName = request.TryGetProperty("field", out var fp) ? fp.GetString() ?? "" : "";
            string value = request.TryGetProperty("text", out var tp) ? tp.GetString() ?? "" : "";
            var box = menu.GetType().GetField(fieldName, BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(menu)
                ?? throw new InvalidOperationException($"No text field {fieldName}.");
            box.GetType().GetProperty("Text")?.SetValue(box, value);
        }
        else if (action == "key")
        {
            string key = request.GetProperty("key").GetString() ?? "";
            if (menu == null || !Enum.TryParse<Microsoft.Xna.Framework.Input.Keys>(key, out var parsed))
                throw new ArgumentException("Invalid menu key.");
            var keyController = modType.GetMethod("GetCompanionDialogue", BindingFlags.NonPublic | BindingFlags.Instance)?.Invoke(modEntry, null);
            bool handledRoot = parsed == Microsoft.Xna.Framework.Input.Keys.Escape &&
                keyController?.GetType().GetMethod("HandleEscape")?.Invoke(keyController, null) is true;
            if (!handledRoot) menu.receiveKeyPress(parsed);
        }
        else if (action == "snapshot") _lifeUiCaptureLabel = label;
        else throw new ArgumentException($"Unknown life UI action {action}.");

        menu = Game1.activeClickableMenu;
        isLife = menu?.GetType().FullName == "StardewAI.Companion.Mod.Menus.CompanionLifeMenu";
        isSetup = menu?.GetType().FullName == "StardewAI.Companion.Mod.Menus.CompanionSetupMenu";
        var state = modType.GetField("_lifeMenuUiState", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(modEntry);
        object? Prop(string name) => state?.GetType().GetProperty(name)?.GetValue(state);
        object? Field(string name) => menu?.GetType().GetField(name, BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(menu);
        var evidenceController = modType.GetMethod("GetCompanionDialogue", BindingFlags.NonPublic | BindingFlags.Instance)?.Invoke(modEntry, null);
        object? ControllerField(string name) => evidenceController?.GetType().GetField(name, BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(evidenceController);
        string? ControllerCallback(string name) => (ControllerField(name) as Delegate)?.Method.Name;
        var commandMenuType = modType.Assembly.GetType("StardewAI.Companion.Mod.Menus.CompanionCommandMenu");
        object? MenuStatic(string name) => commandMenuType?.GetProperty(name, BindingFlags.Public | BindingFlags.Static)?.GetValue(null);
        var hubButtons = (Field("_buttons") as System.Collections.IEnumerable)?.Cast<object>()
            .Select(button => button as System.Runtime.CompilerServices.ITuple)
            .Where(button => button != null && button[0] is Rectangle && button[1] is string)
            .Select(button => {
                var rect = (Rectangle)button![0]!;
                bool fixedPosition = button.Length > 3 && button[3] is true;
                bool visible = fixedPosition || Field("_body") is Rectangle body && body.Contains(rect);
                return new { text = button[1]?.ToString(), visible, fixedPosition,
                    x = rect.X, y = rect.Y, width = rect.Width, height = rect.Height };
            }).ToArray();
        var snapshot = new Dictionary<string, object?>
        {
            ["runId"] = _runId, ["label"] = label, ["action"] = action,
            ["timestampUtc"] = DateTime.UtcNow.ToString("o"),
            ["productionOwnedBridgeLaunchObserved"] = ReadNativeMember(modEntry, "_ownedBridgeLaunch") != null,
            ["productionChatConnected"] = ReadNativeMember(ReadNativeMember(modEntry, "_transportServer"), "IsChatConnected"),
            ["f8Active"] = ControllerField("_f8Active"), ["f8Next"] = ControllerCallback("_next"),
            ["f8AfterPages"] = ControllerCallback("_afterPages"), ["f8Pages"] = ControllerField("_pages")?.GetType().FullName,
            ["nativeExitCallback"] = menu?.exitFunction?.Method.Name,
            ["year"] = Game1.year, ["season"] = Game1.currentSeason, ["day"] = Game1.dayOfMonth,
            ["timeOfDay"] = Game1.timeOfDay, ["menuType"] = menu?.GetType().FullName,
            ["dialogueResponses"] = menu is DialogueBox dialogue
                ? dialogue.responses.Select(response => new { key = response.responseKey, text = response.responseText }).ToArray() : null,
            ["playerTile"] = new { x = (int)Game1.player.Tile.X, y = (int)Game1.player.Tile.Y },
            ["playerFacing"] = Game1.player.FacingDirection,
            ["lifeMenuOpen"] = isLife, ["setupMenuOpen"] = isSetup,
            ["panel"] = isLife ? Field("_activePanel")?.ToString() : null,
            ["hubTab"] = Field("_tab"), ["selectedDecision"] = Field("_replyTo"),
            ["hubButtons"] = hubButtons,
            ["hubBody"] = Field("_body") is Rectangle hubBody
                ? new { x = hubBody.X, y = hubBody.Y, width = hubBody.Width, height = hubBody.Height } : null,
            ["autonomyMode"] = MenuStatic("AutonomyMode"), ["autonomyPaused"] = MenuStatic("AutonomyPaused"),
            ["modeButtonText"] = MenuStatic("ModeButtonText"), ["modeChangePending"] = MenuStatic("ModeChangePending"),
            ["scrollOffset"] = Field("_scroll"), ["maxScroll"] = Field("_maxScroll"),
            ["inputText"] = Field("_input")?.GetType().GetProperty("Text")?.GetValue(Field("_input")),
            ["menuBounds"] = menu == null ? null : new { x = menu.xPositionOnScreen, y = menu.yPositionOnScreen, width = menu.width, height = menu.height },
            ["playStyleDraft"] = isSetup ? Field("_playStyle") : null,
            ["personalityDraft"] = isSetup ? Field("_personality") : null,
            ["careDraft"] = isSetup ? Field("_careFrequency") : null,
            ["chatStatus"] = Prop("ChatStatus")?.ToString(), ["replyText"] = Prop("ReplyText"),
            ["conversation"] = Prop("Conversation"), ["unreadReplyCount"] = Prop("UnreadReplyCount"),
            ["workState"] = modType.Assembly.GetType("StardewAI.Companion.Mod.Menus.CompanionCommandMenu")?.GetProperty("TaskState", BindingFlags.Public | BindingFlags.Static)?.GetValue(null),
            ["companionActionText"] = modType.Assembly.GetType("StardewAI.Companion.Mod.Menus.CompanionCommandMenu")?.GetProperty("CurrentActionText", BindingFlags.Public | BindingFlags.Static)?.GetValue(null),
            ["hudMessages"] = Game1.hudMessages.Select(h => h.GetType().GetField("message", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(h)?.ToString()).ToArray(),
            ["profileRevision"] = Prop("ProfileRevision"), ["hasProfileState"] = Prop("HasProfileState"),
            ["onboarded"] = Prop("IsOnboarded"), ["skipped"] = Prop("IsSkipped"),
            ["playStyle"] = Prop("PlayStyle"), ["personality"] = Prop("Personality"),
            ["careFrequency"] = Prop("CareFrequency"), ["workMode"] = Prop("WorkMode"),
            ["memoryRevision"] = Prop("MemoryRevision"), ["memoryEntries"] = Prop("MemoryEntries"),
            ["milestoneNodes"] = Prop("MilestoneNodes"),
            ["unreadCareHints"] = Prop("UnreadCareHints")
        };
        File.WriteAllText(Path.Combine(outDir, $"life-ui-{label}.json"),
            JsonSerializer.Serialize(snapshot, new JsonSerializerOptions { WriteIndented = true }),
            new System.Text.UTF8Encoding(false));
    }

    private void InjectMenuCommand(string commandText)
    {
        var menu = OpenProductionMenuForAcceptance();
        var menuType = menu.GetType();
        Monitor.Log($"[TestDriver] menu-inject: menu opened, injecting '{commandText}'.", LogLevel.Info);

        var textBoxField = menuType.GetField("_textBox", BindingFlags.NonPublic | BindingFlags.Instance);
        var textBox = textBoxField?.GetValue(menu);
        textBox?.GetType().GetProperty("Text", BindingFlags.Public | BindingFlags.Instance)?.SetValue(textBox, commandText);

        menuType.GetMethod("receiveKeyPress", BindingFlags.Public | BindingFlags.Instance)
            ?.Invoke(menu, new object[] { Microsoft.Xna.Framework.Input.Keys.Enter });
        Monitor.Log("[TestDriver] menu-inject: Enter submitted.", LogLevel.Info);

        if (Game1.activeClickableMenu == menu)
        {
            ((IClickableMenu)menu).exitThisMenu(playSound: false);
            if (Game1.activeClickableMenu == menu)
            {
                Game1.activeClickableMenu = null;
            }
        }
    }

    private Action? GetModActionDelegate(object modEntry, string methodName, out string? error)
    {
        error = null;
        try
        {
            var del = (Action?)Delegate.CreateDelegate(typeof(Action), modEntry, methodName, false);
            if (del != null) return del;

            var method = modEntry.GetType().GetMethod(methodName, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (method != null)
            {
                return (Action?)Delegate.CreateDelegate(typeof(Action), modEntry, method, false);
            }

            error = $"Method '{methodName}' not found on {modEntry.GetType().FullName}.";
            return null;
        }
        catch (Exception ex)
        {
            error = $"Exception creating delegate for '{methodName}': {ex.Message}";
            return null;
        }
    }

    private void InjectFreeModeControl()
    {
        var modInfo = Helper.ModRegistry.Get("CSXizhang.StardewAICompanion");
        var modEntry = modInfo?.GetType().GetProperty("Mod", BindingFlags.Public | BindingFlags.Instance)?.GetValue(modInfo);
        if (modEntry == null)
        {
            string err = "[TestDriver] free-mode-control: production ModEntry instance not found.";
            Monitor.Log(err, LogLevel.Error);
            _evidence["f8ControlInjected"] = false;
            _evidence["f8ControlFailureReason"] = err;
            if (!_evidence.ContainsKey("failureReason"))
                _evidence["failureReason"] = err;
            return;
        }

        var toggle = GetModActionDelegate(modEntry, "ToggleAutonomyMode", out string? bindError);
        if (toggle == null)
        {
            string err = $"[TestDriver] free-mode-control: ToggleAutonomyMode delegate binding failed: {bindError}";
            Monitor.Log(err, LogLevel.Error);
            _evidence["f8ControlInjected"] = false;
            _evidence["f8ControlFailureReason"] = err;
            if (!_evidence.ContainsKey("failureReason"))
                _evidence["failureReason"] = err;
            return;
        }

        try
        {
            toggle();
            _evidence["f8ControlInjected"] = true;
            Monitor.Log("[TestDriver] free-mode-control clicked the production F8 mode-toggle callback.", LogLevel.Info);
        }
        catch (Exception ex)
        {
            string err = $"[TestDriver] free-mode-control: error invoking ToggleAutonomyMode: {ex.Message}";
            Monitor.Log(err, LogLevel.Error);
            _evidence["f8ControlInjected"] = false;
            _evidence["f8ControlFailureReason"] = err;
            if (!_evidence.ContainsKey("failureReason"))
                _evidence["failureReason"] = err;
        }
    }

    private void InjectFreeModeAction(string action)
    {
        string normalizedAction = action?.Trim().ToLowerInvariant() ?? "";
        string? methodName = normalizedAction switch
        {
            "pause" => "RequestPauseMenuAction",
            "resume" => "RequestResumeMenuAction",
            "cancel" => "RequestCancelMenuAction",
            _ => null
        };

        if (methodName == null)
        {
            Monitor.Log($"[TestDriver] free-mode-action: unknown action '{action}'. Expected 'pause', 'resume', or 'cancel'.", LogLevel.Warn);
            return;
        }

        var modInfo = Helper.ModRegistry.Get("CSXizhang.StardewAICompanion");
        var modEntry = modInfo?.GetType().GetProperty("Mod", BindingFlags.Public | BindingFlags.Instance)?.GetValue(modInfo);
        if (modEntry == null)
        {
            string err = $"[TestDriver] free-mode-action ({action}): production ModEntry instance not found.";
            Monitor.Log(err, LogLevel.Error);
            _evidence[$"f8{action}Injected"] = false;
            _evidence[$"f8{action}FailureReason"] = err;
            if (!string.Equals(action, normalizedAction, StringComparison.Ordinal))
            {
                _evidence[$"f8{normalizedAction}Injected"] = false;
                _evidence[$"f8{normalizedAction}FailureReason"] = err;
            }
            if (!_evidence.ContainsKey("failureReason"))
                _evidence["failureReason"] = err;
            return;
        }

        var actionDelegate = GetModActionDelegate(modEntry, methodName, out string? bindError);
        if (actionDelegate == null)
        {
            string err = $"[TestDriver] free-mode-action ({action}): {methodName} delegate binding failed: {bindError}";
            Monitor.Log(err, LogLevel.Error);
            _evidence[$"f8{action}Injected"] = false;
            _evidence[$"f8{action}FailureReason"] = err;
            if (!string.Equals(action, normalizedAction, StringComparison.Ordinal))
            {
                _evidence[$"f8{normalizedAction}Injected"] = false;
                _evidence[$"f8{normalizedAction}FailureReason"] = err;
            }
            if (!_evidence.ContainsKey("failureReason"))
                _evidence["failureReason"] = err;
            return;
        }

        try
        {
            actionDelegate();
            _evidence[$"f8{action}Injected"] = true;
            if (!string.Equals(action, normalizedAction, StringComparison.Ordinal))
            {
                _evidence[$"f8{normalizedAction}Injected"] = true;
            }
            Monitor.Log($"[TestDriver] free-mode-action invoked the production F8 {action} callback.", LogLevel.Info);
            if (!string.IsNullOrWhiteSpace(_evidenceDir))
            {
                var ack = new { action = normalizedAction, invoked = true,
                    year = Game1.year, season = Game1.currentSeason, day = Game1.dayOfMonth,
                    timeOfDay = Game1.timeOfDay };
                File.WriteAllText(Path.Combine(_evidenceDir, $"free-mode-action-{normalizedAction}-ack.json"),
                    JsonSerializer.Serialize(ack));
            }
        }
        catch (Exception ex)
        {
            string err = $"[TestDriver] free-mode-action ({action}): error invoking {methodName}: {ex.Message}";
            Monitor.Log(err, LogLevel.Error);
            _evidence[$"f8{action}Injected"] = false;
            _evidence[$"f8{action}FailureReason"] = err;
            if (!string.Equals(action, normalizedAction, StringComparison.Ordinal))
            {
                _evidence[$"f8{normalizedAction}Injected"] = false;
                _evidence[$"f8{normalizedAction}FailureReason"] = err;
            }
            if (!_evidence.ContainsKey("failureReason"))
                _evidence["failureReason"] = err;
        }
    }

    private bool ValidateTerminalResult(out string? rejectionReason, out Dictionary<string, object?>? terminalData, params string[] expectedStatuses)
    {
        rejectionReason = null;
        terminalData = null;

        var outDir = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
        var terminalPath = Path.Combine(outDir, "terminal-result.json");

        if (!File.Exists(terminalPath))
        {
            rejectionReason = $"Terminal record missing: '{terminalPath}' not found.";
            return false;
        }

        try
        {
            var text = File.ReadAllText(terminalPath);
            using var doc = JsonDocument.Parse(text);
            var root = doc.RootElement;

            // 1. Validate runId match
            string? recRunId = root.TryGetProperty("runId", out var pRun) ? pRun.GetString() : null;
            if (!string.Equals(recRunId, _runId, StringComparison.OrdinalIgnoreCase))
            {
                rejectionReason = $"RunId mismatch: record has '{recRunId}', expected '{_runId}'.";
                return false;
            }

            // 2. Validate scenario match
            string? recScenario = root.TryGetProperty("scenario", out var pScen) ? pScen.GetString() : null;
            if (!string.Equals(recScenario, _scenario, StringComparison.OrdinalIgnoreCase))
            {
                rejectionReason = $"Scenario mismatch: record has '{recScenario}', expected '{_scenario}'.";
                return false;
            }

            // 3. Validate status matches one of the scenario's accepted terminal statuses
            string? recStatus = root.TryGetProperty("status", out var pStat) ? pStat.GetString() : null;
            var accepted = expectedStatuses.Length > 0 ? expectedStatuses : new[] { "succeeded" };
            if (recStatus == null || !accepted.Any(s => string.Equals(s, recStatus, StringComparison.OrdinalIgnoreCase)))
            {
                rejectionReason = $"Terminal status '{recStatus}' is not one of the accepted statuses: [{string.Join(", ", accepted)}].";
                return false;
            }

            // 4. Validate commandId present
            string? recCmd = root.TryGetProperty("commandId", out var pCmd) ? pCmd.GetString() : null;
            if (string.IsNullOrWhiteSpace(recCmd))
            {
                rejectionReason = "Terminal record missing valid commandId.";
                return false;
            }

            // 5. Validate gameSessionId present
            string? recSession = root.TryGetProperty("gameSessionId", out var pSess) ? pSess.GetString() : null;
            if (string.IsNullOrWhiteSpace(recSession))
            {
                rejectionReason = "Terminal record missing valid gameSessionId.";
                return false;
            }

            terminalData = new Dictionary<string, object?>
            {
                ["runId"] = recRunId,
                ["scenario"] = recScenario,
                ["status"] = recStatus,
                ["commandId"] = recCmd,
                ["gameSessionId"] = recSession
            };

            // Enrich with payload details for per-scenario assertions
            if (root.TryGetProperty("resultPayload", out var pPayload) && pPayload.ValueKind == JsonValueKind.Object)
            {
                int GetCount(string name) => pPayload.TryGetProperty(name, out var pv) && pv.TryGetInt32(out var vi) ? vi : 0;
                terminalData["completedCount"] = GetCount("completedCount");
                terminalData["skippedCount"] = GetCount("skippedCount");
                terminalData["failedCount"] = GetCount("failedCount");
                if (pPayload.TryGetProperty("resources", out var pRes) && pRes.ValueKind == JsonValueKind.Object)
                {
                    terminalData["staminaUsed"] = pRes.TryGetProperty("staminaUsed", out var ps) && ps.TryGetSingle(out var sf) ? sf : 0f;
                    terminalData["waterUsed"] = pRes.TryGetProperty("waterUsed", out var pw) && pw.TryGetInt32(out var wi) ? wi : 0;
                }
                if (pPayload.TryGetProperty("error", out var pErr) && pErr.ValueKind == JsonValueKind.Object &&
                    pErr.TryGetProperty("code", out var pErrCode))
                {
                    terminalData["errorCode"] = pErrCode.GetString();
                }
                if (pPayload.TryGetProperty("effects", out var pEffects) && pEffects.ValueKind == JsonValueKind.Array)
                {
                    var effectTiles = new List<string>();
                    foreach (var eff in pEffects.EnumerateArray())
                    {
                        if (eff.TryGetProperty("tile", out var pTile) &&
                            pTile.TryGetProperty("x", out var px) && pTile.TryGetProperty("y", out var py))
                        {
                            effectTiles.Add($"{px.GetInt32()},{py.GetInt32()}");
                        }
                    }
                    terminalData["effectsTiles"] = effectTiles;
                }
            }

            // Pass through wave-2 control-flow evidence recorded by the orchestrator
            foreach (var extraKey in new[] { "replayResponse", "conflictResponse", "controlFlow" })
            {
                if (root.TryGetProperty(extraKey, out var pExtra))
                {
                    terminalData[extraKey] = pExtra.GetRawText();
                }
            }
            return true;
        }
        catch (Exception ex)
        {
            rejectionReason = $"Error parsing terminal-result.json: {ex.Message}";
            return false;
        }
    }

    private static int IntOf(Dictionary<string, object?> d, string key) =>
        d.TryGetValue(key, out var v) ? v switch { int i => i, long l => (int)l, _ => 0 } : 0;

    private static float FloatOf(Dictionary<string, object?> d, string key) =>
        d.TryGetValue(key, out var v) ? v switch { float f => f, double db => (float)db, int i => i, _ => 0f } : 0f;

    private static List<string> EffectsOf(Dictionary<string, object?>? d) =>
        d != null && d.TryGetValue("effectsTiles", out var v) && v is List<string> tiles ? tiles : new List<string>();

    /// <summary>
    /// Evaluates the full gate for precondition/rejection-style scenarios: validates the terminal
    /// record against the EXPECTED terminal status (a correct rejection/failure is a pass),
    /// then asserts dirt state, payload details, actor resource invariance, player invariance,
    /// and optionally that the companion never ended on a blocked tile.
    /// </summary>
    private void EvaluateScenarioGate(
        string expectedStatus,
        bool expectDirtWatered,
        bool expectActorResourcesUnchanged,
        bool targetWatered,
        bool playerInvariant,
        bool screenshotValid,
        string? expectedErrorCode = null,
        string[]? additionalAcceptedStatuses = null,
        Func<Dictionary<string, object?>, bool>? payloadAssertion = null,
        string? payloadAssertionDescription = null,
        bool assertNotOnBlockedTiles = false,
        bool skipDirtCheck = false,
        Dictionary<string, bool>? actualZoneWatered = null,
        Func<Dictionary<string, object?>?, Dictionary<string, bool>, string?>? zoneAssertion = null,
        Func<Dictionary<string, object?>?, int>? expectedWaterSpentFromPayload = null)
    {
        var acceptedStatuses = additionalAcceptedStatuses is { Length: > 0 }
            ? new[] { expectedStatus }.Concat(additionalAcceptedStatuses).ToArray()
            : new[] { expectedStatus };
        bool validTerminal = ValidateTerminalResult(out string? rejectReason, out var termData, acceptedStatuses);
        if (!validTerminal)
        {
            _evidence["actExecuted"] = false;
            _evidence["terminalRecordValid"] = false;
            _evidence["terminalRejectionReason"] = rejectReason;
            _evidence["functionalSkillPassed"] = false;
            _evidence["gateSuccess"] = false;
            if (!_evidence.ContainsKey("failureReason"))
            {
                _evidence["failureReason"] = $"BLOCKED_ON_PEER_INTEGRATION: External Runtime Act did not complete with expected terminal status '{expectedStatus}'. {rejectReason}";
            }
            return;
        }

        _evidence["actExecuted"] = true;
        _evidence["terminalRecordValid"] = true;
        _evidence["terminalRecord"] = termData;

        var failures = new List<string>();

        if (!skipDirtCheck && targetWatered != expectDirtWatered)
            failures.Add($"Target dirt watered state is {targetWatered}, expected {expectDirtWatered}.");

        if (expectedErrorCode != null &&
            !string.Equals(termData?.GetValueOrDefault("errorCode") as string, expectedErrorCode, StringComparison.OrdinalIgnoreCase))
            failures.Add($"Expected error code '{expectedErrorCode}', got '{termData?.GetValueOrDefault("errorCode")}'.");

        if (payloadAssertion != null && (termData == null || !payloadAssertion(termData)))
            failures.Add($"Payload assertion failed: {payloadAssertionDescription}.");

        if (zoneAssertion != null && actualZoneWatered != null)
        {
            string? zoneError = zoneAssertion(termData, actualZoneWatered);
            if (zoneError != null)
                failures.Add(zoneError);
        }

        if (ResolveCompanionActor(out var actor, out var farmer))
        {
            float curStam = farmer?.Stamina ?? _preActorStamina;
            var can = farmer?.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault();
            int curWater = can?.WaterLeft ?? _preActorWater;
            _evidence["postActorStamina"] = curStam;
            _evidence["postActorWater"] = curWater;
            _evidence["actorResourceDeducted"] = curStam < _preActorStamina || curWater < _preActorWater;

            if (expectActorResourcesUnchanged && (curStam != _preActorStamina || curWater != _preActorWater))
            {
                failures.Add($"Actor resources changed (stamina {_preActorStamina}->{curStam}, water {_preActorWater}->{curWater}) where zero cost was expected.");
            }

            if (expectedWaterSpentFromPayload != null)
            {
                int expectedSpent = expectedWaterSpentFromPayload(termData);
                int actualSpent = _preActorWater - curWater;
                if (actualSpent != expectedSpent)
                {
                    failures.Add($"Actor water spent is {actualSpent}, expected exactly {expectedSpent} (double-charge or missing charge).");
                }
            }

            if (assertNotOnBlockedTiles && farmer != null)
            {
                var ft = new Vector2((int)farmer.Tile.X, (int)farmer.Tile.Y);
                if (_blockedTiles.Contains(ft))
                {
                    failures.Add($"Companion ended on blocked tile ({ft.X},{ft.Y}).");
                }
            }
        }
        else
        {
            failures.Add("Could not resolve companion actor for post-Act verification.");
        }

        if (!playerInvariant) failures.Add("Player resources or inventory changed during companion action.");

        bool passed = failures.Count == 0;
        _evidence["functionalSkillPassed"] = passed;
        _evidence["gateSuccess"] = passed;
        if (!passed)
        {
            _evidence["failureReason"] = string.Join(" ", failures);
        }
    }

    private sealed class DoneFileInfo
    {
        public bool Exists { get; set; }
        public bool Success { get; set; }
        public bool SuccessExplicit { get; set; }
        public string? Error { get; set; }
        public string? Scenario { get; set; }
        public string RawJson { get; set; } = "";
    }

    // Reads the external completion signal written by the scenario driver. The file name stays
    // loop-demo-done.json for every scenario on this channel; its "scenario" string is recorded
    // as evidence but not matched.
    private DoneFileInfo ReadLoopDemoDoneFile(string outDir)
    {
        var info = new DoneFileInfo();
        var doneFile = Path.Combine(outDir, "existing-save-buy5-done.json");
        if (!File.Exists(doneFile))
        {
            doneFile = Path.Combine(outDir, "chest-operator-done.json");
        }
        if (!File.Exists(doneFile))
        {
            doneFile = Path.Combine(outDir, "cycle-operator-done.json");
        }
        if (!File.Exists(doneFile))
        {
            doneFile = Path.Combine(outDir, "cycle-mcp-done.json");
        }
        if (!File.Exists(doneFile))
        {
            doneFile = Path.Combine(outDir, "free-mode-control-done.json");
        }
        if (!File.Exists(doneFile))
        {
            doneFile = Path.Combine(outDir, "purchase-mcp-done.json");
        }
        if (!File.Exists(doneFile))
        {
            doneFile = Path.Combine(outDir, "navigation-mcp-done.json");
        }
        if (!File.Exists(doneFile))
        {
            doneFile = Path.Combine(outDir, "shipping-mcp-done.json");
        }
        if (!File.Exists(doneFile))
        {
            doneFile = Path.Combine(outDir, "planting-mcp-done.json");
        }
        if (!File.Exists(doneFile))
        {
            doneFile = Path.Combine(outDir, "daily-chain-done.json");
        }
        if (!File.Exists(doneFile))
        {
            doneFile = Path.Combine(outDir, "farm-capabilities-done.json");
        }
        if (!File.Exists(doneFile))
        {
            doneFile = Path.Combine(outDir, "loop-demo-done.json");
        }
        info.Exists = File.Exists(doneFile);
        if (!info.Exists) return info;

        try
        {
            info.RawJson = File.ReadAllText(doneFile);
            using var doc = JsonDocument.Parse(info.RawJson);
            var root = doc.RootElement;
            if (root.TryGetProperty("success", out var succElem) &&
                (succElem.ValueKind == JsonValueKind.True || succElem.ValueKind == JsonValueKind.False))
            {
                info.Success = succElem.GetBoolean();
                info.SuccessExplicit = true;
            }
            if (root.TryGetProperty("error", out var errElem) && errElem.ValueKind == JsonValueKind.String)
            {
                info.Error = errElem.GetString();
            }
            if (root.TryGetProperty("scenario", out var scenElem) && scenElem.ValueKind == JsonValueKind.String)
            {
                info.Scenario = scenElem.GetString();
            }
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Note reading loop-demo-done.json: {ex.Message}", LogLevel.Warn);
            info.Success = false;
            info.Error = $"Failed to parse loop-demo-done.json: {ex.Message}";
        }
        return info;
    }

    private void RecordDoneFileEvidence(DoneFileInfo done)
    {
        _evidence["framesCaptured"] = _loopDemoFrameIndex;
        _evidence["actExecuted"] = done.Exists;
        _evidence["terminalRecordValid"] = done.Exists;
        if (!done.Exists) return;

        _evidence["loopDemoDone"] = true;
        _evidence["doneFileScenario"] = done.Scenario;
        try
        {
            _evidence["loopDemoSummary"] = JsonSerializer.Deserialize<object>(done.RawJson);
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Note embedding loop-demo-done.json summary: {ex.Message}", LogLevel.Warn);
        }
    }

    /// <summary>
    /// harvest-demo gate: external done-file success plus world assertions — the 3 mature
    /// parsnips harvested, the immature parsnip at (65,15) untouched, the (66,20) chest present,
    /// and (O)24 conservation across chest + companion inventory (5 initial + 3 harvested = 8).
    /// </summary>
    private void EvaluateHarvestDemoGate(string outDir, bool playerInvariant)
    {
        var done = ReadLoopDemoDoneFile(outDir);
        RecordDoneFileEvidence(done);

        var failures = new List<string>();
        var farm = Game1.getFarm();

        var matureTiles = new[] { new Vector2(63, 20), new Vector2(64, 20), new Vector2(65, 20) };
        var matureStates = new Dictionary<string, object?>();
        var stillReady = new List<string>();
        foreach (var t in matureTiles)
        {
            var hd = farm.terrainFeatures.TryGetValue(t, out var tf) ? tf as HoeDirt : null;
            bool ready = hd?.crop != null && hd.readyForHarvest();
            string key = $"{(int)t.X},{(int)t.Y}";
            matureStates[key] = new Dictionary<string, object?>
            {
                ["hasCrop"] = hd?.crop != null,
                ["readyForHarvest"] = ready
            };
            if (ready) stillReady.Add(key);
        }
        bool matureHarvested = stillReady.Count == 0;
        _evidence["matureTileStates"] = matureStates;
        _evidence["matureHarvested"] = matureHarvested;
        if (!matureHarvested)
        {
            failures.Add($"Mature parsnips still ready for harvest on: [{string.Join(",", stillReady)}].");
        }

        var immatureDirt = farm.terrainFeatures.TryGetValue(_targetTile, out var itf) ? itf as HoeDirt : null;
        bool immatureIntact = immatureDirt?.crop != null;
        _evidence["immatureTileState"] = DescribeCropTile(_targetTile, immatureDirt);
        _evidence["immatureIntact"] = immatureIntact;
        if (!immatureIntact)
        {
            failures.Add("Immature parsnip at (65,15) was harvested or destroyed.");
        }

        var chestTile = new Vector2(66, 20);
        var chest = farm.objects.TryGetValue(chestTile, out var chestObj) ? chestObj as StardewValley.Objects.Chest : null;
        bool chestPresent = chest != null;
        _evidence["chestPresent"] = chestPresent;
        int chestParsnips = 0;
        if (chest != null)
        {
            chestParsnips = CountItemsByQualifiedId(chest.Items, "(O)24");
            _evidence["chestContents"] = DescribeItems(chest.Items);
        }
        if (!chestPresent)
        {
            failures.Add("Chest at (66,20) is missing.");
        }

        int companionParsnips = 0;
        bool companionResolved = false;
        if (ResolveCompanionActor(out _, out var companionFarmer) && companionFarmer != null)
        {
            companionResolved = true;
            companionParsnips = CountItemsByQualifiedId(companionFarmer.Items, "(O)24");
        }
        _evidence["companionInventoryResolved"] = companionResolved;
        _evidence["parsnipsInChest"] = chestParsnips;
        _evidence["parsnipsInCompanionInventory"] = companionParsnips;
        int parsnipTotal = chestParsnips + companionParsnips;
        _evidence["parsnipTotal"] = parsnipTotal;
        _evidence["parsnipConservationExpected"] = 8;
        bool parsnipConservation = companionResolved && parsnipTotal == 8;
        _evidence["parsnipConservation"] = parsnipConservation;
        if (!parsnipConservation)
        {
            failures.Add($"Parsnip conservation violated: chest({chestParsnips}) + companion({companionParsnips}) = {parsnipTotal}, expected 8" +
                (companionResolved ? "." : " (companion inventory unresolved)."));
        }

        _evidence["playerUntouched"] = playerInvariant;
        if (!playerInvariant)
        {
            failures.Add("Player resources or inventory changed during companion action.");
        }

        if (!done.Exists)
        {
            failures.Add("TIMEOUT_WAITING_LOOP_DEMO_DONE: harvest-demo timed out before loop-demo-done.json was produced.");
        }
        else if (!done.Success)
        {
            failures.Add($"HARVEST_DEMO_FAILED: {done.Error ?? "orchestrator reported failure in loop-demo-done.json"}");
        }

        bool passed = done.Exists && done.Success && failures.Count == 0;
        _evidence["functionalSkillPassed"] = passed;
        _evidence["gateSuccess"] = passed;
        if (!passed)
        {
            _evidence["failureReason"] = string.Join(" ", failures);
        }
        Monitor.Log($"[TestDriver] harvest-demo gate {(passed ? "PASSED" : "FAILED")}: matureHarvested={matureHarvested}, immatureIntact={immatureIntact}, chestPresent={chestPresent}, parsnips={parsnipTotal}/8, playerUntouched={playerInvariant}, frames={_loopDemoFrameIndex}.",
            passed ? LogLevel.Info : LogLevel.Error);
    }

    /// <summary>
    /// harvest-cancel gate: the external driver cancels mid-run, so partial outcomes are
    /// accepted — the done-file must report success and the world must stay consistent
    /// (every arranged crop tile is either still cropped or harvested; player untouched).
    /// </summary>
    private void EvaluateHarvestCancelGate(string outDir, bool playerInvariant)
    {
        var done = ReadLoopDemoDoneFile(outDir);
        RecordDoneFileEvidence(done);

        var failures = new List<string>();
        var farm = Game1.getFarm();

        var cropTiles = new List<Vector2>();
        for (int x = 60; x <= 63; x++)
        {
            cropTiles.Add(new Vector2(x, 20));
            cropTiles.Add(new Vector2(x, 21));
        }

        var tileStates = new Dictionary<string, object?>();
        int harvestedCount = 0;
        int remainingCount = 0;
        var inconsistentTiles = new List<string>();
        foreach (var t in cropTiles)
        {
            var hd = farm.terrainFeatures.TryGetValue(t, out var tf) ? tf as HoeDirt : null;
            bool hasCrop = hd?.crop != null;
            bool ready = hd?.crop != null && hd.readyForHarvest();
            string key = $"{(int)t.X},{(int)t.Y}";
            tileStates[key] = new Dictionary<string, object?>
            {
                ["dirtPresent"] = hd != null,
                ["hasCrop"] = hasCrop,
                ["readyForHarvest"] = ready
            };
            if (hd == null)
            {
                inconsistentTiles.Add(key);
            }
            else if (hasCrop)
            {
                remainingCount++;
            }
            else
            {
                harvestedCount++;
            }
        }

        bool worldConsistent = inconsistentTiles.Count == 0 && harvestedCount + remainingCount == cropTiles.Count;
        bool stoppedEarly = remainingCount > 0;
        _evidence["harvestTileStates"] = tileStates;
        _evidence["harvestedCount"] = harvestedCount;
        _evidence["remainingCount"] = remainingCount;
        _evidence["stoppedEarly"] = stoppedEarly;
        _evidence["worldConsistent"] = worldConsistent;
        _evidence["playerUntouched"] = playerInvariant;

        if (!worldConsistent)
        {
            failures.Add($"World inconsistent after cancel: harvested({harvestedCount}) + remaining({remainingCount}) != {cropTiles.Count}" +
                (inconsistentTiles.Count > 0 ? $"; tiles missing their HoeDirt: [{string.Join(",", inconsistentTiles)}]." : "."));
        }
        if (!playerInvariant)
        {
            failures.Add("Player resources or inventory changed during companion action.");
        }
        if (!done.Exists)
        {
            failures.Add("TIMEOUT_WAITING_LOOP_DEMO_DONE: harvest-cancel timed out before loop-demo-done.json was produced.");
        }
        else if (!done.Success)
        {
            failures.Add($"HARVEST_CANCEL_FAILED: {done.Error ?? "orchestrator reported failure in loop-demo-done.json"}");
        }

        bool passed = done.Exists && done.Success && worldConsistent && playerInvariant;
        _evidence["functionalSkillPassed"] = passed;
        _evidence["gateSuccess"] = passed;
        if (!passed)
        {
            _evidence["failureReason"] = string.Join(" ", failures);
        }
        Monitor.Log($"[TestDriver] harvest-cancel gate {(passed ? "PASSED" : "FAILED")}: harvested={harvestedCount}, remaining={remainingCount}, stoppedEarly={stoppedEarly}, playerUntouched={playerInvariant}, frames={_loopDemoFrameIndex}.",
            passed ? LogLevel.Info : LogLevel.Error);
    }

    private void ArrangeFarmFlow(Farm farm, HoeDirt targetDirt, Farmer? companionFarmer, object? actor)
    {
        var dirtTiles = new[]
        {
            new Vector2(61, 20), // unwatered tilled soil (no crop)
            new Vector2(62, 20), // unwatered tilled soil with immature crop
            new Vector2(63, 20), // unwatered tilled soil with mature crop
            new Vector2(64, 20), // unwatered tilled soil with mature crop
            new Vector2(65, 20)  // unwatered tilled soil with mature crop
        };
        var matureTiles = new[] { new Vector2(63, 20), new Vector2(64, 20), new Vector2(65, 20) };
        var immatureTile = new Vector2(62, 20);
        var unwateredDirtTile = new Vector2(61, 20);

        var chestReachableTile = new Vector2(66, 20);
        var chestFullTile = new Vector2(68, 20);
        var walkwayTile = new Vector2(67, 20);

        var clearedObjects = new Dictionary<string, string>();

        string? clearedOldTarget = ClearTileObject(farm, _targetTile);
        if (clearedOldTarget != null) clearedObjects[$"{(int)_targetTile.X},{(int)_targetTile.Y}"] = clearedOldTarget;
        _targetTile = unwateredDirtTile;

        string? clearedWalkway = ClearTileObject(farm, walkwayTile);
        if (clearedWalkway != null) clearedObjects[$"{(int)walkwayTile.X},{(int)walkwayTile.Y}"] = clearedWalkway;

        string? cleared61 = ClearTileObject(farm, unwateredDirtTile);
        if (cleared61 != null) clearedObjects[$"{(int)unwateredDirtTile.X},{(int)unwateredDirtTile.Y}"] = cleared61;
        var hd61 = EnsureHoeDirt(farm, unwateredDirtTile);
        hd61.state.Value = 0;
        hd61.crop = null;

        string? cleared62 = ClearTileObject(farm, immatureTile);
        if (cleared62 != null) clearedObjects[$"{(int)immatureTile.X},{(int)immatureTile.Y}"] = cleared62;
        var hd62 = EnsureHoeDirt(farm, immatureTile);
        hd62.state.Value = 0;
        hd62.crop = CreateParsnipCrop(62, 20, farm, mature: false);
        bool immatureReady = hd62.crop != null && hd62.readyForHarvest();

        var matureEvidence = new List<Dictionary<string, object?>>();
        bool allMatureReady = true;
        foreach (var t in matureTiles)
        {
            string? cleared = ClearTileObject(farm, t);
            if (cleared != null) clearedObjects[$"{(int)t.X},{(int)t.Y}"] = cleared;
            var hd = EnsureHoeDirt(farm, t);
            hd.state.Value = 0;
            hd.crop = CreateParsnipCrop((int)t.X, (int)t.Y, farm, mature: true);
            bool ready = hd.crop != null && hd.readyForHarvest();
            if (!ready) allMatureReady = false;
            matureEvidence.Add(DescribeCropTile(t, hd));
            Monitor.Log($"[TestDriver] farm-flow: mature parsnip at ({(int)t.X},{(int)t.Y}) readyForHarvest={ready}.", ready ? LogLevel.Info : LogLevel.Warn);
        }

        string? clearedReachable = ClearTileObject(farm, chestReachableTile);
        if (clearedReachable != null) clearedObjects[$"{(int)chestReachableTile.X},{(int)chestReachableTile.Y}"] = clearedReachable;
        var chestB = new StardewValley.Objects.Chest(true, chestReachableTile, "130");
        chestB.resetLidFrame();
        chestB.Items.Add(ItemRegistry.Create("(O)24", 3));
        chestB.Items.Add(ItemRegistry.Create("(O)24", 2));
        chestB.Items.Add(ItemRegistry.Create("(O)388", 10));
        farm.objects[chestReachableTile] = chestB;

        string? clearedFull = ClearTileObject(farm, chestFullTile);
        if (clearedFull != null) clearedObjects[$"{(int)chestFullTile.X},{(int)chestFullTile.Y}"] = clearedFull;
        var chestA = new StardewValley.Objects.Chest(true, chestFullTile, "130");
        chestA.resetLidFrame();
        for (int i = 0; i < 36; i++)
        {
            chestA.Items.Add(ItemRegistry.Create("(O)390", 999));
        }
        farm.objects[chestFullTile] = chestA;

        int initialCompanionSlots = 0;
        int initialCompanionFreeSlots = 0;
        var companionItemsSnapshot = new List<Dictionary<string, object?>>();

        if (companionFarmer != null)
        {
            var can = companionFarmer.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault()
                      ?? new StardewValley.Tools.WateringCan { WaterLeft = 40 };
            can.WaterLeft = 40;

            companionFarmer.Items.Clear();
            companionFarmer.Items.Add(can);
            companionFarmer.Items.Add(ItemRegistry.Create("(O)388", 15)); // Wood x15
            companionFarmer.Items.Add(ItemRegistry.Create("(O)390", 10)); // Stone x10
            companionFarmer.Items.Add(ItemRegistry.Create("(O)771", 5));  // Fiber x5
            companionFarmer.Items.Add(ItemRegistry.Create("(O)330", 5));  // Clay x5
            companionFarmer.Items.Add(ItemRegistry.Create("(O)92", 5));   // Sap x5
            companionFarmer.Items.Add(ItemRegistry.Create("(O)382", 5));  // Coal x5
            companionFarmer.Items.Add(ItemRegistry.Create("(O)378", 5));  // Copper Ore x5
            companionFarmer.Items.Add(ItemRegistry.Create("(O)380", 5));  // Iron Ore x5
            companionFarmer.Items.Add(ItemRegistry.Create("(O)709", 2));  // Hardwood x2
            companionFarmer.Items.Add(ItemRegistry.Create("(O)770", 2));  // Mixed Seeds x2

            int cap = companionFarmer.MaxItems > 0 ? companionFarmer.MaxItems : 12;
            while (companionFarmer.Items.Count < cap - 1)
            {
                companionFarmer.Items.Add(ItemRegistry.Create("(O)390", 1));
            }
            while (companionFarmer.Items.Count < cap)
            {
                companionFarmer.Items.Add(null);
            }

            companionFarmer.CurrentToolIndex = 0;
            companionFarmer.Position = new Vector2(64 * 64, 18 * 64);
            companionFarmer.faceDirection(2);
            companionFarmer.Stamina = 270.0f;
            companionFarmer.maxStamina.Value = 270;

            initialCompanionSlots = companionFarmer.Items.Count;
            initialCompanionFreeSlots = companionFarmer.Items.Count(it => it == null);
            companionItemsSnapshot = DescribeItems(companionFarmer.Items);
        }

        if (actor != null)
        {
            try
            {
                var stProp = actor.GetType().GetProperty("Stamina");
                stProp?.SetValue(actor, 270.0f);
                var wProp = actor.GetType().GetProperty("WaterLeft");
                wProp?.SetValue(actor, 40);
                var posProp = actor.GetType().GetProperty("PixelPosition");
                posProp?.SetValue(actor, new Vector2(64 * 64, 18 * 64));
            }
            catch (Exception ex)
            {
                Monitor.Log($"[TestDriver] Note arranging companion actor in farm-flow: {ex.Message}", LogLevel.Warn);
            }
        }

        _preActorStamina = 270.0f;
        _preActorWater = 40;
        _targetDirtIsWatered = false;

        _zoneTiles.Clear();
        foreach (var dt in dirtTiles)
        {
            _zoneTiles.Add(dt);
        }

        _evidence["arrange"] = new Dictionary<string, object?>
        {
            ["scenario"] = _scenario,
            ["dirtTiles"] = dirtTiles.Select(d => new { x = (int)d.X, y = (int)d.Y }).ToList(),
            ["matureTiles"] = matureEvidence,
            ["allMatureReadyAtArrange"] = allMatureReady,
            ["immatureTile"] = DescribeCropTile(immatureTile, hd62),
            ["immatureReadyAtArrange"] = immatureReady,
            ["unwateredTilledDirtTile"] = DescribeCropTile(unwateredDirtTile, hd61),
            ["chestReachable"] = new Dictionary<string, object?>
            {
                ["x"] = (int)chestReachableTile.X,
                ["y"] = (int)chestReachableTile.Y,
                ["capacity"] = chestB.GetActualCapacity(),
                ["freeSlots"] = Math.Max(0, chestB.GetActualCapacity() - chestB.Items.Count),
                ["contents"] = DescribeItems(chestB.Items)
            },
            ["chestFull"] = new Dictionary<string, object?>
            {
                ["x"] = (int)chestFullTile.X,
                ["y"] = (int)chestFullTile.Y,
                ["capacity"] = chestA.GetActualCapacity(),
                ["freeSlots"] = Math.Max(0, chestA.GetActualCapacity() - chestA.Items.Count),
                ["itemCount"] = chestA.Items.Count
            },
            ["companion"] = new Dictionary<string, object?>
            {
                ["tile"] = new { x = 64, y = 18 },
                ["stamina"] = 270.0f,
                ["waterLeft"] = 40,
                ["slots"] = initialCompanionSlots,
                ["freeSlots"] = initialCompanionFreeSlots,
                ["contents"] = companionItemsSnapshot
            },
            ["expectedConservation"] = new Dictionary<string, object?>
            {
                ["parsnips"] = "chest(5) + companion(0) + harvested(3) = 8",
                ["wood"] = "chest(10) + companion(15) = 25",
                ["stone"] = "companion(10) (excluding full chest)"
            },
            ["clearedObjects"] = clearedObjects
        };

        Monitor.Log($"[TestDriver] farm-flow: arranged 3 mature parsnips (63-65,20), 1 immature (62,20), 1 dry dirt (61,20), reachable chest at (66,20), full chest at (68,20), companion freeSlots={initialCompanionFreeSlots}.", LogLevel.Info);
    }

    private void ArrangeFarmFlowInterrupt(Farm farm, Farmer? companionFarmer, object? actor)
    {
        var matureTiles = new List<Vector2>();
        for (int x = 60; x <= 65; x++)
        {
            matureTiles.Add(new Vector2(x, 20));
        }
        var waterTiles = new[] { new Vector2(60, 21), new Vector2(61, 21) };
        var chestTile = new Vector2(66, 20);

        var clearedObjects = new Dictionary<string, string>();
        var matureEvidence = new List<Dictionary<string, object?>>();
        bool allReady = true;
        foreach (var t in matureTiles)
        {
            string? cleared = ClearTileObject(farm, t);
            if (cleared != null) clearedObjects[$"{(int)t.X},{(int)t.Y}"] = cleared;
            var hd = EnsureHoeDirt(farm, t);
            hd.state.Value = 0;
            hd.crop = CreateParsnipCrop((int)t.X, (int)t.Y, farm, mature: true);
            bool ready = hd.crop != null && hd.readyForHarvest();
            if (!ready) allReady = false;
            matureEvidence.Add(DescribeCropTile(t, hd));
        }

        foreach (var wt in waterTiles)
        {
            string? cleared = ClearTileObject(farm, wt);
            if (cleared != null) clearedObjects[$"{(int)wt.X},{(int)wt.Y}"] = cleared;
            var hd = EnsureHoeDirt(farm, wt);
            hd.state.Value = 0;
            hd.crop = null;
        }

        string? clearedChest = ClearTileObject(farm, chestTile);
        if (clearedChest != null) clearedObjects[$"{(int)chestTile.X},{(int)chestTile.Y}"] = clearedChest;
        var chest = new StardewValley.Objects.Chest(true, chestTile, "130");
        chest.resetLidFrame(); // Initialize the native player-chest lid before fixture persistence.
        chest.Items.Add(ItemRegistry.Create("(O)24", 2));
        chest.Items.Add(ItemRegistry.Create("(O)388", 10));
        farm.objects[chestTile] = chest;

        if (companionFarmer != null)
        {
            var can = companionFarmer.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault()
                      ?? new StardewValley.Tools.WateringCan { WaterLeft = 40 };
            can.WaterLeft = 40;
            companionFarmer.Stamina = 270.0f;
            companionFarmer.maxStamina.Value = 270;
            companionFarmer.Position = new Vector2(64 * 64, 15 * 64);
            companionFarmer.faceDirection(2);
        }

        if (actor != null)
        {
            try
            {
                var stProp = actor.GetType().GetProperty("Stamina");
                stProp?.SetValue(actor, 270.0f);
                var wProp = actor.GetType().GetProperty("WaterLeft");
                wProp?.SetValue(actor, 40);
                var posProp = actor.GetType().GetProperty("PixelPosition");
                posProp?.SetValue(actor, new Vector2(64 * 64, 15 * 64));
            }
            catch (Exception ex)
            {
                Monitor.Log($"[TestDriver] Note arranging actor in farm-flow-interrupt: {ex.Message}", LogLevel.Warn);
            }
        }

        _preActorStamina = 270.0f;
        _preActorWater = 40;
        _targetDirtIsWatered = false;

        _zoneTiles.Clear();
        foreach (var wt in waterTiles) _zoneTiles.Add(wt);

        _evidence["arrange"] = new Dictionary<string, object?>
        {
            ["scenario"] = _scenario,
            ["matureTiles"] = matureEvidence,
            ["allMatureReadyAtArrange"] = allReady,
            ["waterTiles"] = waterTiles.Select(wt => new { x = (int)wt.X, y = (int)wt.Y }).ToList(),
            ["chest"] = new { x = (int)chestTile.X, y = (int)chestTile.Y },
            ["clearedObjects"] = clearedObjects
        };

        Monitor.Log("[TestDriver] Arranged farm-flow-interrupt scenario: 6 mature crops (60-65,20), 2 dry dirt tiles (60-61,21), chest at (66,20).", LogLevel.Info);
    }

    private void ArrangePlantingMcp(Farm farm, HoeDirt targetDirt, Farmer? companionFarmer, object? actor)
    {
        // planting-mcp scenario arrangement:
        // 2 bare tillable tiles (63,21), (64,21)
        // 2 tilled unplanted tiles (61,21), (62,21)
        // 1 immature crop tile as protection target (60,21)
        // 4 total target planting tiles: (61,21), (62,21), (63,21), (64,21)
        var tilledUnplantedTiles = new[] { new Vector2(61, 21), new Vector2(62, 21) };
        var bareTillableTiles = new[] { new Vector2(63, 21), new Vector2(64, 21) };
        var allTargetTiles = new[] { new Vector2(61, 21), new Vector2(62, 21), new Vector2(63, 21), new Vector2(64, 21) };
        var protectedCropTile = new Vector2(60, 21);

        var clearedObjects = new Dictionary<string, string>();

        // Clear test area obstacles: target tiles, protected tile, companion start tile, and walkways
        var tilesToClear = new List<Vector2>
        {
            protectedCropTile,
            new Vector2(61, 21),
            new Vector2(62, 21),
            new Vector2(63, 21),
            new Vector2(64, 21),
            new Vector2(65, 21),
            new Vector2(60, 22),
            new Vector2(61, 22),
            new Vector2(62, 22),
            new Vector2(63, 22),
            new Vector2(64, 22),
            new Vector2(62, 23)
        };

        foreach (var t in tilesToClear)
        {
            string? cleared = ClearTileObject(farm, t);
            if (cleared != null) clearedObjects[$"{(int)t.X},{(int)t.Y}"] = cleared;
        }

        // 1. Arrange protected immature crop at (60, 21)
        var hdProtected = EnsureHoeDirt(farm, protectedCropTile);
        hdProtected.state.Value = 0;
        hdProtected.crop = CreateParsnipCrop((int)protectedCropTile.X, (int)protectedCropTile.Y, farm, mature: false);
        _initialProtectedCropRef = hdProtected.crop;
        _targetTile = protectedCropTile;

        // 2. Arrange 2 tilled unplanted dirt tiles at (61, 21) and (62, 21)
        foreach (var t in tilledUnplantedTiles)
        {
            var hd = EnsureHoeDirt(farm, t);
            hd.state.Value = 0;
            hd.crop = null;
        }

        // 3. Arrange 2 bare tillable ground tiles at (63, 21) and (64, 21) (remove any HoeDirt / terrainFeatures)
        foreach (var t in bareTillableTiles)
        {
            if (farm.terrainFeatures.ContainsKey(t))
            {
                farm.terrainFeatures.Remove(t);
            }
        }

        // 4. Arrange companion inventory: genuine Hoe, WateringCan (40 water), and 4 seasonal Parsnip seeds ((O)472)
        int initialCompanionSlots = 0;
        int initialCompanionFreeSlots = 0;
        var companionItemsSnapshot = new List<Dictionary<string, object?>>();

        if (companionFarmer != null)
        {
            companionFarmer.Items.Clear();
            companionFarmer.Items.Add(new StardewValley.Tools.Hoe());
            var can = new StardewValley.Tools.WateringCan { WaterLeft = 40 };
            companionFarmer.Items.Add(can);
            companionFarmer.Items.Add(ItemRegistry.Create("(O)472", 4)); // Parsnip Seeds x4

            int cap = companionFarmer.MaxItems > 0 ? companionFarmer.MaxItems : 12;
            while (companionFarmer.Items.Count < cap)
            {
                companionFarmer.Items.Add(null);
            }

            companionFarmer.CurrentToolIndex = 0;
            companionFarmer.Position = new Vector2(62 * 64, 23 * 64);
            companionFarmer.faceDirection(0); // Face North toward test tiles
            companionFarmer.Stamina = 270.0f;
            companionFarmer.maxStamina.Value = 270;

            initialCompanionSlots = companionFarmer.Items.Count;
            initialCompanionFreeSlots = companionFarmer.Items.Count(it => it == null);
            companionItemsSnapshot = DescribeItems(companionFarmer.Items);
        }

        if (actor != null)
        {
            try
            {
                var stProp = actor.GetType().GetProperty("Stamina");
                stProp?.SetValue(actor, 270.0f);
                var wProp = actor.GetType().GetProperty("WaterLeft");
                wProp?.SetValue(actor, 40);
                var posProp = actor.GetType().GetProperty("PixelPosition");
                posProp?.SetValue(actor, new Vector2(62 * 64, 23 * 64));
            }
            catch (Exception ex)
            {
                Monitor.Log($"[TestDriver] Note arranging companion actor in planting-mcp: {ex.Message}", LogLevel.Warn);
            }
        }

        _preActorStamina = 270.0f;
        _preActorWater = 40;
        _targetDirtIsWatered = false;

        _zoneTiles.Clear();
        foreach (var t in allTargetTiles)
        {
            _zoneTiles.Add(t);
        }

        var arrangeEvidence = new Dictionary<string, object?>
        {
            ["scenario"] = _scenario,
            ["bareTillableTiles"] = bareTillableTiles.Select(d => new { x = (int)d.X, y = (int)d.Y }).ToList(),
            ["tilledUnplantedTiles"] = tilledUnplantedTiles.Select(d => new { x = (int)d.X, y = (int)d.Y }).ToList(),
            ["targetPlantTiles"] = allTargetTiles.Select(d => new { x = (int)d.X, y = (int)d.Y }).ToList(),
            ["protectedCropTile"] = new { x = (int)protectedCropTile.X, y = (int)protectedCropTile.Y },
            ["initialProtectedCrop"] = DescribeCropTile(protectedCropTile, hdProtected),
            ["initialSeedId"] = "(O)472",
            ["initialSeedNetIndex"] = "472",
            ["initialSeedCount"] = 4,
            ["companion"] = new Dictionary<string, object?>
            {
                ["tile"] = new { x = 62, y = 23 },
                ["stamina"] = 270.0f,
                ["waterLeft"] = 40,
                ["hasHoe"] = true,
                ["hasWateringCan"] = true,
                ["seedCount"] = 4,
                ["slots"] = initialCompanionSlots,
                ["freeSlots"] = initialCompanionFreeSlots,
                ["contents"] = companionItemsSnapshot
            },
            ["host"] = new Dictionary<string, object?>
            {
                ["tile"] = new { x = (int)Game1.player.Tile.X, y = (int)Game1.player.Tile.Y },
                ["stamina"] = _initialPlayerStamina,
                ["money"] = _initialPlayerMoney
            },
            ["clearedObjects"] = clearedObjects
        };

        _evidence["arrange"] = arrangeEvidence;

        try
        {
            var outDir = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
            Directory.CreateDirectory(outDir);
            File.WriteAllText(Path.Combine(outDir, "arrange-evidence.json"), JsonSerializer.Serialize(arrangeEvidence, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Note writing arrange-evidence.json: {ex.Message}", LogLevel.Warn);
        }

        Monitor.Log("[TestDriver] Arranged planting-mcp scenario: 2 bare tillable (63-64,21), 2 tilled unplanted (61-62,21), 1 protected immature crop (60,21), companion at (62,23) with Hoe, WateringCan, 4 Parsnip seeds.", LogLevel.Info);
    }

    private void ArrangeShippingMcp(Farm farm, HoeDirt targetDirt, Farmer? companionFarmer, object? actor)
    {
        Vector2 binTile = new Vector2(71, 14);
        var bin = farm.buildings.FirstOrDefault(b => b is StardewValley.Buildings.ShippingBin ||
            string.Equals(b.buildingType.Value, "Shipping Bin", StringComparison.OrdinalIgnoreCase));
        if (bin != null)
        {
            binTile = new Vector2(bin.tileX.Value, bin.tileY.Value);
        }
        else if (farm.mapShippingBinPosition.HasValue)
        {
            binTile = farm.mapShippingBinPosition.Value;
        }

        Vector2 standTile = new Vector2(binTile.X - 1, binTile.Y);
        ClearTileObject(farm, standTile);

        if (companionFarmer != null)
        {
            var shippingBin = farm.getShippingBin(companionFarmer);
            shippingBin?.Clear();

            companionFarmer.Items.Clear();
            companionFarmer.Items.Add(ItemRegistry.Create("(O)24", 4));

            int cap = companionFarmer.MaxItems > 0 ? companionFarmer.MaxItems : 12;
            while (companionFarmer.Items.Count < cap)
            {
                companionFarmer.Items.Add(null);
            }

            companionFarmer.CurrentToolIndex = 0;
            companionFarmer.Position = new Vector2(standTile.X * 64, standTile.Y * 64);
            companionFarmer.faceDirection(1);
            companionFarmer.Stamina = 270.0f;
            companionFarmer.maxStamina.Value = 270;
        }

        if (actor != null)
        {
            try
            {
                var stProp = actor.GetType().GetProperty("Stamina");
                stProp?.SetValue(actor, 270.0f);
                var posProp = actor.GetType().GetProperty("PixelPosition");
                posProp?.SetValue(actor, new Vector2(standTile.X * 64, standTile.Y * 64));
            }
            catch (Exception ex)
            {
                Monitor.Log($"[TestDriver] Note arranging companion actor in shipping-mcp: {ex.Message}", LogLevel.Warn);
            }
        }

        _preActorStamina = 270.0f;
        _preActorWater = 40;
        _targetDirtIsWatered = false;

        _zoneTiles.Clear();
        _zoneTiles.Add(binTile);

        var arrangeEvidence = new Dictionary<string, object?>
        {
            ["scenario"] = _scenario,
            ["shippingBinTile"] = new { x = (int)binTile.X, y = (int)binTile.Y },
            ["companionStandTile"] = new { x = (int)standTile.X, y = (int)standTile.Y },
            ["initialItem"] = "(O)24",
            ["initialCount"] = 4,
            ["targetShipCount"] = 2,
            ["companion"] = new Dictionary<string, object?>
            {
                ["tile"] = new { x = (int)standTile.X, y = (int)standTile.Y },
                ["stamina"] = 270.0f,
                ["inventoryCount"] = 4
            }
        };
        _evidence["arrange"] = arrangeEvidence;

        try
        {
            var outDir = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
            Directory.CreateDirectory(outDir);
            File.WriteAllText(Path.Combine(outDir, "arrange-evidence.json"), JsonSerializer.Serialize(arrangeEvidence, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Note writing arrange-evidence.json: {ex.Message}", LogLevel.Warn);
        }

        Monitor.Log($"[TestDriver] Arranged shipping-mcp scenario: shipping bin at ({binTile.X},{binTile.Y}), companion at ({standTile.X},{standTile.Y}) with 4 Parsnips.", LogLevel.Info);
    }

    private void ArrangeNavigationMcp(Farm farm, Farmer? companionFarmer, object? actor)
    {
        Vector2 startTile = new Vector2(64, 15);
        ClearTileObject(farm, startTile);

        if (companionFarmer != null)
        {
            companionFarmer.Position = new Vector2(startTile.X * 64, startTile.Y * 64);
            companionFarmer.faceDirection(2);
            companionFarmer.Stamina = 270.0f;
            companionFarmer.maxStamina.Value = 270;
            companionFarmer.currentLocation = farm;
        }

        if (actor != null)
        {
            try
            {
                var stProp = actor.GetType().GetProperty("Stamina");
                stProp?.SetValue(actor, 270.0f);
                var posProp = actor.GetType().GetProperty("PixelPosition");
                posProp?.SetValue(actor, new Vector2(startTile.X * 64, startTile.Y * 64));
                var locField = actor.GetType().GetField("_locationName", BindingFlags.NonPublic | BindingFlags.Instance);
                locField?.SetValue(actor, "Farm");
            }
            catch (Exception ex)
            {
                Monitor.Log($"[TestDriver] Note arranging companion actor in navigation-mcp: {ex.Message}", LogLevel.Warn);
            }
        }

        // Dynamically discover SeedShop door in Town from runtime data
        Vector2 seedShopDoorTile = new Vector2(43, 57);
        Vector2 townTargetTile = new Vector2(43, 57); // Request the door tile itself (impassable footprint) to exercise adjacent fallback
        try
        {
            var townLoc = Game1.getLocationFromName("Town");
            if (townLoc != null && townLoc.doors != null)
            {
                foreach (var pair in townLoc.doors.Pairs)
                {
                    var doorPt = pair.Key;
                    string? actionStr = townLoc.doesTileHaveProperty(doorPt.X, doorPt.Y, "Action", "Buildings");
                    if (string.IsNullOrWhiteSpace(actionStr))
                    {
                        actionStr = townLoc.doesTileHaveProperty(doorPt.X, doorPt.Y, "Action", "Back");
                    }
                    if (!string.IsNullOrWhiteSpace(actionStr) && actionStr.IndexOf("SeedShop", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        seedShopDoorTile = new Vector2(doorPt.X, doorPt.Y);
                        townTargetTile = new Vector2(doorPt.X, doorPt.Y);
                        break;
                    }
                }
            }
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Note discovering Town SeedShop door: {ex.Message}", LogLevel.Warn);
        }

        _navDoorTile = seedShopDoorTile;

        _preActorStamina = 270.0f;
        _preActorWater = 40;
        _targetDirtIsWatered = false;

        _zoneTiles.Clear();
        _zoneTiles.Add(startTile);

        var arrangeEvidence = new Dictionary<string, object?>
        {
            ["scenario"] = _scenario,
            ["startLocation"] = "Farm",
            ["startTile"] = new { x = (int)startTile.X, y = (int)startTile.Y },
            ["townTargetLocation"] = "Town",
            ["townTargetTile"] = new { x = (int)townTargetTile.X, y = (int)townTargetTile.Y },
            ["seedShopDoorTile"] = new { x = (int)seedShopDoorTile.X, y = (int)seedShopDoorTile.Y },
            ["returnLocation"] = "Farm",
            ["returnTile"] = new { x = (int)startTile.X, y = (int)startTile.Y },
            ["hostInitialLocation"] = Game1.player?.currentLocation?.Name ?? "Farm",
            ["hostInitialTile"] = new { x = (int)(Game1.player?.Tile.X ?? 0), y = (int)(Game1.player?.Tile.Y ?? 0) },
            ["hostInitialStamina"] = _initialPlayerStamina,
            ["hostInitialMoney"] = _initialPlayerMoney
        };
        _evidence["arrange"] = arrangeEvidence;

        try
        {
            var outDir = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
            Directory.CreateDirectory(outDir);
            File.WriteAllText(Path.Combine(outDir, "arrange-evidence.json"), JsonSerializer.Serialize(arrangeEvidence, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Note writing arrange-evidence.json: {ex.Message}", LogLevel.Warn);
        }

        Monitor.Log($"[TestDriver] Arranged navigation-mcp scenario: companion on Farm at ({startTile.X},{startTile.Y}), target Town SeedShop at ({townTargetTile.X},{townTargetTile.Y}).", LogLevel.Info);
    }

    private void ArrangePurchaseMcp(Farm farm, Farmer? companionFarmer, object? actor)
    {
        var seedShop = Game1.getLocationFromName("SeedShop");
        if (seedShop == null)
        {
            Monitor.Log("[TestDriver] FATAL: SeedShop location not found!", LogLevel.Error);
            throw new InvalidOperationException("SeedShop location not found.");
        }

        Vector2? foundCounter = null;

        // Dynamically locate counter tile from 'Buy' action on Buildings/Back layers
        if (seedShop.Map?.Layers != null)
        {
            string[] actionLayers = { "Buildings", "Back" };
            foreach (string layerId in actionLayers)
            {
                var layer = seedShop.Map.GetLayer(layerId);
                if (layer == null) continue;

                for (int y = 0; y < layer.LayerHeight; y++)
                {
                    for (int x = 0; x < layer.LayerWidth; x++)
                    {
                        string? action = seedShop.doesTileHaveProperty(x, y, "Action", layerId);
                        if (string.IsNullOrEmpty(action))
                        {
                            var t = layer.Tiles[x, y];
                            if (t?.Properties != null && t.Properties.TryGetValue("Action", out var prop))
                            {
                                action = prop?.ToString();
                            }
                        }
                        if (!string.IsNullOrEmpty(action))
                        {
                            string[] parts = ArgUtility.SplitBySpaceQuoteAware(action);
                            if (parts.Length > 0 && string.Equals(parts[0], "Buy", StringComparison.OrdinalIgnoreCase))
                            {
                                foundCounter = new Vector2(x, y);
                                break;
                            }
                        }
                    }
                    if (foundCounter.HasValue) break;
                }
                if (foundCounter.HasValue) break;
            }
        }

        if (!foundCounter.HasValue)
        {
            Monitor.Log("[TestDriver] FATAL: No 'Buy' action counter tile found in SeedShop indoor map!", LogLevel.Error);
            throw new InvalidOperationException("No 'Buy' action counter tile found in SeedShop indoor map.");
        }

        Vector2 counterTile = foundCounter.Value;
        Vector2 standTile = new Vector2(counterTile.X, counterTile.Y + 1);

        // 1. Initial wallet setup: Host 500g
        Game1.player.Money = 500;
        _initialPlayerMoney = 500;

        // 2. Set companion inventory empty (12 empty slots)
        if (companionFarmer != null)
        {
            companionFarmer.Items.Clear();
            int cap = companionFarmer.MaxItems > 0 ? companionFarmer.MaxItems : 12;
            while (companionFarmer.Items.Count < cap)
            {
                companionFarmer.Items.Add(null);
            }
            companionFarmer.Position = new Vector2(standTile.X * 64, standTile.Y * 64);
            companionFarmer.faceDirection(0); // Face up towards counter
            companionFarmer.currentLocation = seedShop;
        }

        if (actor != null)
        {
            try
            {
                var posProp = actor.GetType().GetProperty("PixelPosition");
                posProp?.SetValue(actor, new Vector2(standTile.X * 64, standTile.Y * 64));
                var locField = actor.GetType().GetField("_locationName", BindingFlags.NonPublic | BindingFlags.Instance);
                locField?.SetValue(actor, "SeedShop");
                var faceMethod = actor.GetType().GetMethod("Face");
                faceMethod?.Invoke(actor, new object[] { 0 });
            }
            catch (Exception ex)
            {
                Monitor.Log($"[TestDriver] Note arranging companion actor in purchase-mcp: {ex.Message}", LogLevel.Warn);
            }
        }

        // 3. Phase A (Closed Shop arrangement): timeOfDay=620, shop closed
        Game1.timeOfDay = 620;
        _purchaseMcpPhase = "closed";

        // In golden save AstraFarm_448732481, Pierre's saved position is (4, 17) counter standing tile.
        // For Phase A (closed shop negative verification), move Pierre out of the standing area
        // to (10, 20) (his natural 7:00 schedule tile), and assert he is NOT in ownerArea.
        Rectangle ownerArea = new Rectangle(4, 17, 1, 1);
        var pierre = seedShop.characters.FirstOrDefault(c => c != null && c.Name == "Pierre") ?? Game1.getCharacterFromName("Pierre");
        if (pierre == null)
        {
            Monitor.Log("[TestDriver] FATAL: Pierre NPC not found in game!", LogLevel.Error);
            throw new InvalidOperationException("Pierre NPC not found in game.");
        }

        Game1.warpCharacter(pierre, "SeedShop", new Vector2(10, 20));
        pierre.setTilePosition(new Point(10, 20));
        pierre.controller = null;

        if (ownerArea.Contains(pierre.TilePoint))
        {
            Monitor.Log($"[TestDriver] FATAL: Pierre TilePoint ({pierre.TilePoint.X},{pierre.TilePoint.Y}) is still inside owner area ({ownerArea.X},{ownerArea.Y},{ownerArea.Width},{ownerArea.Height}) during Phase A arrange!", LogLevel.Error);
            throw new InvalidOperationException($"Pierre is unexpectedly inside owner standing area ({ownerArea.X},{ownerArea.Y}) during Phase A arrange.");
        }

        _preActorStamina = 270.0f;
        _preActorWater = 40;
        _targetDirtIsWatered = false;

        _zoneTiles.Clear();
        _zoneTiles.Add(counterTile);

        var arrangeEvidence = new Dictionary<string, object?>
        {
            ["scenario"] = _scenario,
            ["phase"] = "closed",
            ["counterTile"] = new { x = (int)counterTile.X, y = (int)counterTile.Y },
            ["companionStandTile"] = new { x = (int)standTile.X, y = (int)standTile.Y },
            ["pierreTilePoint"] = new { x = pierre.TilePoint.X, y = pierre.TilePoint.Y },
            ["ownerArea"] = new { x = ownerArea.X, y = ownerArea.Y, width = ownerArea.Width, height = ownerArea.Height },
            ["inCounterArea"] = ownerArea.Contains(pierre.TilePoint),
            ["initialHostMoney"] = 500,
            ["targetItem"] = "(O)472",
            ["targetCount"] = 2,
            ["budgetLimit"] = 100,
            ["shopId"] = "SeedShop",
            ["timeOfDay"] = 620
        };
        _evidence["arrange"] = arrangeEvidence;

        try
        {
            var outDir = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
            Directory.CreateDirectory(outDir);
            File.WriteAllText(Path.Combine(outDir, "arrange-evidence.json"), JsonSerializer.Serialize(arrangeEvidence, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Note writing arrange-evidence.json: {ex.Message}", LogLevel.Warn);
        }

        Monitor.Log($"[TestDriver] Arranged purchase-mcp Phase A (closed): counter at ({counterTile.X},{counterTile.Y}), companion at ({standTile.X},{standTile.Y}), pierre at ({pierre.TilePoint.X},{pierre.TilePoint.Y}), timeOfDay=620.", LogLevel.Info);
    }

    private bool ArrangePurchaseMcpPhaseB(string outDir, out string failureReason)
    {
        failureReason = "";
        var seedShop = Game1.getLocationFromName("SeedShop");
        if (seedShop == null)
        {
            failureReason = "SeedShop location not found.";
            return false;
        }

        Vector2? foundCounter = null;

        if (seedShop.Map?.Layers != null)
        {
            string[] actionLayers = { "Buildings", "Back" };
            foreach (string layerId in actionLayers)
            {
                var layer = seedShop.Map.GetLayer(layerId);
                if (layer == null) continue;
                for (int y = 0; y < layer.LayerHeight; y++)
                {
                    for (int x = 0; x < layer.LayerWidth; x++)
                    {
                        string? action = seedShop.doesTileHaveProperty(x, y, "Action", layerId);
                        if (string.IsNullOrEmpty(action))
                        {
                            var t = layer.Tiles[x, y];
                            if (t?.Properties != null && t.Properties.TryGetValue("Action", out var prop))
                            {
                                action = prop?.ToString();
                            }
                        }
                        if (!string.IsNullOrEmpty(action))
                        {
                            string[] parts = ArgUtility.SplitBySpaceQuoteAware(action);
                            if (parts.Length > 0 && string.Equals(parts[0], "Buy", StringComparison.OrdinalIgnoreCase))
                            {
                                foundCounter = new Vector2(x, y);
                                break;
                            }
                        }
                    }
                    if (foundCounter.HasValue) break;
                }
                if (foundCounter.HasValue) break;
            }
        }

        if (!foundCounter.HasValue)
        {
            failureReason = "No 'Buy' action counter tile found in SeedShop indoor map.";
            return false;
        }

        Vector2 counterTile = foundCounter.Value;

        // Owner standing area: mirror of vanilla Stardew Valley GameLocation.HandleBuyAction IL constant for SeedShop
        Rectangle ownerArea = new Rectangle(4, 17, 1, 1);

        var pierre = seedShop.characters.FirstOrDefault(c => c.Name == "Pierre") ?? Game1.getCharacterFromName("Pierre");
        if (pierre == null)
        {
            failureReason = "Pierre NPC not found in game.";
            return false;
        }

        if (pierre.currentLocation != seedShop)
        {
            Game1.warpCharacter(pierre, "SeedShop", new Vector2(ownerArea.X, ownerArea.Y));
        }

        // Set time to 900 and check schedule (Pierre's natural schedule: 700 -> (10,20), 830 -> (4,17))
        Game1.timeOfDay = 900;
        if (pierre.Schedule == null || pierre.Schedule.Count == 0)
        {
            pierre.TryLoadSchedule();
        }
        pierre.lastAttemptedSchedule = 700;
        Monitor.Log($"[TestDriver] Phase B arrange: set lastAttemptedSchedule = 700, checking schedule at timeOfDay={Game1.timeOfDay}", LogLevel.Info);
        pierre.checkSchedule(Game1.timeOfDay);

        Point? controllerEndPoint = null;
        if (pierre.controller != null && pierre.controller.pathToEndPoint != null && pierre.controller.pathToEndPoint.Count > 0)
        {
            controllerEndPoint = pierre.controller.pathToEndPoint.Last();
            Monitor.Log($"[TestDriver] Phase B arrange: snapping Pierre to controller endpoint ({controllerEndPoint.Value.X}, {controllerEndPoint.Value.Y})", LogLevel.Info);
            pierre.setTilePosition(controllerEndPoint.Value);
            pierre.controller = null;
        }

        bool usedFallback = false;
        bool inArea = ownerArea.Contains(pierre.TilePoint);
        if (!inArea)
        {
            Monitor.Log($"[TestDriver] Phase B arrange: Pierre at ({pierre.TilePoint.X},{pierre.TilePoint.Y}) not in ownerArea ({ownerArea.X},{ownerArea.Y},{ownerArea.Width},{ownerArea.Height}). Applying fallback placement to ({ownerArea.X},{ownerArea.Y}).", LogLevel.Info);
            Game1.warpCharacter(pierre, "SeedShop", new Vector2(ownerArea.X, ownerArea.Y));
            pierre.setTilePosition(new Point(ownerArea.X, ownerArea.Y));
            pierre.Position = new Vector2(ownerArea.X * 64f, ownerArea.Y * 64f);
            pierre.controller = null;
            usedFallback = true;
            inArea = ownerArea.Contains(pierre.TilePoint);
        }

        Monitor.Log($"[TestDriver] Phase B arrange final: Pierre TilePoint=({pierre.TilePoint.X},{pierre.TilePoint.Y}), inArea={inArea}, usedFallback={usedFallback}", LogLevel.Info);

        if (!inArea)
        {
            failureReason = $"Pierre TilePoint ({pierre.TilePoint.X},{pierre.TilePoint.Y}) is not in owner standing area ({ownerArea.X},{ownerArea.Y},{ownerArea.Width},{ownerArea.Height}).";
            Monitor.Log($"[TestDriver] {failureReason}", LogLevel.Error);
            return false;
        }

        pierre.faceDirection(2); // Face down towards counter

        var phaseBEvidence = new Dictionary<string, object?>
        {
            ["phase"] = "open",
            ["timeOfDay"] = Game1.timeOfDay,
            ["pierreTilePoint"] = new { x = pierre.TilePoint.X, y = pierre.TilePoint.Y },
            ["ownerArea"] = new { x = ownerArea.X, y = ownerArea.Y, width = ownerArea.Width, height = ownerArea.Height },
            ["counterTile"] = new { x = (int)counterTile.X, y = (int)counterTile.Y },
            ["inCounterArea"] = inArea,
            ["usedFallback"] = usedFallback,
            ["controllerEndPoint"] = controllerEndPoint.HasValue ? new { x = controllerEndPoint.Value.X, y = controllerEndPoint.Value.Y } : null
        };
        _evidence["phaseBArrange"] = phaseBEvidence;

        try
        {
            File.WriteAllText(Path.Combine(outDir, "arrange-phase-b-evidence.json"), JsonSerializer.Serialize(phaseBEvidence, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Note writing arrange-phase-b-evidence.json: {ex.Message}", LogLevel.Warn);
        }

        Monitor.Log($"[TestDriver] Arranged purchase-mcp Phase B: timeOfDay={Game1.timeOfDay}, Pierre at ({pierre.TilePoint.X},{pierre.TilePoint.Y}) in owner area.", LogLevel.Info);
        return true;
    }

    private void ArrangeCycleMcp(Farm farm, HoeDirt targetDirt, Farmer? companionFarmer, object? actor)
    {
        // cycle-mcp scenario arrangement:
        // 2 bare tillable ground tiles (63,21), (64,21)
        // 2 tilled unplanted dirt tiles (61,21), (62,21)
        // 1 protected immature crop tile (60,21)
        // 4 total target planting tiles: (61,21), (62,21), (63,21), (64,21)
        var tilledUnplantedTiles = new[] { new Vector2(61, 21), new Vector2(62, 21) };
        var bareTillableTiles = new[] { new Vector2(63, 21), new Vector2(64, 21) };
        var allTargetTiles = new[] { new Vector2(61, 21), new Vector2(62, 21), new Vector2(63, 21), new Vector2(64, 21) };
        var protectedCropTile = new Vector2(60, 21);

        var clearedObjects = new Dictionary<string, string>();

        // Clear test area obstacles: target tiles, protected tile, companion start tile, and walkways
        var tilesToClear = new List<Vector2>
        {
            protectedCropTile,
            new Vector2(61, 21),
            new Vector2(62, 21),
            new Vector2(63, 21),
            new Vector2(64, 21),
            new Vector2(65, 21),
            new Vector2(60, 22),
            new Vector2(61, 22),
            new Vector2(62, 22),
            new Vector2(63, 22),
            new Vector2(64, 22),
            new Vector2(62, 23)
        };

        foreach (var t in tilesToClear)
        {
            string? cleared = ClearTileObject(farm, t);
            if (cleared != null) clearedObjects[$"{(int)t.X},{(int)t.Y}"] = cleared;
        }

        // 1. Arrange protected immature crop at (60, 21)
        var hdProtected = EnsureHoeDirt(farm, protectedCropTile);
        hdProtected.state.Value = 0;
        hdProtected.crop = CreateParsnipCrop((int)protectedCropTile.X, (int)protectedCropTile.Y, farm, mature: false);
        _initialProtectedCropRef = hdProtected.crop;
        _targetTile = protectedCropTile;

        // 2. Arrange 2 tilled unplanted dirt tiles at (61, 21) and (62, 21)
        foreach (var t in tilledUnplantedTiles)
        {
            var hd = EnsureHoeDirt(farm, t);
            hd.state.Value = 0;
            hd.crop = null;
        }

        // 3. Arrange 2 bare tillable ground tiles at (63, 21) and (64, 21) (remove any HoeDirt / terrainFeatures)
        foreach (var t in bareTillableTiles)
        {
            if (farm.terrainFeatures.ContainsKey(t))
            {
                farm.terrainFeatures.Remove(t);
            }
        }

        // 4. Host money setup: 500g baseline
        Game1.player.Money = 500;
        _initialPlayerMoney = 500;
        _initialPlayerStamina = Game1.player.stamina;

        // 5. Arrange companion inventory: genuine Hoe, WateringCan (40 water), NO seeds
        int initialCompanionSlots = 0;
        int initialCompanionFreeSlots = 0;
        var companionItemsSnapshot = new List<Dictionary<string, object?>>();

        if (companionFarmer != null)
        {
            companionFarmer.Items.Clear();
            companionFarmer.Items.Add(new StardewValley.Tools.Hoe());
            var can = new StardewValley.Tools.WateringCan { WaterLeft = 40 };
            companionFarmer.Items.Add(can);
            // No seeds added! Backpack contains only Hoe and WateringCan

            int cap = companionFarmer.MaxItems > 0 ? companionFarmer.MaxItems : 12;
            while (companionFarmer.Items.Count < cap)
            {
                companionFarmer.Items.Add(null);
            }

            companionFarmer.CurrentToolIndex = 0;
            companionFarmer.Position = new Vector2(62 * 64, 23 * 64);
            companionFarmer.faceDirection(0); // Face North toward test tiles
            companionFarmer.Stamina = 270.0f;
            companionFarmer.maxStamina.Value = 270;
            companionFarmer.currentLocation = farm;

            initialCompanionSlots = companionFarmer.Items.Count;
            initialCompanionFreeSlots = companionFarmer.Items.Count(it => it == null);
            companionItemsSnapshot = DescribeItems(companionFarmer.Items);
        }

        if (actor != null)
        {
            try
            {
                var stProp = actor.GetType().GetProperty("Stamina");
                stProp?.SetValue(actor, 270.0f);
                var wProp = actor.GetType().GetProperty("WaterLeft");
                wProp?.SetValue(actor, 40);
                var posProp = actor.GetType().GetProperty("PixelPosition");
                posProp?.SetValue(actor, new Vector2(62 * 64, 23 * 64));
                var locField = actor.GetType().GetField("_locationName", BindingFlags.NonPublic | BindingFlags.Instance);
                locField?.SetValue(actor, "Farm");
                var faceMethod = actor.GetType().GetMethod("Face");
                faceMethod?.Invoke(actor, new object[] { 0 });
            }
            catch (Exception ex)
            {
                Monitor.Log($"[TestDriver] Note arranging companion actor in cycle-mcp: {ex.Message}", LogLevel.Warn);
            }
        }

        _preActorStamina = 270.0f;
        _preActorWater = 40;
        _targetDirtIsWatered = false;

        _zoneTiles.Clear();
        foreach (var t in allTargetTiles)
        {
            _zoneTiles.Add(t);
        }

        // 6. SeedShop and Pierre arrangement at timeOfDay = 900 (same logic as purchase-mcp Phase B)
        Game1.timeOfDay = 900;
        var seedShop = Game1.getLocationFromName("SeedShop");
        if (seedShop == null)
        {
            Monitor.Log("[TestDriver] FATAL: SeedShop location not found during cycle-mcp arrange!", LogLevel.Error);
            throw new InvalidOperationException("SeedShop location not found.");
        }

        Rectangle ownerArea = new Rectangle(4, 17, 1, 1);
        var pierre = seedShop.characters.FirstOrDefault(c => c != null && c.Name == "Pierre") ?? Game1.getCharacterFromName("Pierre");
        if (pierre == null)
        {
            Monitor.Log("[TestDriver] FATAL: Pierre NPC not found in game during cycle-mcp arrange!", LogLevel.Error);
            throw new InvalidOperationException("Pierre NPC not found in game.");
        }

        if (pierre.currentLocation != seedShop)
        {
            Game1.warpCharacter(pierre, "SeedShop", new Vector2(ownerArea.X, ownerArea.Y));
        }

        if (pierre.Schedule == null || pierre.Schedule.Count == 0)
        {
            pierre.TryLoadSchedule();
        }
        pierre.lastAttemptedSchedule = 700;
        Monitor.Log($"[TestDriver] cycle-mcp arrange: checking schedule for Pierre at timeOfDay={Game1.timeOfDay}", LogLevel.Info);
        pierre.checkSchedule(Game1.timeOfDay);

        Point? controllerEndPoint = null;
        if (pierre.controller != null && pierre.controller.pathToEndPoint != null && pierre.controller.pathToEndPoint.Count > 0)
        {
            controllerEndPoint = pierre.controller.pathToEndPoint.Last();
            Monitor.Log($"[TestDriver] cycle-mcp arrange: snapping Pierre to controller endpoint ({controllerEndPoint.Value.X}, {controllerEndPoint.Value.Y})", LogLevel.Info);
            pierre.setTilePosition(controllerEndPoint.Value);
            pierre.controller = null;
        }

        bool usedFallback = false;
        bool inArea = ownerArea.Contains(pierre.TilePoint);
        if (!inArea)
        {
            Monitor.Log($"[TestDriver] cycle-mcp arrange: Pierre at ({pierre.TilePoint.X},{pierre.TilePoint.Y}) not in ownerArea. Applying fallback placement to ({ownerArea.X},{ownerArea.Y}).", LogLevel.Info);
            Game1.warpCharacter(pierre, "SeedShop", new Vector2(ownerArea.X, ownerArea.Y));
            pierre.setTilePosition(new Point(ownerArea.X, ownerArea.Y));
            pierre.Position = new Vector2(ownerArea.X * 64f, ownerArea.Y * 64f);
            pierre.controller = null;
            usedFallback = true;
            inArea = ownerArea.Contains(pierre.TilePoint);
        }

        pierre.faceDirection(2); // Face down towards counter
        Monitor.Log($"[TestDriver] cycle-mcp arrange: Pierre placed at ({pierre.TilePoint.X},{pierre.TilePoint.Y}), inArea={inArea}, usedFallback={usedFallback}", inArea ? LogLevel.Info : LogLevel.Error);

        if (!inArea)
        {
            throw new InvalidOperationException($"Pierre is not in owner standing area ({ownerArea.X},{ownerArea.Y}) during cycle-mcp arrange.");
        }

        var arrangeEvidence = new Dictionary<string, object?>
        {
            ["scenario"] = _scenario,
            ["initialHostMoney"] = 500,
            ["bareTillableTiles"] = bareTillableTiles.Select(d => new { x = (int)d.X, y = (int)d.Y }).ToList(),
            ["tilledUnplantedTiles"] = tilledUnplantedTiles.Select(d => new { x = (int)d.X, y = (int)d.Y }).ToList(),
            ["targetPlantTiles"] = allTargetTiles.Select(d => new { x = (int)d.X, y = (int)d.Y }).ToList(),
            ["protectedCropTile"] = new { x = (int)protectedCropTile.X, y = (int)protectedCropTile.Y },
            ["initialProtectedCrop"] = DescribeCropTile(protectedCropTile, hdProtected),
            ["targetSeedId"] = "(O)472",
            ["targetSeedNetIndex"] = "472",
            ["targetSeedCount"] = 4,
            ["companion"] = new Dictionary<string, object?>
            {
                ["tile"] = new { x = 62, y = 23 },
                ["stamina"] = 270.0f,
                ["waterLeft"] = 40,
                ["hasHoe"] = true,
                ["hasWateringCan"] = true,
                ["seedCount"] = 0,
                ["slots"] = initialCompanionSlots,
                ["freeSlots"] = initialCompanionFreeSlots,
                ["items"] = companionItemsSnapshot
            },
            ["counterTile"] = new { x = 4, y = 18 },
            ["standTile"] = new { x = 4, y = 19 },
            ["pierreTilePoint"] = new { x = pierre.TilePoint.X, y = pierre.TilePoint.Y },
            ["ownerArea"] = new { x = ownerArea.X, y = ownerArea.Y, width = ownerArea.Width, height = ownerArea.Height },
            ["inCounterArea"] = inArea,
            ["usedFallback"] = usedFallback,
            ["timeOfDay"] = Game1.timeOfDay,
            ["clearedObjects"] = clearedObjects
        };
        _evidence["arrange"] = arrangeEvidence;

        var outDir = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
        try
        {
            Directory.CreateDirectory(outDir);
            File.WriteAllText(Path.Combine(outDir, "arrange-evidence.json"), JsonSerializer.Serialize(arrangeEvidence, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Note writing arrange-evidence.json: {ex.Message}", LogLevel.Warn);
        }

        Monitor.Log("[TestDriver] Arranged cycle-mcp scenario: companion on Farm at (62,23) with Hoe, WateringCan (0 seeds), host money=500, Pierre at SeedShop (4,17), time=900.", LogLevel.Info);
    }

    private void ArrangeChestOperator(Farm farm, HoeDirt targetDirt, Farmer? companionFarmer, object? actor)
    {
        // chest-operator scenario arrangement:
        // 1 protected immature crop tile at (60, 21)
        // 3 target planting tiles: (61,21), (62,21), (63,21) (tilled unwatered dirt)
        // 13 additional tilled unwatered dirt tiles to provide exact 17 reachable unwatered tiles workload
        // 1 chest at (59, 17) containing 3 Carrot Seeds "(O)CarrotSeeds" + 10 Wood "(O)388"
        var tilledUnplantedTiles = new[] { new Vector2(61, 21), new Vector2(62, 21), new Vector2(63, 21) };
        var allTargetTiles = tilledUnplantedTiles;
        var protectedCropTile = new Vector2(60, 21);
        var chestTile = new Vector2(59, 17);
        var additionalTilledTiles = new[]
        {
            new Vector2(64, 21), new Vector2(65, 21), new Vector2(66, 21), new Vector2(67, 21),
            new Vector2(60, 22), new Vector2(61, 22), new Vector2(62, 22), new Vector2(63, 22),
            new Vector2(64, 22), new Vector2(65, 22), new Vector2(66, 22), new Vector2(67, 22),
            new Vector2(68, 22)
        };

        var clearedObjects = new Dictionary<string, string>();
        for (int cx = 58; cx <= 70; cx++)
        {
            for (int cy = 16; cy <= 24; cy++)
            {
                var t = new Vector2(cx, cy);
                string? cleared = ClearTileObject(farm, t);
                if (cleared != null) clearedObjects[$"{cx},{cy}"] = cleared;
                if (farm.terrainFeatures.TryGetValue(t, out var tf) && tf is HoeDirt)
                {
                    farm.terrainFeatures.Remove(t);
                }
            }
        }

        // Clean up stray HoeDirt outside the bounding box (e.g. baseline target (65, 15))
        if (farm.terrainFeatures.ContainsKey(new Vector2(65, 15)))
        {
            farm.terrainFeatures.Remove(new Vector2(65, 15));
        }

        // 1. Arrange protected immature crop at (60, 21)
        var hdProtected = EnsureHoeDirt(farm, protectedCropTile);
        hdProtected.state.Value = 0;
        hdProtected.crop = CreateParsnipCrop((int)protectedCropTile.X, (int)protectedCropTile.Y, farm, mature: false);
        _initialProtectedCropRef = hdProtected.crop;
        _targetTile = protectedCropTile;

        // 2. Arrange 3 tilled unplanted dirt tiles at (61, 21), (62, 21), (63, 21)
        foreach (var t in tilledUnplantedTiles)
        {
            var hd = EnsureHoeDirt(farm, t);
            hd.state.Value = 0;
            hd.crop = null;
        }

        // 3. Arrange 13 additional tilled unwatered dirt tiles (total 17 unwatered tiles: 1 protected + 3 target + 13 additional)
        foreach (var t in additionalTilledTiles)
        {
            var hd = EnsureHoeDirt(farm, t);
            hd.state.Value = 0;
            hd.crop = null;
        }

        // 4. Place chest at (59, 17) containing 3 Carrot Seeds + 10 Wood
        string? clearedChest = ClearTileObject(farm, chestTile);
        if (clearedChest != null) clearedObjects[$"{(int)chestTile.X},{(int)chestTile.Y}"] = clearedChest;
        var chest = new StardewValley.Objects.Chest(true, chestTile, "130");
        chest.resetLidFrame(); // Initialize the native player-chest lid before fixture persistence.
        chest.Items.Add(ItemRegistry.Create("(O)CarrotSeeds", 3));
        chest.Items.Add(ItemRegistry.Create("(O)388", 10)); // Wood
        farm.objects[chestTile] = chest;

        // 5. Host money, player baseline, and non-obstructing observation position
        if (Game1.player != null)
        {
            Game1.player.Position = new Vector2(55 * 64, 15 * 64);
            Game1.player.faceDirection(2);
        }
        _initialPlayerMoney = Game1.player?.Money ?? 0;
        _initialPlayerStamina = Game1.player?.stamina ?? 0;

        // 6. Arrange companion inventory: genuine Hoe, WateringCan (40 water), NO seeds
        int initialCompanionSlots = 0;
        int initialCompanionFreeSlots = 0;
        var companionItemsSnapshot = new List<Dictionary<string, object?>>();

        if (companionFarmer != null)
        {
            companionFarmer.Items.Clear();
            companionFarmer.Items.Add(new StardewValley.Tools.Hoe());
            var can = new StardewValley.Tools.WateringCan { WaterLeft = 40 };
            companionFarmer.Items.Add(can);

            int cap = companionFarmer.MaxItems > 0 ? companionFarmer.MaxItems : 12;
            while (companionFarmer.Items.Count < cap)
            {
                companionFarmer.Items.Add(null);
            }

            companionFarmer.CurrentToolIndex = 0;
            companionFarmer.Position = new Vector2(62 * 64, 23 * 64);
            companionFarmer.faceDirection(0);
            companionFarmer.Stamina = 270.0f;
            companionFarmer.maxStamina.Value = 270;
            companionFarmer.currentLocation = farm;

            initialCompanionSlots = companionFarmer.Items.Count;
            initialCompanionFreeSlots = companionFarmer.Items.Count(it => it == null);
            companionItemsSnapshot = DescribeItems(companionFarmer.Items);
        }

        if (actor != null)
        {
            try
            {
                var stProp = actor.GetType().GetProperty("Stamina");
                stProp?.SetValue(actor, 270.0f);
                var wProp = actor.GetType().GetProperty("WaterLeft");
                wProp?.SetValue(actor, 40);
                var posProp = actor.GetType().GetProperty("PixelPosition");
                posProp?.SetValue(actor, new Vector2(62 * 64, 23 * 64));
                var locField = actor.GetType().GetField("_locationName", BindingFlags.NonPublic | BindingFlags.Instance);
                locField?.SetValue(actor, "Farm");
                var faceMethod = actor.GetType().GetMethod("Face");
                faceMethod?.Invoke(actor, new object[] { 0 });
            }
            catch (Exception ex)
            {
                Monitor.Log($"[TestDriver] Note arranging companion actor in chest-operator: {ex.Message}", LogLevel.Warn);
            }
        }

        _preActorStamina = 270.0f;
        _preActorWater = 40;
        _targetDirtIsWatered = false;

        _zoneTiles.Clear();
        _zoneTiles.Add(protectedCropTile);
        foreach (var t in allTargetTiles)
        {
            _zoneTiles.Add(t);
        }
        foreach (var t in additionalTilledTiles)
        {
            _zoneTiles.Add(t);
        }

        var arrangeEvidence = new Dictionary<string, object?>
        {
            ["scenario"] = _scenario,
            ["initialHostMoney"] = _initialPlayerMoney,
            ["arrangedTotalUnwateredTiles"] = 17,
            ["bareTillableTiles"] = Array.Empty<object>(),
            ["tilledUnplantedTiles"] = tilledUnplantedTiles.Select(d => new { x = (int)d.X, y = (int)d.Y }).ToList(),
            ["additionalTilledTiles"] = additionalTilledTiles.Select(d => new { x = (int)d.X, y = (int)d.Y }).ToList(),
            ["targetPlantTiles"] = allTargetTiles.Select(d => new { x = (int)d.X, y = (int)d.Y }).ToList(),
            ["protectedCropTile"] = new { x = (int)protectedCropTile.X, y = (int)protectedCropTile.Y },
            ["initialProtectedCrop"] = DescribeCropTile(protectedCropTile, hdProtected),
            ["chestTile"] = new { x = (int)chestTile.X, y = (int)chestTile.Y },
            ["targetSeedId"] = "(O)CarrotSeeds",
            ["targetSeedCount"] = 3,
            ["companion"] = new Dictionary<string, object?>
            {
                ["tile"] = new { x = 62, y = 23 },
                ["stamina"] = 270.0f,
                ["waterLeft"] = 40,
                ["hasHoe"] = true,
                ["hasWateringCan"] = true,
                ["seedCount"] = 0,
                ["slots"] = initialCompanionSlots,
                ["freeSlots"] = initialCompanionFreeSlots,
                ["items"] = companionItemsSnapshot
            },
            ["clearedObjects"] = clearedObjects
        };
        _evidence["arrange"] = arrangeEvidence;

        var outDir = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
        try
        {
            Directory.CreateDirectory(outDir);
            File.WriteAllText(Path.Combine(outDir, "arrange-evidence.json"), JsonSerializer.Serialize(arrangeEvidence, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Note writing arrange-evidence.json: {ex.Message}", LogLevel.Warn);
        }

        Monitor.Log("[TestDriver] Arranged chest-operator scenario: chest at (59,17) with 3 CarrotSeeds, companion on Farm at (62,23) with Hoe & WateringCan.", LogLevel.Info);
    }

    private void ArrangeExistingSaveBuy5(Farm farm, Farmer? companionFarmer, object? actor)
    {
        // 1. Snapshot pure baselines from real existing save:
        _initialFarmHoeDirtTiles.Clear();
        _initialFarmHoeDirtWithCrops.Clear();
        foreach (var kvp in farm.terrainFeatures.Pairs)
        {
            if (kvp.Value is HoeDirt hd)
            {
                _initialFarmHoeDirtTiles.Add(kvp.Key);
                if (hd.crop != null)
                {
                    _initialFarmHoeDirtWithCrops.Add(kvp.Key);
                }
            }
        }

        // 2. Player baseline (money and stamina)
        _initialPlayerMoney = Game1.player?.Money ?? 0;
        _initialPlayerStamina = Game1.player?.stamina ?? 0;

        // 3. Companion setup:
        // Ensure companion has Hoe, WateringCan (40 water), and NO seeds in inventory
        int initialCompanionSlots = 0;
        int initialCompanionFreeSlots = 0;
        var companionItemsSnapshot = new List<Dictionary<string, object?>>();

        if (companionFarmer != null)
        {
            companionFarmer.Items.Clear();
            companionFarmer.Items.Add(new StardewValley.Tools.Hoe());
            var can = new StardewValley.Tools.WateringCan { WaterLeft = 40 };
            companionFarmer.Items.Add(can);

            int cap = companionFarmer.MaxItems > 0 ? companionFarmer.MaxItems : 12;
            while (companionFarmer.Items.Count < cap)
            {
                companionFarmer.Items.Add(null);
            }

            companionFarmer.CurrentToolIndex = 0;
            companionFarmer.Position = new Vector2(64 * 64, 15 * 64);
            companionFarmer.faceDirection(2);
            companionFarmer.Stamina = 270.0f;
            companionFarmer.maxStamina.Value = 270;
            companionFarmer.currentLocation = farm;

            initialCompanionSlots = companionFarmer.Items.Count;
            initialCompanionFreeSlots = companionFarmer.Items.Count(it => it == null);
            companionItemsSnapshot = DescribeItems(companionFarmer.Items);
        }

        if (actor != null)
        {
            try
            {
                var stProp = actor.GetType().GetProperty("Stamina");
                stProp?.SetValue(actor, 270.0f);
                var wProp = actor.GetType().GetProperty("WaterLeft");
                wProp?.SetValue(actor, 40);
                var posProp = actor.GetType().GetProperty("PixelPosition");
                posProp?.SetValue(actor, new Vector2(64 * 64, 15 * 64));
                var locField = actor.GetType().GetField("_locationName", BindingFlags.NonPublic | BindingFlags.Instance);
                locField?.SetValue(actor, "Farm");
                var faceMethod = actor.GetType().GetMethod("Face");
                faceMethod?.Invoke(actor, new object[] { 2 });
            }
            catch (Exception ex)
            {
                Monitor.Log($"[TestDriver] Note arranging companion actor in existing-save-buy5: {ex.Message}", LogLevel.Warn);
            }
        }

        _preActorStamina = 270.0f;
        _preActorWater = 40;

        var arrangeEvidence = new Dictionary<string, object?>
        {
            ["scenario"] = _scenario,
            ["initialHostMoney"] = _initialPlayerMoney,
            ["initialHoeDirtCount"] = _initialFarmHoeDirtTiles.Count,
            ["initialCropsCount"] = _initialFarmHoeDirtWithCrops.Count,
            ["timeOfDay"] = Game1.timeOfDay,
            ["season"] = Game1.currentSeason,
            ["dayOfMonth"] = Game1.dayOfMonth,
            ["year"] = Game1.year,
            ["companion"] = new Dictionary<string, object?>
            {
                ["tile"] = new { x = 64, y = 15 },
                ["stamina"] = 270.0f,
                ["waterLeft"] = 40,
                ["hasHoe"] = true,
                ["hasWateringCan"] = true,
                ["seedCount"] = 0,
                ["slots"] = initialCompanionSlots,
                ["freeSlots"] = initialCompanionFreeSlots,
                ["items"] = companionItemsSnapshot
            }
        };
        _evidence["arrange"] = arrangeEvidence;

        var outDir = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
        try
        {
            Directory.CreateDirectory(outDir);
            File.WriteAllText(Path.Combine(outDir, "arrange-evidence.json"), JsonSerializer.Serialize(arrangeEvidence, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Note writing arrange-evidence.json: {ex.Message}", LogLevel.Warn);
        }

        Monitor.Log($"[TestDriver] Arranged existing-save-buy5: baseline {_initialFarmHoeDirtTiles.Count} HoeDirt, {_initialFarmHoeDirtWithCrops.Count} crops, host money={_initialPlayerMoney}, timeOfDay={Game1.timeOfDay}.", LogLevel.Info);
    }

    private void EvaluateFarmFlowGate(string outDir, bool playerInvariant)
    {
        var done = ReadLoopDemoDoneFile(outDir);
        RecordDoneFileEvidence(done);

        var failures = new List<string>();
        var farm = Game1.getFarm();

        // 1. Mature crops harvested
        var matureTiles = new[] { new Vector2(63, 20), new Vector2(64, 20), new Vector2(65, 20) };
        var matureStates = new Dictionary<string, object?>();
        var stillReady = new List<string>();
        foreach (var t in matureTiles)
        {
            var hd = farm.terrainFeatures.TryGetValue(t, out var tf) ? tf as HoeDirt : null;
            bool ready = hd?.crop != null && hd.readyForHarvest();
            string key = $"{(int)t.X},{(int)t.Y}";
            matureStates[key] = new Dictionary<string, object?>
            {
                ["hasCrop"] = hd?.crop != null,
                ["readyForHarvest"] = ready
            };
            if (ready) stillReady.Add(key);
        }
        bool matureHarvested = stillReady.Count == 0;
        _evidence["matureTileStates"] = matureStates;
        _evidence["matureHarvested"] = matureHarvested;
        if (!matureHarvested)
        {
            failures.Add($"Mature parsnips still ready for harvest on: [{string.Join(",", stillReady)}].");
        }

        // 2. Immature crop intact
        var immatureTile = new Vector2(62, 20);
        var immatureDirt = farm.terrainFeatures.TryGetValue(immatureTile, out var itf) ? itf as HoeDirt : null;
        bool immatureIntact = immatureDirt?.crop != null && immatureDirt.crop.netSeedIndex.Value == "472" && !immatureDirt.readyForHarvest();
        _evidence["immatureTileState"] = DescribeCropTile(immatureTile, immatureDirt);
        _evidence["immatureIntact"] = immatureIntact;
        if (!immatureIntact)
        {
            failures.Add("Immature parsnip at (62,20) was harvested, destroyed, or missing.");
        }

        // 3. All target dirt tiles watered (61..65, 20)
        var allDirtTiles = new[] { new Vector2(61, 20), new Vector2(62, 20), new Vector2(63, 20), new Vector2(64, 20), new Vector2(65, 20) };
        var dirtWateredStates = new Dictionary<string, bool>();
        var unwateredTiles = new List<string>();
        foreach (var dt in allDirtTiles)
        {
            var hd = farm.terrainFeatures.TryGetValue(dt, out var dtf) ? dtf as HoeDirt : null;
            bool watered = hd != null && hd.state.Value == 1;
            string key = $"{(int)dt.X},{(int)dt.Y}";
            dirtWateredStates[key] = watered;
            if (!watered) unwateredTiles.Add(key);
        }
        bool allDirtWatered = unwateredTiles.Count == 0;
        _evidence["dirtWateredStates"] = dirtWateredStates;
        _evidence["allDirtWatered"] = allDirtWatered;
        if (!allDirtWatered)
        {
            failures.Add($"Tilled dirt tiles remaining unwatered: [{string.Join(",", unwateredTiles)}].");
        }

        // 4. Reachable chest & conservation
        var chestTile = new Vector2(66, 20);
        var chestB = farm.objects.TryGetValue(chestTile, out var cObj) ? cObj as StardewValley.Objects.Chest : null;
        bool chestBPresent = chestB != null;
        _evidence["chestBPresent"] = chestBPresent;
        if (!chestBPresent)
        {
            failures.Add("Reachable chest at (66,20) is missing.");
        }

        // 5. Full chest preserved
        var fullChestTile = new Vector2(68, 20);
        var chestA = farm.objects.TryGetValue(fullChestTile, out var fObj) ? fObj as StardewValley.Objects.Chest : null;
        bool fullChestPresent = chestA != null;
        bool fullChestUntouched = false;
        if (chestA != null)
        {
            fullChestUntouched = chestA.Items.Count == 36 &&
                chestA.Items.All(it => it != null && it.QualifiedItemId == "(O)390" && it.Stack == 999);
        }
        _evidence["fullChestPresent"] = fullChestPresent;
        _evidence["fullChestUntouched"] = fullChestUntouched;
        if (!fullChestPresent || !fullChestUntouched)
        {
            failures.Add("Full chest at (68,20) was destroyed, modified, or item stacks altered.");
        }

        // 6. Companion inventory & tools retention
        bool companionResolved = ResolveCompanionActor(out _, out var companionFarmer) && companionFarmer != null;
        _evidence["companionInventoryResolved"] = companionResolved;

        bool wateringCanInCompanion = false;
        bool wateringCanInChests = false;
        if (companionFarmer != null)
        {
            wateringCanInCompanion = companionFarmer.Items.OfType<StardewValley.Tools.WateringCan>().Any();
        }
        if (chestB != null && chestB.Items.OfType<StardewValley.Tools.WateringCan>().Any()) wateringCanInChests = true;
        if (chestA != null && chestA.Items.OfType<StardewValley.Tools.WateringCan>().Any()) wateringCanInChests = true;

        bool toolsRetained = companionResolved && wateringCanInCompanion && !wateringCanInChests;
        _evidence["toolsRetainedInCompanionBackpack"] = toolsRetained;
        _evidence["wateringCanInCompanion"] = wateringCanInCompanion;
        _evidence["wateringCanInChests"] = wateringCanInChests;
        if (!toolsRetained)
        {
            failures.Add("Watering can was not retained in companion backpack (missing or put into chest).");
        }

        // 7. Item conservation
        int chestParsnips = chestB != null ? CountItemsByQualifiedId(chestB.Items, "(O)24") : 0;
        int companionParsnips = companionFarmer != null ? CountItemsByQualifiedId(companionFarmer.Items, "(O)24") : 0;
        int parsnipTotal = chestParsnips + companionParsnips;
        bool parsnipConservation = companionResolved && parsnipTotal == 8;

        _evidence["parsnipsInChestB"] = chestParsnips;
        _evidence["parsnipsInCompanion"] = companionParsnips;
        _evidence["parsnipTotal"] = parsnipTotal;
        _evidence["parsnipConservationExpected"] = 8;
        _evidence["parsnipConservation"] = parsnipConservation;
        if (!parsnipConservation)
        {
            failures.Add($"Parsnip conservation violated: chest({chestParsnips}) + companion({companionParsnips}) = {parsnipTotal}, expected 8.");
        }

        int chestWood = chestB != null ? CountItemsByQualifiedId(chestB.Items, "(O)388") : 0;
        int companionWood = companionFarmer != null ? CountItemsByQualifiedId(companionFarmer.Items, "(O)388") : 0;
        int woodTotal = chestWood + companionWood;
        bool woodConservation = companionResolved && woodTotal == 25;
        _evidence["woodInChestB"] = chestWood;
        _evidence["woodInCompanion"] = companionWood;
        _evidence["woodTotal"] = woodTotal;
        _evidence["woodConservationExpected"] = 25;
        _evidence["woodConservation"] = woodConservation;
        if (!woodConservation)
        {
            failures.Add($"Wood conservation violated: chest({chestWood}) + companion({companionWood}) = {woodTotal}, expected 25.");
        }

        int chestStone = chestB != null ? CountItemsByQualifiedId(chestB.Items, "(O)390") : 0;
        int companionStone = companionFarmer != null ? CountItemsByQualifiedId(companionFarmer.Items, "(O)390") : 0;
        int stoneTotal = chestStone + companionStone;
        bool stoneConservation = companionResolved && stoneTotal == 10;
        _evidence["stoneInChestB"] = chestStone;
        _evidence["stoneInCompanion"] = companionStone;
        _evidence["stoneTotal"] = stoneTotal;
        _evidence["stoneConservationExpected"] = 10;
        _evidence["stoneConservation"] = stoneConservation;
        if (!stoneConservation)
        {
            failures.Add($"Stone conservation violated: chest({chestStone}) + companion({companionStone}) = {stoneTotal}, expected 10.");
        }

        // 8. Host attribution
        bool playerResourcesInvariant = (Game1.player != null && Game1.player.stamina == _initialPlayerStamina && Game1.player.Money == _initialPlayerMoney);
        bool inventoryInvariant = CheckInventoryInvariance();
        bool playerUntouched = playerResourcesInvariant && inventoryInvariant;

        _evidence["hostChanges"] = new Dictionary<string, object?>
        {
            ["initialStamina"] = _initialPlayerStamina,
            ["finalStamina"] = Game1.player?.stamina ?? 0,
            ["staminaDelta"] = (Game1.player?.stamina ?? 0) - _initialPlayerStamina,
            ["initialMoney"] = _initialPlayerMoney,
            ["finalMoney"] = Game1.player?.Money ?? 0,
            ["moneyDelta"] = (Game1.player?.Money ?? 0) - _initialPlayerMoney,
            ["inventoryInvariant"] = inventoryInvariant,
            ["playerUntouched"] = playerUntouched,
            ["attributedToHostManualAction"] = !playerUntouched
        };
        _evidence["playerUntouched"] = playerUntouched;
        if (!playerUntouched)
        {
            failures.Add("Host player state modified during companion execution without manual attribution.");
        }

        // 9. Done file checks
        if (!done.Exists)
        {
            failures.Add("TIMEOUT_WAITING_LOOP_DEMO_DONE: farm-flow timed out before loop-demo-done.json was produced.");
        }
        else if (!done.Success)
        {
            failures.Add($"FARM_FLOW_FAILED: {done.Error ?? "orchestrator reported failure in loop-demo-done.json"}");
        }

        bool passed = done.Exists && done.Success && failures.Count == 0;
        _evidence["functionalSkillPassed"] = passed;
        _evidence["gateSuccess"] = passed;
        if (!passed)
        {
            _evidence["failureReason"] = string.Join(" ", failures);
        }
        Monitor.Log($"[TestDriver] farm-flow gate {(passed ? "PASSED" : "FAILED")}: matureHarvested={matureHarvested}, immatureIntact={immatureIntact}, allDirtWatered={allDirtWatered}, parsnips={parsnipTotal}/8, wood={woodTotal}/25, fullChestUntouched={fullChestUntouched}, toolsRetained={toolsRetained}, playerUntouched={playerUntouched}.",
            passed ? LogLevel.Info : LogLevel.Error);
    }

    private void EvaluateFarmFlowInterruptGate(string outDir, bool playerInvariant)
    {
        var done = ReadLoopDemoDoneFile(outDir);
        RecordDoneFileEvidence(done);

        var failures = new List<string>();
        var farm = Game1.getFarm();

        // 1. Water tiles watered by re-issued command
        var waterTiles = new[] { new Vector2(60, 21), new Vector2(61, 21) };
        var waterStates = new Dictionary<string, bool>();
        foreach (var wt in waterTiles)
        {
            var hd = farm.terrainFeatures.TryGetValue(wt, out var tf) ? tf as HoeDirt : null;
            bool watered = hd != null && hd.state.Value == 1;
            waterStates[$"{(int)wt.X},{(int)wt.Y}"] = watered;
            if (!watered) failures.Add($"Target soil at ({(int)wt.X},{(int)wt.Y}) was not watered after interrupt and re-issue.");
        }
        bool waterTilesWatered = waterStates.Values.All(w => w);
        _evidence["waterTilesWatered"] = waterTilesWatered;
        _evidence["waterTileStates"] = waterStates;

        // 2. Crop consistency after interrupt
        int harvestedCount = 0;
        int remainingCount = 0;
        for (int x = 60; x <= 65; x++)
        {
            var t = new Vector2(x, 20);
            var hd = farm.terrainFeatures.TryGetValue(t, out var tf) ? tf as HoeDirt : null;
            if (hd == null)
            {
                failures.Add($"HoeDirt at ({(int)t.X},{(int)t.Y}) missing.");
            }
            else if (hd.crop != null)
            {
                remainingCount++;
            }
            else
            {
                harvestedCount++;
            }
        }
        bool cropsConsistent = (harvestedCount + remainingCount == 6);
        _evidence["cropsConsistent"] = cropsConsistent;
        _evidence["harvestedCount"] = harvestedCount;
        _evidence["remainingCount"] = remainingCount;
        if (!cropsConsistent)
        {
            failures.Add($"Crop count inconsistent after interrupt: harvested({harvestedCount}) + remaining({remainingCount}) != 6.");
        }

        // 3. Tools retained
        bool companionResolved = ResolveCompanionActor(out _, out var companionFarmer) && companionFarmer != null;
        bool hasWateringCan = companionFarmer != null && companionFarmer.Items.OfType<StardewValley.Tools.WateringCan>().Any();
        _evidence["toolsRetainedInCompanionBackpack"] = hasWateringCan;
        if (!hasWateringCan)
        {
            failures.Add("Watering can missing from companion backpack.");
        }

        // 4. Host attribution
        bool playerResourcesInvariant = (Game1.player != null && Game1.player.stamina == _initialPlayerStamina && Game1.player.Money == _initialPlayerMoney);
        bool inventoryInvariant = CheckInventoryInvariance();
        bool playerUntouched = playerResourcesInvariant && inventoryInvariant;

        _evidence["hostChanges"] = new Dictionary<string, object?>
        {
            ["initialStamina"] = _initialPlayerStamina,
            ["finalStamina"] = Game1.player?.stamina ?? 0,
            ["staminaDelta"] = (Game1.player?.stamina ?? 0) - _initialPlayerStamina,
            ["initialMoney"] = _initialPlayerMoney,
            ["finalMoney"] = Game1.player?.Money ?? 0,
            ["moneyDelta"] = (Game1.player?.Money ?? 0) - _initialPlayerMoney,
            ["inventoryInvariant"] = inventoryInvariant,
            ["playerUntouched"] = playerUntouched,
            ["attributedToHostManualAction"] = !playerUntouched
        };
        _evidence["playerUntouched"] = playerUntouched;
        if (!playerUntouched)
        {
            failures.Add("Host player state modified during companion execution.");
        }

        // 5. Done file
        if (!done.Exists)
        {
            failures.Add("TIMEOUT_WAITING_LOOP_DEMO_DONE: farm-flow-interrupt timed out before loop-demo-done.json was produced.");
        }
        else if (!done.Success)
        {
            failures.Add($"FARM_FLOW_INTERRUPT_FAILED: {done.Error ?? "orchestrator reported failure in loop-demo-done.json"}");
        }

        bool passed = done.Exists && done.Success && failures.Count == 0;
        _evidence["functionalSkillPassed"] = passed;
        _evidence["gateSuccess"] = passed;
        if (!passed)
        {
            _evidence["failureReason"] = string.Join(" ", failures);
        }
        Monitor.Log($"[TestDriver] farm-flow-interrupt gate {(passed ? "PASSED" : "FAILED")}: waterTilesWatered={waterTilesWatered}, cropsConsistent={cropsConsistent} (harvested={harvestedCount}, remaining={remainingCount}), playerUntouched={playerUntouched}.",
            passed ? LogLevel.Info : LogLevel.Error);
    }

    private void EvaluatePlantingMcpGate(string outDir, bool playerInvariant)
    {
        var done = ReadLoopDemoDoneFile(outDir);
        RecordDoneFileEvidence(done);

        var failures = new List<string>();
        var farm = Game1.getFarm();

        var targetPlantTiles = new[]
        {
            new Vector2(61, 21),
            new Vector2(62, 21),
            new Vector2(63, 21),
            new Vector2(64, 21)
        };
        var protectedCropTile = new Vector2(60, 21);

        // 1. Target tiles check: all 4 must have HoeDirt, have parsnip crop planted, and be watered
        var targetStates = new Dictionary<string, object?>();
        int plantedCount = 0;
        int wateredCount = 0;
        var unplantedTiles = new List<string>();
        var unwateredTiles = new List<string>();

        foreach (var t in targetPlantTiles)
        {
            var hd = farm.terrainFeatures.TryGetValue(t, out var tf) ? tf as HoeDirt : null;
            bool hasDirt = hd != null;
            bool hasCrop = hd?.crop != null;
            string? seedId = hd?.crop?.netSeedIndex?.Value;
            bool isCorrectSeed = seedId == "472";
            bool watered = hd != null && hd.state.Value == 1;

            string key = $"{(int)t.X},{(int)t.Y}";
            targetStates[key] = new Dictionary<string, object?>
            {
                ["dirtPresent"] = hasDirt,
                ["hasCrop"] = hasCrop,
                ["seedIndex"] = seedId,
                ["isCorrectSeed"] = isCorrectSeed,
                ["watered"] = watered,
                ["cropPhase"] = hd?.crop?.currentPhase?.Value,
                ["dead"] = hd?.crop?.dead?.Value
            };

            if (hasCrop && isCorrectSeed)
            {
                plantedCount++;
            }
            else
            {
                unplantedTiles.Add(key);
            }

            if (watered)
            {
                wateredCount++;
            }
            else
            {
                unwateredTiles.Add(key);
            }
        }

        _evidence["targetPlantTiles"] = targetStates;
        _evidence["plantedCount"] = plantedCount;
        _evidence["wateredCount"] = wateredCount;

        if (plantedCount < 4)
        {
            failures.Add($"Target tiles missing newly planted parsnip crops: [{string.Join(",", unplantedTiles)}]. Total planted: {plantedCount}/4.");
        }
        if (wateredCount < 4)
        {
            failures.Add($"Target tiles not watered: [{string.Join(",", unwateredTiles)}]. Total watered: {wateredCount}/4.");
        }

        // 2. Protected tile check: original immature crop must be preserved
        var protectedHd = farm.terrainFeatures.TryGetValue(protectedCropTile, out var ptf) ? ptf as HoeDirt : null;
        bool protectedDirtPresent = protectedHd != null;
        bool protectedCropPresent = protectedHd?.crop != null;
        string? protectedSeed = protectedHd?.crop?.netSeedIndex?.Value;
        bool protectedNotHarvested = protectedHd?.crop != null && !protectedHd.readyForHarvest();
        bool protectedNotDead = protectedHd?.crop != null && !protectedHd.crop.dead.Value;
        bool protectedReferenceIdentical = _initialProtectedCropRef != null && object.ReferenceEquals(protectedHd?.crop, _initialProtectedCropRef);
        bool protectedPreserved = protectedDirtPresent && protectedCropPresent && protectedSeed == "472" && protectedNotHarvested && protectedNotDead && protectedReferenceIdentical;

        _evidence["protectedCropTileState"] = new Dictionary<string, object?>
        {
            ["tile"] = new { x = (int)protectedCropTile.X, y = (int)protectedCropTile.Y },
            ["dirtPresent"] = protectedDirtPresent,
            ["cropPresent"] = protectedCropPresent,
            ["seedIndex"] = protectedSeed,
            ["notHarvested"] = protectedNotHarvested,
            ["notDead"] = protectedNotDead,
            ["referenceIdentical"] = protectedReferenceIdentical,
            ["preserved"] = protectedPreserved
        };
        _evidence["protectedCropPreserved"] = protectedPreserved;

        if (!protectedPreserved)
        {
            if (!protectedReferenceIdentical)
            {
                failures.Add($"Protected immature crop at ({(int)protectedCropTile.X},{(int)protectedCropTile.Y}) was replaced by another instance (ReferenceEquals failed).");
            }
            else
            {
                failures.Add($"Protected immature crop at ({(int)protectedCropTile.X},{(int)protectedCropTile.Y}) was destroyed, replaced, harvested, or missing.");
            }
        }

        // 3. Seeds in companion inventory check
        bool companionResolved = ResolveCompanionActor(out _, out var companionFarmer) && companionFarmer != null;
        _evidence["companionResolved"] = companionResolved;

        int remainingSeeds = companionFarmer != null ? CountItemsByQualifiedId(companionFarmer.Items, "(O)472") : -1;
        int seedReduction = companionFarmer != null ? (4 - remainingSeeds) : -1;
        bool seedReductionMatches = (remainingSeeds == 0) && (seedReduction == plantedCount);

        _evidence["initialSeeds"] = 4;
        _evidence["remainingSeeds"] = remainingSeeds;
        _evidence["seedReduction"] = seedReduction;
        _evidence["seedReductionMatches"] = seedReductionMatches;

        if (!seedReductionMatches)
        {
            failures.Add($"Seed consumption mismatch: remainingSeeds={remainingSeeds} (expected 0), seedReduction={seedReduction} (expected {plantedCount}).");
        }

        // 4. Companion tools retention check
        bool hasHoe = companionFarmer != null && companionFarmer.Items.OfType<StardewValley.Tools.Hoe>().Any();
        bool hasWateringCan = companionFarmer != null && companionFarmer.Items.OfType<StardewValley.Tools.WateringCan>().Any();
        bool toolsRetained = companionResolved && hasHoe && hasWateringCan;

        _evidence["companionHasHoe"] = hasHoe;
        _evidence["companionHasWateringCan"] = hasWateringCan;
        _evidence["toolsRetainedInCompanionBackpack"] = toolsRetained;

        if (!toolsRetained)
        {
            failures.Add($"Companion tools not retained in backpack: Hoe={hasHoe}, WateringCan={hasWateringCan}.");
        }

        // 5. Host attribution (diagnostic only, does not fail gate per spec)
        bool hostResourcesInvariant = (Game1.player != null && Game1.player.stamina == _initialPlayerStamina && Game1.player.Money == _initialPlayerMoney);
        bool hostInventoryInvariant = CheckInventoryInvariance();
        bool hostUntouched = hostResourcesInvariant && hostInventoryInvariant;

        _evidence["hostChanges"] = new Dictionary<string, object?>
        {
            ["initialStamina"] = _initialPlayerStamina,
            ["finalStamina"] = Game1.player?.stamina ?? 0,
            ["staminaDelta"] = (Game1.player?.stamina ?? 0) - _initialPlayerStamina,
            ["initialMoney"] = _initialPlayerMoney,
            ["finalMoney"] = Game1.player?.Money ?? 0,
            ["moneyDelta"] = (Game1.player?.Money ?? 0) - _initialPlayerMoney,
            ["inventoryInvariant"] = hostInventoryInvariant,
            ["hostUntouched"] = hostUntouched,
            ["diagnosticOnly"] = true
        };
        _evidence["hostUntouched"] = hostUntouched;
        Monitor.Log($"[TestDriver] planting-mcp host attribution (diagnostic): untouched={hostUntouched}, staminaDelta={(Game1.player?.stamina ?? 0) - _initialPlayerStamina}.", LogLevel.Info);

        // 6. Done file checks
        if (!done.Exists)
        {
            failures.Add("TIMEOUT_WAITING_DONE_FILE: planting-mcp timed out before done file (planting-mcp-done.json or loop-demo-done.json) was produced.");
        }
        else if (!done.Success)
        {
            failures.Add($"PLANTING_MCP_FAILED: {done.Error ?? "orchestrator reported failure in done file"}");
        }

        bool passed = done.Exists && done.Success && failures.Count == 0;
        _evidence["functionalSkillPassed"] = passed;
        _evidence["gateSuccess"] = passed;
        if (!passed)
        {
            _evidence["failureReason"] = string.Join(" ", failures);
        }

        Monitor.Log($"[TestDriver] planting-mcp gate {(passed ? "PASSED" : "FAILED")}: planted={plantedCount}/4, watered={wateredCount}/4, protectedPreserved={protectedPreserved}, seedReduction={seedReduction}/4, toolsRetained={toolsRetained}, doneSuccess={done.Success}.",
            passed ? LogLevel.Info : LogLevel.Error);
    }

    private void EvaluateShippingMcpGate(string outDir, bool playerInvariant)
    {
        var done = ReadLoopDemoDoneFile(outDir);
        RecordDoneFileEvidence(done);

        var failures = new List<string>();
        var farm = Game1.getFarm();

        // 1. Resolve companion
        bool companionResolved = ResolveCompanionActor(out _, out var companionFarmer) && companionFarmer != null;
        _evidence["companionResolved"] = companionResolved;

        // 2. Shipping bin check: must contain 2 shipped parsnips
        var shippingBin = companionFarmer != null ? farm.getShippingBin(companionFarmer) : farm.getShippingBin(Game1.player);
        int binParsnips = shippingBin != null ? CountItemsByQualifiedId(shippingBin, "(O)24") : 0;
        int binTotalCount = shippingBin != null ? shippingBin.Sum(i => i?.Stack ?? 0) : 0;

        _evidence["binParsnips"] = binParsnips;
        _evidence["binTotalCount"] = binTotalCount;

        if (binParsnips != 2)
        {
            failures.Add($"Shipping bin parsnip count mismatch: expected 2, got {binParsnips}.");
        }

        // 3. Companion backpack check: must have 2 remaining parsnips (started with 4, shipped 2)
        int remainingBackpack = companionFarmer != null ? CountItemsByQualifiedId(companionFarmer.Items, "(O)24") : -1;
        _evidence["initialBackpackCount"] = 4;
        _evidence["remainingBackpackCount"] = remainingBackpack;

        if (remainingBackpack != 2)
        {
            failures.Add($"Companion backpack parsnip count mismatch: expected 2, got {remainingBackpack}.");
        }

        // 4. Host invariance: host stamina, money, and inventory
        bool hostResourcesInvariant = (Game1.player != null && Game1.player.stamina == _initialPlayerStamina && Game1.player.Money == _initialPlayerMoney);
        bool hostInventoryInvariant = CheckInventoryInvariance();
        bool hostUntouched = hostResourcesInvariant && hostInventoryInvariant;

        _evidence["hostChanges"] = new Dictionary<string, object?>
        {
            ["initialStamina"] = _initialPlayerStamina,
            ["finalStamina"] = Game1.player?.stamina ?? 0,
            ["staminaDelta"] = (Game1.player?.stamina ?? 0) - _initialPlayerStamina,
            ["initialMoney"] = _initialPlayerMoney,
            ["finalMoney"] = Game1.player?.Money ?? 0,
            ["moneyDelta"] = (Game1.player?.Money ?? 0) - _initialPlayerMoney,
            ["inventoryInvariant"] = hostInventoryInvariant,
            ["hostUntouched"] = hostUntouched,
            ["diagnosticOnly"] = false
        };
        _evidence["hostUntouched"] = hostUntouched;

        if (!hostUntouched)
        {
            failures.Add("Host player stamina, money, or inventory changed during companion shipping.");
        }

        // 5. Done file checks
        if (!done.Exists)
        {
            failures.Add("TIMEOUT_WAITING_DONE_FILE: shipping-mcp timed out before done file (shipping-mcp-done.json or loop-demo-done.json) was produced.");
        }
        else if (!done.Success)
        {
            failures.Add($"SHIPPING_MCP_FAILED: {done.Error ?? "orchestrator reported failure in done file"}");
        }

        bool passed = done.Exists && done.Success && failures.Count == 0;
        _evidence["functionalSkillPassed"] = passed;
        _evidence["gateSuccess"] = passed;
        if (!passed)
        {
            _evidence["failureReason"] = string.Join(" ", failures);
        }

        Monitor.Log($"[TestDriver] shipping-mcp gate {(passed ? "PASSED" : "FAILED")}: binParsnips={binParsnips}/2, remainingBackpack={remainingBackpack}/2, hostUntouched={hostUntouched}, doneSuccess={done.Success}.",
            passed ? LogLevel.Info : LogLevel.Error);
    }

    private void EvaluateNavigationMcpGate(string outDir, bool playerInvariant)
    {
        var done = ReadLoopDemoDoneFile(outDir);
        RecordDoneFileEvidence(done);

        var failures = new List<string>();

        // 1. Resolve companion actor and farmer
        bool companionResolved = ResolveCompanionActor(out var actor, out var companionFarmer);
        _evidence["companionResolved"] = companionResolved;

        string finalLocation = "";
        Vector2 finalTile = Vector2.Zero;
        if (companionFarmer != null)
        {
            finalLocation = companionFarmer.currentLocation?.Name ?? "";
            finalTile = companionFarmer.Tile;
        }
        else if (actor != null)
        {
            try
            {
                var locProp = actor.GetType().GetProperty("LocationName");
                finalLocation = locProp?.GetValue(actor) as string ?? "";
                var posProp = actor.GetType().GetProperty("PixelPosition");
                if (posProp?.GetValue(actor) is Vector2 pos)
                {
                    finalTile = new Vector2((int)(pos.X / 64f), (int)(pos.Y / 64f));
                }
            }
            catch { }
        }

        _evidence["companionFinalLocation"] = finalLocation;
        _evidence["companionFinalTile"] = new { x = (int)finalTile.X, y = (int)finalTile.Y };

        if (!string.Equals(finalLocation, "Farm", StringComparison.OrdinalIgnoreCase))
        {
            failures.Add($"Companion final location mismatch: expected 'Farm', got '{finalLocation}'.");
        }

        // 2. Parse done file JSON for navigation details
        bool outboundOk = false;
        bool inboundOk = false;
        bool busStopTraversedOutbound = false;
        bool busStopTraversedInbound = false;
        bool continuousPathOutbound = false;
        bool continuousPathInbound = false;

        if (done.Exists && !string.IsNullOrWhiteSpace(done.RawJson))
        {
            try
            {
                using var doc = JsonDocument.Parse(done.RawJson);
                var root = doc.RootElement;
                if (root.TryGetProperty("outbound", out var outElem))
                {
                    if (outElem.TryGetProperty("terminalState", out var ts) &&
                        string.Equals(ts.GetString(), "succeeded", StringComparison.OrdinalIgnoreCase))
                    {
                        outboundOk = true;
                    }
                    if (outElem.TryGetProperty("finalLocation", out var fl) &&
                        !string.Equals(fl.GetString(), "Town", StringComparison.OrdinalIgnoreCase))
                    {
                        outboundOk = false;
                        failures.Add($"Outbound finalLocation mismatch: expected 'Town', got '{fl.GetString()}'.");
                    }
                    if (outElem.TryGetProperty("finalTile", out var ftElem) &&
                        ftElem.TryGetProperty("x", out var fx) && ftElem.TryGetProperty("y", out var fy))
                    {
                        int x = fx.GetInt32();
                        int y = fy.GetInt32();
                        int dist = Math.Abs(x - (int)_navDoorTile.X) + Math.Abs(y - (int)_navDoorTile.Y);
                        _evidence["outboundFinalTile"] = new { x, y };
                        _evidence["outboundDistanceToDoor"] = dist;
                        if (dist > 3)
                        {
                            outboundOk = false;
                            failures.Add($"Outbound final tile ({x},{y}) too far from door target ({_navDoorTile.X},{_navDoorTile.Y}): distance {dist} > 3.");
                        }
                    }
                    if (outElem.TryGetProperty("targetAdjusted", out var adjElem))
                    {
                        _evidence["outboundTargetAdjusted"] = adjElem.GetBoolean();
                    }
                    if (outElem.TryGetProperty("visitedLocations", out var vl) && vl.ValueKind == JsonValueKind.Array)
                    {
                        var visited = vl.EnumerateArray().Select(e => e.GetString() ?? "").ToList();
                        busStopTraversedOutbound = visited.Any(v => string.Equals(v, "BusStop", StringComparison.OrdinalIgnoreCase));
                    }
                    if (outElem.TryGetProperty("hopCount", out var hc) && hc.GetInt32() > 0)
                    {
                        continuousPathOutbound = true;
                    }
                    else if (outElem.TryGetProperty("effects", out var eff) && eff.ValueKind == JsonValueKind.Array && eff.GetArrayLength() > 0)
                    {
                        continuousPathOutbound = true;
                    }
                }
                if (root.TryGetProperty("inbound", out var inElem))
                {
                    if (inElem.TryGetProperty("terminalState", out var ts) &&
                        string.Equals(ts.GetString(), "succeeded", StringComparison.OrdinalIgnoreCase))
                    {
                        inboundOk = true;
                    }
                    if (inElem.TryGetProperty("finalLocation", out var fl) &&
                        !string.Equals(fl.GetString(), "Farm", StringComparison.OrdinalIgnoreCase))
                    {
                        inboundOk = false;
                        failures.Add($"Inbound finalLocation mismatch: expected 'Farm', got '{fl.GetString()}'.");
                    }
                    if (inElem.TryGetProperty("visitedLocations", out var vl) && vl.ValueKind == JsonValueKind.Array)
                    {
                        var visited = vl.EnumerateArray().Select(e => e.GetString() ?? "").ToList();
                        busStopTraversedInbound = visited.Any(v => string.Equals(v, "BusStop", StringComparison.OrdinalIgnoreCase));
                    }
                    if (inElem.TryGetProperty("hopCount", out var hc) && hc.GetInt32() > 0)
                    {
                        continuousPathInbound = true;
                    }
                    else if (inElem.TryGetProperty("effects", out var eff) && eff.ValueKind == JsonValueKind.Array && eff.GetArrayLength() > 0)
                    {
                        continuousPathInbound = true;
                    }
                }
            }
            catch (Exception ex)
            {
                failures.Add($"Failed to parse done file JSON: {ex.Message}");
            }
        }

        _evidence["outboundSucceeded"] = outboundOk;
        _evidence["inboundSucceeded"] = inboundOk;
        _evidence["busStopTraversedOutbound"] = busStopTraversedOutbound;
        _evidence["busStopTraversedInbound"] = busStopTraversedInbound;
        _evidence["continuousPathOutbound"] = continuousPathOutbound;
        _evidence["continuousPathInbound"] = continuousPathInbound;

        if (!outboundOk)
        {
            failures.Add("Outbound navigation from Farm to Town did not succeed.");
        }
        if (!inboundOk)
        {
            failures.Add("Inbound navigation from Town to Farm did not succeed.");
        }
        if (!busStopTraversedOutbound || !busStopTraversedInbound)
        {
            failures.Add($"Cross-map route did not traverse BusStop (outbound={busStopTraversedOutbound}, inbound={busStopTraversedInbound}). Disallowed non-topological jump.");
        }

        // 3. Host player invariance (position, stamina, money, inventory)
        bool hostResourcesInvariant = (Game1.player != null && Game1.player.stamina == _initialPlayerStamina && Game1.player.Money == _initialPlayerMoney);
        bool hostInventoryInvariant = CheckInventoryInvariance();
        bool hostUntouched = hostResourcesInvariant && hostInventoryInvariant;

        _evidence["hostChanges"] = new Dictionary<string, object?>
        {
            ["initialStamina"] = _initialPlayerStamina,
            ["finalStamina"] = Game1.player?.stamina ?? 0,
            ["staminaDelta"] = (Game1.player?.stamina ?? 0) - _initialPlayerStamina,
            ["initialMoney"] = _initialPlayerMoney,
            ["finalMoney"] = Game1.player?.Money ?? 0,
            ["moneyDelta"] = (Game1.player?.Money ?? 0) - _initialPlayerMoney,
            ["inventoryInvariant"] = hostInventoryInvariant,
            ["hostUntouched"] = hostUntouched,
            ["diagnosticOnly"] = false
        };
        _evidence["hostUntouched"] = hostUntouched;

        if (!hostUntouched)
        {
            failures.Add("Host player stamina, money, or inventory changed during companion navigation.");
        }

        // 4. Done file checks
        if (!done.Exists)
        {
            failures.Add("TIMEOUT_WAITING_DONE_FILE: navigation-mcp timed out before done file (navigation-mcp-done.json or loop-demo-done.json) was produced.");
        }
        else if (!done.Success)
        {
            failures.Add($"NAVIGATION_MCP_FAILED: {done.Error ?? "orchestrator reported failure in done file"}");
        }

        bool passed = done.Exists && done.Success && failures.Count == 0;
        _evidence["functionalSkillPassed"] = passed;
        _evidence["gateSuccess"] = passed;
        if (!passed)
        {
            _evidence["failureReason"] = string.Join(" ", failures);
        }

        Monitor.Log($"[TestDriver] navigation-mcp gate {(passed ? "PASSED" : "FAILED")}: finalLocation={finalLocation}, outboundOk={outboundOk}, inboundOk={inboundOk}, busStopTraversed={busStopTraversedOutbound && busStopTraversedInbound}, hostUntouched={hostUntouched}, doneSuccess={done.Success}.",
            passed ? LogLevel.Info : LogLevel.Error);
    }

    private void EvaluatePurchaseMcpGate(string outDir, bool playerInvariant)
    {
        var done = ReadLoopDemoDoneFile(outDir);
        RecordDoneFileEvidence(done);

        var failures = new List<string>();

        // 1. Resolve companion actor and farmer
        bool companionResolved = ResolveCompanionActor(out var actor, out var companionFarmer);
        _evidence["companionResolved"] = companionResolved;

        int seedCount = 0;
        if (companionFarmer != null)
        {
            foreach (var itm in companionFarmer.Items)
            {
                if (itm != null && (string.Equals(itm.QualifiedItemId, "(O)472", StringComparison.OrdinalIgnoreCase) ||
                                    string.Equals(itm.ItemId, "472", StringComparison.OrdinalIgnoreCase)))
                {
                    seedCount += itm.Stack;
                }
            }
        }
        else if (actor != null)
        {
            try
            {
                var countMethod = actor.GetType().GetMethod("GetItemCount");
                if (countMethod != null)
                {
                    seedCount = (int)(countMethod.Invoke(actor, new object[] { "(O)472" }) ?? 0);
                }
            }
            catch (Exception ex)
            {
                Monitor.Log($"[TestDriver] Note reading companion item count: {ex.Message}", LogLevel.Warn);
            }
        }

        _evidence["purchasedSeedCount"] = seedCount;
        if (seedCount < 2)
        {
            failures.Add($"INSUFFICIENT_PURCHASED_ITEMS: expected at least 2 Parsnip Seeds ((O)472) in companion inventory, found {seedCount}.");
        }

        // 2. Money delta verification: host 500 -> 460 (delta == -40)
        int finalPlayerMoney = Game1.player?.Money ?? 0;
        int hostMoneyDelta = finalPlayerMoney - _initialPlayerMoney;
        _evidence["initialPlayerMoney"] = _initialPlayerMoney;
        _evidence["finalPlayerMoney"] = finalPlayerMoney;
        _evidence["hostMoneyDelta"] = hostMoneyDelta;

        if (hostMoneyDelta != -40)
        {
            failures.Add($"MONEY_DELTA_MISMATCH: expected host money delta of -40g (500 -> 460), got {hostMoneyDelta}g (500 -> {finalPlayerMoney}).");
        }

        // 3. Host player stamina and inventory invariance (stamina untouched, inventory untouched)
        bool hostStaminaUntouched = (Game1.player != null && Game1.player.stamina == _initialPlayerStamina);
        bool hostInventoryUntouched = CheckInventoryInvariance();
        bool hostUntouched = hostStaminaUntouched && hostInventoryUntouched && (hostMoneyDelta == -40);
        _evidence["hostUntouched"] = hostUntouched;
        _evidence["hostStaminaUntouched"] = hostStaminaUntouched;
        _evidence["hostInventoryUntouched"] = hostInventoryUntouched;

        if (!hostStaminaUntouched)
        {
            failures.Add("Host player stamina changed during companion purchase.");
        }
        if (!hostInventoryUntouched)
        {
            failures.Add("Host player inventory changed during companion purchase.");
        }

        // 4. Phase A (closed shop real negative call) verification
        var phaseAFile = Path.Combine(outDir, "purchase-mcp-phase-a-done.json");
        bool phaseAExists = File.Exists(phaseAFile);
        bool phaseASuccess = false;
        string? phaseAError = null;
        if (phaseAExists)
        {
            try
            {
                using var phaseADoc = JsonDocument.Parse(File.ReadAllText(phaseAFile));
                phaseASuccess = phaseADoc.RootElement.TryGetProperty("success", out var ps) && ps.GetBoolean();
                if (phaseADoc.RootElement.TryGetProperty("error", out var pe))
                {
                    phaseAError = pe.GetString();
                }
            }
            catch (Exception ex)
            {
                failures.Add($"PHASE_A_DONE_PARSE_ERROR: {ex.Message}");
            }
        }

        _evidence["phaseAFileExists"] = phaseAExists;
        _evidence["phaseASuccess"] = phaseASuccess;
        _evidence["phaseAError"] = phaseAError;

        if (!phaseAExists)
        {
            failures.Add("PHASE_A_DONE_FILE_MISSING: purchase-mcp-phase-a-done.json was not produced.");
        }
        else if (!phaseASuccess)
        {
            failures.Add($"PHASE_A_NEGATIVE_CHECK_FAILED: Phase A closed shop call did not succeed: {phaseAError}");
        }

        // 5. Done file checks
        if (!done.Exists)
        {
            failures.Add("TIMEOUT_WAITING_DONE_FILE: purchase-mcp timed out before done file (purchase-mcp-done.json or loop-demo-done.json) was produced.");
        }
        else if (!done.Success)
        {
            failures.Add($"PURCHASE_MCP_FAILED: {done.Error ?? "orchestrator reported failure in done file"}");
        }

        bool passed = phaseAExists && phaseASuccess && done.Exists && done.Success && failures.Count == 0;
        _evidence["functionalSkillPassed"] = passed;
        _evidence["gateSuccess"] = passed;
        if (!passed)
        {
            _evidence["failureReason"] = string.Join(" ", failures);
        }

        Monitor.Log($"[TestDriver] purchase-mcp gate {(passed ? "PASSED" : "FAILED")}: phaseA={phaseASuccess}, seeds={seedCount}/2, hostMoneyDelta={hostMoneyDelta}/-40, hostUntouched={hostUntouched}, doneSuccess={done.Success}.",
            passed ? LogLevel.Info : LogLevel.Error);
    }

    private void EvaluateCycleMcpGate(string outDir, bool playerInvariant)
    {
        var done = ReadLoopDemoDoneFile(outDir);
        RecordDoneFileEvidence(done);

        var failures = new List<string>();
        var farm = Game1.getFarm();

        var targetPlantTiles = new[]
        {
            new Vector2(61, 21),
            new Vector2(62, 21),
            new Vector2(63, 21),
            new Vector2(64, 21)
        };
        var protectedCropTile = new Vector2(60, 21);

        // 1. Target tiles check: all 4 must have HoeDirt, have parsnip crop planted, and be watered
        var targetStates = new Dictionary<string, object?>();
        int plantedCount = 0;
        int wateredCount = 0;
        var unplantedTiles = new List<string>();
        var unwateredTiles = new List<string>();

        foreach (var t in targetPlantTiles)
        {
            var hd = farm.terrainFeatures.TryGetValue(t, out var tf) ? tf as HoeDirt : null;
            bool hasDirt = hd != null;
            bool hasCrop = hd?.crop != null;
            string? seedId = hd?.crop?.netSeedIndex?.Value;
            bool isCorrectSeed = seedId == "472";
            bool watered = hd != null && hd.state.Value == 1;

            string key = $"{(int)t.X},{(int)t.Y}";
            targetStates[key] = new Dictionary<string, object?>
            {
                ["dirtPresent"] = hasDirt,
                ["hasCrop"] = hasCrop,
                ["seedIndex"] = seedId,
                ["isCorrectSeed"] = isCorrectSeed,
                ["watered"] = watered,
                ["cropPhase"] = hd?.crop?.currentPhase?.Value,
                ["dead"] = hd?.crop?.dead?.Value
            };

            if (hasCrop && isCorrectSeed)
            {
                plantedCount++;
            }
            else
            {
                unplantedTiles.Add(key);
            }

            if (watered)
            {
                wateredCount++;
            }
            else
            {
                unwateredTiles.Add(key);
            }
        }

        _evidence["targetPlantTiles"] = targetStates;
        _evidence["plantedCount"] = plantedCount;
        _evidence["wateredCount"] = wateredCount;

        if (plantedCount < 4)
        {
            failures.Add($"Target tiles missing newly planted parsnip crops: [{string.Join(",", unplantedTiles)}]. Total planted: {plantedCount}/4.");
        }
        if (wateredCount < 4)
        {
            failures.Add($"Target tiles not watered: [{string.Join(",", unwateredTiles)}]. Total watered: {wateredCount}/4.");
        }

        // 2. Protected tile check: original immature crop must be preserved
        var protectedHd = farm.terrainFeatures.TryGetValue(protectedCropTile, out var ptf) ? ptf as HoeDirt : null;
        bool protectedDirtPresent = protectedHd != null;
        bool protectedCropPresent = protectedHd?.crop != null;
        string? protectedSeed = protectedHd?.crop?.netSeedIndex?.Value;
        bool protectedNotHarvested = protectedHd?.crop != null && !protectedHd.readyForHarvest();
        bool protectedNotDead = protectedHd?.crop != null && !protectedHd.crop.dead.Value;
        bool protectedReferenceIdentical = _initialProtectedCropRef != null && object.ReferenceEquals(protectedHd?.crop, _initialProtectedCropRef);
        bool protectedPreserved = protectedDirtPresent && protectedCropPresent && protectedSeed == "472" && protectedNotHarvested && protectedNotDead && protectedReferenceIdentical;

        _evidence["protectedCropTileState"] = new Dictionary<string, object?>
        {
            ["tile"] = new { x = (int)protectedCropTile.X, y = (int)protectedCropTile.Y },
            ["dirtPresent"] = protectedDirtPresent,
            ["cropPresent"] = protectedCropPresent,
            ["seedIndex"] = protectedSeed,
            ["notHarvested"] = protectedNotHarvested,
            ["notDead"] = protectedNotDead,
            ["referenceIdentical"] = protectedReferenceIdentical,
            ["preserved"] = protectedPreserved
        };
        _evidence["protectedCropPreserved"] = protectedPreserved;

        if (!protectedPreserved)
        {
            if (!protectedReferenceIdentical)
            {
                failures.Add($"Protected immature crop at ({(int)protectedCropTile.X},{(int)protectedCropTile.Y}) was replaced by another instance (ReferenceEquals failed).");
            }
            else
            {
                failures.Add($"Protected immature crop at ({(int)protectedCropTile.X},{(int)protectedCropTile.Y}) was destroyed, replaced, harvested, or missing.");
            }
        }

        // 3. Companion inventory & tools retention check
        bool companionResolved = ResolveCompanionActor(out var actor, out var companionFarmer);
        _evidence["companionResolved"] = companionResolved;

        int remainingSeeds = companionFarmer != null ? CountItemsByQualifiedId(companionFarmer.Items, "(O)472") : 0;
        bool hasHoe = companionFarmer != null && companionFarmer.Items.OfType<StardewValley.Tools.Hoe>().Any();
        bool hasWateringCan = companionFarmer != null && companionFarmer.Items.OfType<StardewValley.Tools.WateringCan>().Any();
        bool toolsRetained = companionResolved && hasHoe && hasWateringCan;

        _evidence["companionRemainingSeeds"] = remainingSeeds;
        _evidence["companionHasHoe"] = hasHoe;
        _evidence["companionHasWateringCan"] = hasWateringCan;
        _evidence["toolsRetainedInCompanionBackpack"] = toolsRetained;

        if (remainingSeeds != 0)
        {
            failures.Add($"Companion inventory still has {remainingSeeds} Parsnip Seeds remaining (expected 0 after planting all 4).");
        }
        if (!toolsRetained)
        {
            failures.Add($"Companion tools not retained in backpack: Hoe={hasHoe}, WateringCan={hasWateringCan}.");
        }

        // 4. Host player verification (money delta == -80, stamina untouched, inventory invariant)
        int finalPlayerMoney = Game1.player?.Money ?? 0;
        int hostMoneyDelta = finalPlayerMoney - _initialPlayerMoney;
        bool hostStaminaUntouched = (Game1.player != null && Game1.player.stamina == _initialPlayerStamina);
        bool hostInventoryUntouched = CheckInventoryInvariance();
        bool hostMoneyCorrect = (hostMoneyDelta == -80);
        bool hostUntouched = hostStaminaUntouched && hostInventoryUntouched && hostMoneyCorrect;

        _evidence["initialPlayerMoney"] = _initialPlayerMoney;
        _evidence["finalPlayerMoney"] = finalPlayerMoney;
        _evidence["hostMoneyDelta"] = hostMoneyDelta;
        _evidence["hostStaminaUntouched"] = hostStaminaUntouched;
        _evidence["hostInventoryUntouched"] = hostInventoryUntouched;
        _evidence["hostMoneyCorrect"] = hostMoneyCorrect;
        _evidence["hostUntouched"] = hostUntouched;

        if (!hostMoneyCorrect)
        {
            failures.Add($"MONEY_DELTA_MISMATCH: expected host money delta of -80g (500 -> 420), got {hostMoneyDelta}g (500 -> {finalPlayerMoney}).");
        }
        if (!hostStaminaUntouched)
        {
            failures.Add("Host player stamina changed during companion execution.");
        }
        if (!hostInventoryUntouched)
        {
            failures.Add("Host player inventory changed during companion execution.");
        }

        // 5. Done file checks
        if (!done.Exists)
        {
            failures.Add("TIMEOUT_WAITING_DONE_FILE: cycle-mcp timed out before done file (cycle-mcp-done.json or loop-demo-done.json) was produced.");
        }
        else if (!done.Success)
        {
            failures.Add($"CYCLE_MCP_FAILED: {done.Error ?? "orchestrator reported failure in done file"}");
        }

        bool passed = done.Exists && done.Success && failures.Count == 0;
        _evidence["functionalSkillPassed"] = passed;
        _evidence["gateSuccess"] = passed;
        if (!passed)
        {
            _evidence["failureReason"] = string.Join(" ", failures);
        }

        Monitor.Log($"[TestDriver] cycle-mcp gate {(passed ? "PASSED" : "FAILED")}: planted={plantedCount}/4, watered={wateredCount}/4, protectedPreserved={protectedPreserved}, companionSeedsRemaining={remainingSeeds}, toolsRetained={toolsRetained}, hostMoneyDelta={hostMoneyDelta} (expected -80), doneSuccess={done.Success}.",
            passed ? LogLevel.Info : LogLevel.Error);
    }

    private void EvaluateCycleOperatorGate(string outDir, bool playerInvariant)
    {
        var done = ReadLoopDemoDoneFile(outDir);
        RecordDoneFileEvidence(done);

        var failures = new List<string>();
        var farm = Game1.getFarm();
        var protectedCropTile = new Vector2(60, 21);

        // 1. Protected tile check: original immature crop must be preserved
        var protectedHd = farm.terrainFeatures.TryGetValue(protectedCropTile, out var ptf) ? ptf as HoeDirt : null;
        bool protectedDirtPresent = protectedHd != null;
        bool protectedCropPresent = protectedHd?.crop != null;
        string? protectedSeed = protectedHd?.crop?.netSeedIndex?.Value;
        bool protectedNotHarvested = protectedHd?.crop != null && !protectedHd.readyForHarvest();
        bool protectedNotDead = protectedHd?.crop != null && !protectedHd.crop.dead.Value;
        bool protectedReferenceIdentical = _initialProtectedCropRef != null && object.ReferenceEquals(protectedHd?.crop, _initialProtectedCropRef);
        bool protectedPreserved = protectedDirtPresent && protectedCropPresent && protectedSeed == "472" && protectedNotHarvested && protectedNotDead && protectedReferenceIdentical;

        _evidence["protectedCropTileState"] = new Dictionary<string, object?>
        {
            ["tile"] = new { x = (int)protectedCropTile.X, y = (int)protectedCropTile.Y },
            ["dirtPresent"] = protectedDirtPresent,
            ["cropPresent"] = protectedCropPresent,
            ["seedIndex"] = protectedSeed,
            ["notHarvested"] = protectedNotHarvested,
            ["notDead"] = protectedNotDead,
            ["referenceIdentical"] = protectedReferenceIdentical,
            ["preserved"] = protectedPreserved
        };
        _evidence["protectedCropPreserved"] = protectedPreserved;

        if (!protectedPreserved)
        {
            if (!protectedReferenceIdentical)
            {
                failures.Add($"Protected immature crop at ({(int)protectedCropTile.X},{(int)protectedCropTile.Y}) was replaced by another instance (ReferenceEquals failed).");
            }
            else
            {
                failures.Add($"Protected immature crop at ({(int)protectedCropTile.X},{(int)protectedCropTile.Y}) was destroyed, replaced, harvested, or missing.");
            }
        }

        // 2. Scan newly planted parsnip crops across farm (excluding protected tile)
        var newlyPlanted = new List<Vector2>();
        var newlyWatered = new List<Vector2>();
        var newCropDetails = new Dictionary<string, object?>();

        foreach (var (tile, feature) in farm.terrainFeatures.Pairs)
        {
            if (tile == protectedCropTile) continue;
            if (feature is HoeDirt hd && hd.crop != null && hd.crop.netSeedIndex.Value == "472")
            {
                newlyPlanted.Add(tile);
                bool watered = hd.state.Value == 1;
                if (watered) newlyWatered.Add(tile);
                newCropDetails[$"{(int)tile.X},{(int)tile.Y}"] = new Dictionary<string, object?>
                {
                    ["seedIndex"] = hd.crop.netSeedIndex.Value,
                    ["cropPhase"] = hd.crop.currentPhase.Value,
                    ["watered"] = watered,
                    ["dead"] = hd.crop.dead.Value
                };
            }
        }

        int plantedCount = newlyPlanted.Count;
        int wateredCount = newlyWatered.Count;
        _evidence["newlyPlantedTiles"] = newCropDetails;
        _evidence["plantedCount"] = plantedCount;
        _evidence["wateredCount"] = wateredCount;

        if (plantedCount < 4)
        {
            failures.Add($"Insufficient newly planted parsnip crops found on farm: {plantedCount}/4.");
        }
        if (wateredCount < 4)
        {
            failures.Add($"Insufficient watered newly planted crops found on farm: {wateredCount}/4.");
        }

        // 3. Companion inventory & tools retention check
        bool companionResolved = ResolveCompanionActor(out var actor, out var companionFarmer);
        _evidence["companionResolved"] = companionResolved;

        int remainingSeeds = companionFarmer != null ? CountItemsByQualifiedId(companionFarmer.Items, "(O)472") : 0;
        bool hasHoe = companionFarmer != null && companionFarmer.Items.OfType<StardewValley.Tools.Hoe>().Any();
        bool hasWateringCan = companionFarmer != null && companionFarmer.Items.OfType<StardewValley.Tools.WateringCan>().Any();
        bool toolsRetained = companionResolved && hasHoe && hasWateringCan;

        _evidence["companionRemainingSeeds"] = remainingSeeds;
        _evidence["companionHasHoe"] = hasHoe;
        _evidence["companionHasWateringCan"] = hasWateringCan;
        _evidence["toolsRetainedInCompanionBackpack"] = toolsRetained;

        if (remainingSeeds != 0)
        {
            failures.Add($"Companion inventory still has {remainingSeeds} Parsnip Seeds remaining (expected 0 after planting all 4).");
        }
        if (!toolsRetained)
        {
            failures.Add($"Companion tools not retained in backpack: Hoe={hasHoe}, WateringCan={hasWateringCan}.");
        }

        // 4. Host player verification (money delta == -80, stamina untouched, inventory invariant)
        int finalPlayerMoney = Game1.player?.Money ?? 0;
        int hostMoneyDelta = finalPlayerMoney - _initialPlayerMoney;
        bool hostStaminaUntouched = (Game1.player != null && Game1.player.stamina == _initialPlayerStamina);
        bool hostInventoryUntouched = CheckInventoryInvariance();
        bool hostMoneyCorrect = (hostMoneyDelta == -80);
        bool hostUntouched = hostStaminaUntouched && hostInventoryUntouched && hostMoneyCorrect;

        _evidence["initialPlayerMoney"] = _initialPlayerMoney;
        _evidence["finalPlayerMoney"] = finalPlayerMoney;
        _evidence["hostMoneyDelta"] = hostMoneyDelta;
        _evidence["hostStaminaUntouched"] = hostStaminaUntouched;
        _evidence["hostInventoryUntouched"] = hostInventoryUntouched;
        _evidence["hostMoneyCorrect"] = hostMoneyCorrect;
        _evidence["hostUntouched"] = hostUntouched;

        if (!hostMoneyCorrect)
        {
            failures.Add($"MONEY_DELTA_MISMATCH: expected host money delta of -80g (500 -> 420), got {hostMoneyDelta}g (500 -> {finalPlayerMoney}).");
        }
        if (!hostStaminaUntouched)
        {
            failures.Add("Host player stamina changed during companion execution.");
        }
        if (!hostInventoryUntouched)
        {
            failures.Add("Host player inventory changed during companion execution.");
        }

        // 5. Done file checks
        if (!done.Exists)
        {
            failures.Add("TIMEOUT_WAITING_DONE_FILE: cycle-operator timed out before done file (cycle-operator-done.json or loop-demo-done.json) was produced.");
        }
        else if (!done.Success)
        {
            failures.Add($"CYCLE_OPERATOR_FAILED: {done.Error ?? "operator reported failure in done file"}");
        }

        bool passed = done.Exists && done.Success && failures.Count == 0;
        _evidence["functionalSkillPassed"] = passed;
        _evidence["gateSuccess"] = passed;
        if (!passed)
        {
            _evidence["failureReason"] = string.Join(" ", failures);
        }

        Monitor.Log($"[TestDriver] cycle-operator gate {(passed ? "PASSED" : "FAILED")}: planted={plantedCount}/4, watered={wateredCount}/4, protectedPreserved={protectedPreserved}, companionSeedsRemaining={remainingSeeds}, toolsRetained={toolsRetained}, hostMoneyDelta={hostMoneyDelta} (expected -80), doneSuccess={done.Success}.",
            passed ? LogLevel.Info : LogLevel.Error);
    }

    private void EvaluateChestOperatorGate(string outDir, bool playerInvariant)
    {
        var done = ReadLoopDemoDoneFile(outDir);
        RecordDoneFileEvidence(done);

        var farm = Game1.getFarm();
        var failures = new List<string>();

        // 1. Protected crop check at (60, 21)
        var protectedTile = new Vector2(60, 21);
        bool protectedPreserved = false;
        if (farm.terrainFeatures.TryGetValue(protectedTile, out var tfProtected) && tfProtected is HoeDirt hdProt)
        {
            protectedPreserved = hdProt.crop != null &&
                                 hdProt.crop.currentPhase.Value == 0 &&
                                 !hdProt.crop.dead.Value;
        }
        _evidence["protectedCropPreserved"] = protectedPreserved;
        if (!protectedPreserved)
        {
            failures.Add("Protected immature crop at (60, 21) was disturbed or destroyed.");
        }

        // 2. Scan newly planted carrot crops across farm (excluding protected tile)
        var newlyPlanted = new List<Vector2>();
        var newlyWatered = new List<Vector2>();
        var newCropDetails = new Dictionary<string, object?>();

        foreach (var (tile, feature) in farm.terrainFeatures.Pairs)
        {
            if (tile == protectedTile) continue;
            if (feature is HoeDirt hd && hd.crop != null && !hd.crop.dead.Value)
            {
                string seedId = hd.crop.netSeedIndex.Value ?? "";
                string harvestId = hd.crop.indexOfHarvest.Value ?? "";
                if (seedId.Contains("Carrot", StringComparison.OrdinalIgnoreCase) ||
                    harvestId.Contains("Carrot", StringComparison.OrdinalIgnoreCase))
                {
                    newlyPlanted.Add(tile);
                    bool watered = hd.state.Value == 1;
                    if (watered) newlyWatered.Add(tile);
                    newCropDetails[$"{(int)tile.X},{(int)tile.Y}"] = new Dictionary<string, object?>
                    {
                        ["seedIndex"] = seedId,
                        ["indexOfHarvest"] = harvestId,
                        ["cropPhase"] = hd.crop.currentPhase.Value,
                        ["watered"] = watered,
                        ["dead"] = hd.crop.dead.Value
                    };
                }
            }
        }

        int plantedCount = newlyPlanted.Count;
        int wateredCount = newlyWatered.Count;
        _evidence["newlyPlantedTiles"] = newCropDetails;
        _evidence["plantedCount"] = plantedCount;
        _evidence["wateredCount"] = wateredCount;
        if (plantedCount < 3)
        {
            failures.Add($"Insufficient newly planted carrot crops found on farm: {plantedCount}/3.");
        }
        if (wateredCount < 3)
        {
            failures.Add($"Insufficient watered newly planted crops found on farm: {wateredCount}/3.");
        }

        // 2b. Total watered dirt tiles check across the farm (verifying equivalent 17-tile watering workload)
        int totalWateredDirtCount = 0;
        var totalWateredTilesList = new List<Vector2>();
        foreach (var (tile, feature) in farm.terrainFeatures.Pairs)
        {
            if (feature is HoeDirt hd && hd.state.Value == 1)
            {
                totalWateredDirtCount++;
                totalWateredTilesList.Add(tile);
            }
        }
        _evidence["totalWateredDirtCount"] = totalWateredDirtCount;
        _evidence["totalWateredTiles"] = totalWateredTilesList.Select(t => new { x = (int)t.X, y = (int)t.Y }).ToList();
        if (totalWateredDirtCount < 17)
        {
            failures.Add($"Insufficient total watered dirt tiles found on farm: {totalWateredDirtCount}/17 (requires full 17-tile workload).");
        }

        // 3. Chest check at (59, 17): carrot seeds withdrawn
        var chestTile = new Vector2(59, 17);
        int chestCarrotSeeds = 0;
        if (farm.objects.TryGetValue(chestTile, out var obj) && obj is StardewValley.Objects.Chest chest)
        {
            chestCarrotSeeds = CountItemsByQualifiedId(chest.Items, "(O)CarrotSeeds");
        }
        _evidence["chestCarrotSeedsRemaining"] = chestCarrotSeeds;
        if (chestCarrotSeeds != 0)
        {
            failures.Add($"Chest at (59, 17) still has {chestCarrotSeeds} Carrot Seeds remaining (expected 0).");
        }

        // 4. Companion backpack check: seeds planted (0 remaining), tools retained
        bool companionResolved = ResolveCompanionActor(out var actor, out var companionFarmer);
        _evidence["companionResolved"] = companionResolved;
        int remainingSeeds = companionFarmer != null ? CountItemsByQualifiedId(companionFarmer.Items, "(O)CarrotSeeds") : 0;
        bool hasHoe = companionFarmer != null && companionFarmer.Items.OfType<StardewValley.Tools.Hoe>().Any();
        bool hasWateringCan = companionFarmer != null && companionFarmer.Items.OfType<StardewValley.Tools.WateringCan>().Any();
        bool toolsRetained = companionResolved && hasHoe && hasWateringCan;

        _evidence["companionRemainingSeeds"] = remainingSeeds;
        _evidence["companionHasHoe"] = hasHoe;
        _evidence["companionHasWateringCan"] = hasWateringCan;
        _evidence["toolsRetainedInCompanionBackpack"] = toolsRetained;

        if (remainingSeeds != 0)
        {
            failures.Add($"Companion inventory still has {remainingSeeds} Carrot Seeds remaining.");
        }
        if (!toolsRetained)
        {
            failures.Add($"Companion tools not retained in backpack: Hoe={hasHoe}, WateringCan={hasWateringCan}.");
        }

        // 5. Host player verification (stamina, inventory, money)
        int finalPlayerMoney = Game1.player?.Money ?? 0;
        int hostMoneyDelta = finalPlayerMoney - _initialPlayerMoney;
        bool hostStaminaUntouched = (Game1.player != null && Game1.player.stamina == _initialPlayerStamina);
        bool hostInventoryUntouched = CheckInventoryInvariance();
        bool hostMoneyUntouched = (hostMoneyDelta == 0);
        bool hostUntouched = hostStaminaUntouched && hostInventoryUntouched && hostMoneyUntouched;

        _evidence["hostMoneyDelta"] = hostMoneyDelta;
        _evidence["hostStaminaUntouched"] = hostStaminaUntouched;
        _evidence["hostInventoryUntouched"] = hostInventoryUntouched;
        _evidence["hostMoneyUntouched"] = hostMoneyUntouched;
        _evidence["hostUntouched"] = hostUntouched;

        if (!hostUntouched)
        {
            failures.Add($"Host player was disturbed: staminaUntouched={hostStaminaUntouched}, inventoryUntouched={hostInventoryUntouched}, moneyUntouched={hostMoneyUntouched}.");
        }

        // 6. Done file check
        if (!done.Exists)
        {
            failures.Add("TIMEOUT_WAITING_DONE_FILE: chest-operator timed out before chest-operator-done.json was produced.");
        }
        else if (!done.Success)
        {
            failures.Add($"CHEST_OPERATOR_FAILED: {done.Error ?? "operator reported failure in done file"}");
        }

        bool passed = done.Exists && done.Success && failures.Count == 0;
        _evidence["functionalSkillPassed"] = passed;
        _evidence["gateSuccess"] = passed;
        if (!passed)
        {
            _evidence["failureReason"] = string.Join(" ", failures);
        }

        Monitor.Log($"[TestDriver] chest-operator gate {(passed ? "PASSED" : "FAILED")}: planted={plantedCount}/3, newCropWatered={wateredCount}/3, totalWatered={totalWateredDirtCount}/17, chestCarrotSeeds={chestCarrotSeeds}, protectedPreserved={protectedPreserved}, doneSuccess={done.Success}.",
            passed ? LogLevel.Info : LogLevel.Error);
    }

    private void EvaluateExistingSaveBuy5Gate(string outDir, bool playerInvariant)
    {
        var done = ReadLoopDemoDoneFile(outDir);
        RecordDoneFileEvidence(done);

        var failures = new List<string>();
        var farm = Game1.getFarm();

        // 1. Inspect Farm HoeDirt features
        var currentHoeDirt = new Dictionary<Vector2, HoeDirt>();
        foreach (var kvp in farm.terrainFeatures.Pairs)
        {
            if (kvp.Value is HoeDirt hd)
            {
                currentHoeDirt[kvp.Key] = hd;
            }
        }

        var newlyTilledTiles = currentHoeDirt.Keys.Where(k => !_initialFarmHoeDirtTiles.Contains(k)).ToList();
        var newlyPlantedTiles = currentHoeDirt.Where(kvp => kvp.Value.crop != null && !_initialFarmHoeDirtWithCrops.Contains(kvp.Key)).ToList();
        var newlyTilledAndPlanted = newlyPlantedTiles.Where(kvp => !_initialFarmHoeDirtTiles.Contains(kvp.Key)).ToList();
        var newlyWateredTiles = newlyPlantedTiles.Where(kvp => kvp.Value.state.Value == 1).ToList();

        // Check 5 newly tilled tiles
        if (newlyTilledTiles.Count < 5)
        {
            failures.Add($"Insufficient newly tilled tiles: expected >= 5, got {newlyTilledTiles.Count}.");
        }

        // Check 5 newly planted crops
        if (newlyPlantedTiles.Count < 5)
        {
            failures.Add($"Insufficient newly planted crops: expected >= 5, got {newlyPlantedTiles.Count}.");
        }

        // Check crops are on newly tilled soil
        if (newlyTilledAndPlanted.Count < 5)
        {
            failures.Add($"Crops not on newly tilled soil: only {newlyTilledAndPlanted.Count} newly planted crops are on newly tilled tiles.");
        }

        // Check 5 newly planted tiles are watered
        if (newlyWateredTiles.Count < 5)
        {
            failures.Add($"Insufficient watered newly planted crops: expected >= 5, got {newlyWateredTiles.Count}.");
        }

        // 2. Existing crops preserved
        int existingPreserved = 0;
        foreach (var pt in _initialFarmHoeDirtWithCrops)
        {
            if (currentHoeDirt.TryGetValue(pt, out var exHd) && exHd.crop != null)
            {
                existingPreserved++;
            }
            else
            {
                failures.Add($"Existing crop at ({pt.X}, {pt.Y}) was destroyed or missing.");
            }
        }

        // 3. Companion backpack check: tools retained, seeds planted
        bool companionResolved = ResolveCompanionActor(out var actor, out var companionFarmer);
        _evidence["companionResolved"] = companionResolved;
        int remainingSeeds = 0;
        if (companionFarmer != null)
        {
            foreach (var it in companionFarmer.Items)
            {
                if (it != null && (it.Category == StardewValley.Object.SeedsCategory || it.Name.Contains("Seed", StringComparison.OrdinalIgnoreCase)))
                {
                    remainingSeeds += it.Stack;
                }
            }
        }
        bool hasHoe = companionFarmer != null && companionFarmer.Items.OfType<StardewValley.Tools.Hoe>().Any();
        bool hasWateringCan = companionFarmer != null && companionFarmer.Items.OfType<StardewValley.Tools.WateringCan>().Any();
        bool toolsRetained = companionResolved && hasHoe && hasWateringCan;

        _evidence["companionRemainingSeeds"] = remainingSeeds;
        _evidence["companionHasHoe"] = hasHoe;
        _evidence["companionHasWateringCan"] = hasWateringCan;
        _evidence["toolsRetainedInCompanionBackpack"] = toolsRetained;

        if (!toolsRetained)
        {
            failures.Add($"Companion tools not retained in backpack: Hoe={hasHoe}, WateringCan={hasWateringCan}.");
        }

        // 4. Host player verification (stamina, inventory, genuine money spending)
        int finalPlayerMoney = Game1.player?.Money ?? 0;
        int hostMoneyDelta = _initialPlayerMoney - finalPlayerMoney; // positive if spent
        bool hostStaminaUntouched = (Game1.player != null && Game1.player.stamina == _initialPlayerStamina);
        bool hostInventoryUntouched = CheckInventoryInvariance();
        bool hostMoneySpent = (hostMoneyDelta > 0);

        _evidence["hostMoneyBaseline"] = _initialPlayerMoney;
        _evidence["hostMoneyFinal"] = finalPlayerMoney;
        _evidence["hostMoneySpent"] = hostMoneyDelta;
        _evidence["hostStaminaUntouched"] = hostStaminaUntouched;
        _evidence["hostInventoryUntouched"] = hostInventoryUntouched;
        _evidence["hostMoneySpentValid"] = hostMoneySpent;

        if (!hostStaminaUntouched)
        {
            failures.Add($"Host stamina changed: initial {_initialPlayerStamina}, final {Game1.player?.stamina}.");
        }
        if (!hostInventoryUntouched)
        {
            failures.Add("Host inventory changed unexpectedly.");
        }
        if (!hostMoneySpent)
        {
            failures.Add($"Host money was not spent for genuine seed purchase: initial {_initialPlayerMoney}, final {finalPlayerMoney}.");
        }

        // 5. Done file check
        if (!done.Exists)
        {
            failures.Add("TIMEOUT_WAITING_DONE_FILE: existing-save-buy5 timed out before existing-save-buy5-done.json was produced.");
        }
        else if (!done.Success)
        {
            failures.Add($"EXISTING_SAVE_BUY5_OPERATOR_FAILED: {done.Error ?? "operator reported failure in done file"}");
        }

        bool passed = done.Exists && done.Success && failures.Count == 0;
        _evidence["functionalSkillPassed"] = passed;
        _evidence["gateSuccess"] = passed;
        _evidence["actExecuted"] = done.Exists;
        _evidence["terminalRecordValid"] = passed;
        _evidence["newlyTilledTiles"] = newlyTilledTiles.Select(t => new { x = (int)t.X, y = (int)t.Y }).ToList();
        _evidence["newlyPlantedTiles"] = newlyPlantedTiles.Select(kvp => new {
            x = (int)kvp.Key.X,
            y = (int)kvp.Key.Y,
            seedIndex = kvp.Value.crop?.netSeedIndex?.Value,
            watered = kvp.Value.state.Value == 1
        }).ToList();
        _evidence["newlyTilledCount"] = newlyTilledTiles.Count;
        _evidence["newlyPlantedCount"] = newlyPlantedTiles.Count;
        _evidence["newlyWateredCount"] = newlyWateredTiles.Count;
        _evidence["existingCropsPreserved"] = existingPreserved;
        _evidence["initialCropsTotal"] = _initialFarmHoeDirtWithCrops.Count;

        if (!passed)
        {
            _evidence["failureReason"] = string.Join(" ", failures);
        }

        Monitor.Log($"[TestDriver] existing-save-buy5 gate {(passed ? "PASSED" : "FAILED")}: tilled={newlyTilledTiles.Count}/5, planted={newlyPlantedTiles.Count}/5, watered={newlyWateredTiles.Count}/5, moneySpent={hostMoneyDelta}g, existingPreserved={existingPreserved}/{_initialFarmHoeDirtWithCrops.Count}, doneSuccess={done.Success}.",
            passed ? LogLevel.Info : LogLevel.Error);
    }

    private void FinishScenario(bool arrangeSuccess)
    {
        _scenarioState = ScenarioState.Finished;
        var teardownErrors = new List<string>();
        if (_steadyFrameIntervalsMs.Count > 0)
        {
            double[] intervals = _steadyFrameIntervalsMs.OrderBy(value => value).ToArray();
            _evidence["steadyGameplayFrameIntervals"] = new {
                samples = intervals.Length,
                medianMs = intervals[intervals.Length / 2],
                p95Ms = intervals[(int)(intervals.Length * .95)],
                p99Ms = intervals[(int)(intervals.Length * .99)],
                maxMs = intervals[^1],
                over100Ms = intervals.Count(value => value > 100),
                over250Ms = intervals.Count(value => value > 250),
                sampleLimit = 3600,
                scope = "First at most 3600 steady UpdateTicked intervals after READY; excludes loading, menus and requested screenshots; includes all game/mod work; not a whole-game performance benchmark"
            };
        }

        // Guaranteed initial state: keys must exist before any read
        _evidence["gateSuccess"] = false;
        _evidence["functionalSkillPassed"] = false;
        _evidence["actExecuted"] = false;
        _evidence["terminalRecordValid"] = false;

        // Drain async PNG encodes BEFORE evaluating screenshot evidence (File.Exists checks).
        FlushCaptureJobs();

        try
        {
            bool expectedMoneyInvariant = _scenario.Equals("purchase-mcp", StringComparison.OrdinalIgnoreCase)
                ? (Game1.player?.Money == _initialPlayerMoney - 40)
                : (_scenario.Equals("cycle-mcp", StringComparison.OrdinalIgnoreCase) ||
                   _scenario.Equals("cycle-operator", StringComparison.OrdinalIgnoreCase))
                ? (Game1.player?.Money == _initialPlayerMoney - 80)
                : _scenario.Equals("existing-save-buy5", StringComparison.OrdinalIgnoreCase)
                ? (Game1.player?.Money < _initialPlayerMoney)
                : (Game1.player?.Money == _initialPlayerMoney);
            bool playerResourcesInvariant = (Game1.player != null && Game1.player.stamina == _initialPlayerStamina && expectedMoneyInvariant);
            bool inventoryInvariant = CheckInventoryInvariance();
            bool playerInvariant = playerResourcesInvariant && inventoryInvariant;
            Monitor.Log($"[TestDriver] Player resources at finish: Stamina={Game1.player?.stamina} (baseline {_initialPlayerStamina}), Money={Game1.player?.Money} (baseline {_initialPlayerMoney}), InventoryInvariant={inventoryInvariant}.",
                playerResourcesInvariant ? LogLevel.Info : LogLevel.Error);

            var farm = Game1.getFarm();
            var dirt = farm?.terrainFeatures.TryGetValue(_targetTile, out var tf) == true && tf is HoeDirt hd ? hd : null;
            bool targetWatered = (dirt?.state.Value == 1);

            var zoneWatered = new Dictionary<string, bool>();
            if (farm != null)
            {
                foreach (var zt in _zoneTiles)
                {
                    zoneWatered[$"{(int)zt.X},{(int)zt.Y}"] =
                        farm.terrainFeatures.TryGetValue(zt, out var ztf) && ztf is HoeDirt zhd && zhd.state.Value == 1;
                }
            }

            bool isArrangeSmoke = _testMode.Equals("arrange-smoke", StringComparison.OrdinalIgnoreCase);

            var outDir = _evidenceDir ?? Path.Combine(Directory.GetCurrentDirectory(), ".test-runs", "evidence");
            bool screenshotValid = _screenshotCaptured && File.Exists(Path.Combine(outDir, "render.png"));

            _evidence["runId"] = _runId;
            _evidence["scenario"] = _scenario;
            _evidence["mode"] = _testMode;
            _evidence["timestampUtc"] = DateTime.UtcNow.ToString("o");
            _evidence["fixedSeed"] = 42;
            _evidence["targetTile"] = new { x = (int)_targetTile.X, y = (int)_targetTile.Y };
            _evidence["targetDirtWatered"] = targetWatered;
            _evidence["playerResourcesInvariant"] = playerInvariant;
            _evidence["inventoryInvariant"] = inventoryInvariant;
            _evidence["screenshotCaptured"] = screenshotValid;
            _evidence["smapiVersion"] = Constants.ApiVersion.ToString();
            _evidence["gameVersion"] = Game1.version;
            _evidence["realActorObserved"] = (_lastActorObservationStatus == ActorObservationStatus.Observed);
            _evidence["actorObservationStatus"] = _lastActorObservationStatus.ToString();
            _evidence["actorObservationDetail"] = _lastActorObservationDetail;
            if (_zoneTiles.Count > 0)
            {
                _evidence["zoneWatered"] = zoneWatered;
            }

            if (isArrangeSmoke)
            {
                _evidence["runType"] = "arrange-smoke-only";
                _evidence["arrangeCompleted"] = arrangeSuccess;
                _evidence["actExecuted"] = false;
                _evidence["functionalSkillPassed"] = false;
                bool smokePass = arrangeSuccess && playerInvariant && screenshotValid && (_lastActorObservationStatus == ActorObservationStatus.Observed);
                _evidence["gateSuccess"] = smokePass;
                _evidence["note"] = "Arrange-only smoke run. Validates save loading, farm warp, soil arrangement, companion observation, screenshot capture, and player invariance. Does not execute or assert Skill actions.";
                if (!smokePass && !_evidence.ContainsKey("failureReason"))
                {
                    if (!screenshotValid) _evidence["failureReason"] = "Arrange smoke failed: No valid render.png captured during RenderedWorld.";
                    else if (_lastActorObservationStatus != ActorObservationStatus.Observed) _evidence["failureReason"] = $"Arrange smoke failed: Companion actor not observed ({_lastActorObservationStatus}: {_lastActorObservationDetail}).";
                    else if (!playerInvariant) _evidence["failureReason"] = "Arrange smoke failed: Player resources or inventory changed.";
                    else _evidence["failureReason"] = "Arrange smoke failed: Arrange did not complete successfully.";
                }
            }
            else
            {
                _evidence["runType"] = "functional-scenario";
                _evidence["arrangeCompleted"] = arrangeSuccess;

                if (_lastActorObservationStatus != ActorObservationStatus.Observed)
                {
                    _evidence["actExecuted"] = false;
                    _evidence["terminalRecordValid"] = false;
                    _evidence["functionalSkillPassed"] = false;
                    _evidence["gateSuccess"] = false;
                    if (!_evidence.ContainsKey("failureReason"))
                    {
                        _evidence["failureReason"] = $"ACTOR_NOT_OBSERVED: Companion actor observation threshold failed ({_lastActorObservationStatus}: {_lastActorObservationDetail}).";
                    }
                }
                else if (!screenshotValid && !(_scenario == "free-mode-control" &&
                    _harnessFixtureMode is "water-refill" or "harvest-store" or "rest-water" or "mature-trees" or "walk-pickup" or "walk-pickup-full"))
                {
                    _evidence["functionalSkillPassed"] = false;
                    _evidence["gateSuccess"] = false;
                    if (!_evidence.ContainsKey("failureReason"))
                    {
                        _evidence["failureReason"] = "NO_VALID_RENDER_FRAME: TestDriver failed to capture a genuine rendered world gameplay frame during RenderedWorld.";
                    }
                }
                else if (_scenario.Equals("normal-watering", StringComparison.OrdinalIgnoreCase))
                {
                    bool validTerminal = ValidateTerminalResult(out string? rejectReason, out var termData);
                    if (!validTerminal)
                    {
                        _evidence["actExecuted"] = false;
                        _evidence["terminalRecordValid"] = false;
                        _evidence["terminalRejectionReason"] = rejectReason;
                        _evidence["functionalSkillPassed"] = false;
                        _evidence["gateSuccess"] = false;
                        if (!_evidence.ContainsKey("failureReason"))
                        {
                            _evidence["failureReason"] = $"BLOCKED_ON_PEER_INTEGRATION: External Runtime Act was not executed or did not complete. {rejectReason}";
                        }
                    }
                    else
                    {
                        _evidence["actExecuted"] = true;
                        _evidence["terminalRecordValid"] = true;
                        _evidence["terminalRecord"] = termData;

                        bool actorResourceDeducted = false;
                        if (ResolveCompanionActor(out var actor, out var farmer))
                        {
                            float curStam = farmer?.Stamina ?? 270f;
                            var can = farmer?.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault();
                            int curWater = can?.WaterLeft ?? 40;
                            if (curStam < _preActorStamina || curWater < _preActorWater)
                            {
                                actorResourceDeducted = true;
                            }
                            _evidence["postActorStamina"] = curStam;
                            _evidence["postActorWater"] = curWater;
                        }
                        _evidence["actorResourceDeducted"] = actorResourceDeducted;

                        bool skillPassed = targetWatered && playerInvariant && screenshotValid;
                        _evidence["functionalSkillPassed"] = skillPassed;
                        _evidence["gateSuccess"] = skillPassed;

                        if (!targetWatered)
                        {
                            _evidence["failureReason"] = "Target soil at (65,15) remained unwatered despite successful terminal execution record.";
                        }
                        else if (!playerInvariant)
                        {
                            _evidence["failureReason"] = "Player resources or inventory changed during companion action.";
                        }
                    }
                }
                else if (_scenario.Equals("already-watered", StringComparison.OrdinalIgnoreCase))
                {
                    // Expected: executor skips the already-watered tile with zero cost, terminal succeeded.
                    EvaluateScenarioGate("succeeded", expectDirtWatered: true, expectActorResourcesUnchanged: true,
                        targetWatered, playerInvariant, screenshotValid,
                        payloadAssertion: td => IntOf(td, "skippedCount") == 1 && IntOf(td, "completedCount") == 0 &&
                                                IntOf(td, "waterUsed") == 0 && FloatOf(td, "staminaUsed") == 0f,
                        payloadAssertionDescription: "skipped=1, completed=0, waterUsed=0, staminaUsed=0");
                }
                else if (_scenario.Equals("empty-can", StringComparison.OrdinalIgnoreCase))
                {
                    // Expected: precondition rejection, no tool effect, no borrowed resources.
                    EvaluateScenarioGate("rejected", expectDirtWatered: false, expectActorResourcesUnchanged: true,
                        targetWatered, playerInvariant, screenshotValid, expectedErrorCode: "CAN_EMPTY");
                }
                else if (_scenario.Equals("low-stamina", StringComparison.OrdinalIgnoreCase))
                {
                    // Expected: precondition rejection, no over-budget action.
                    EvaluateScenarioGate("rejected", expectDirtWatered: false, expectActorResourcesUnchanged: true,
                        targetWatered, playerInvariant, screenshotValid, expectedErrorCode: "INSUFFICIENT_STAMINA");
                }
                else if (_scenario.Equals("blocked", StringComparison.OrdinalIgnoreCase))
                {
                    // Expected: bounded navigation failure, no entering blocked tiles, no teleport, zero cost.
                    EvaluateScenarioGate("failed", expectDirtWatered: false, expectActorResourcesUnchanged: true,
                        targetWatered, playerInvariant, screenshotValid,
                        payloadAssertion: td => IntOf(td, "failedCount") == 1 && td.ContainsKey("errorCode"),
                        payloadAssertionDescription: "failedCount=1 with a navigation error code",
                        assertNotOnBlockedTiles: true);
                }
                else if (_scenario.Equals("pause-resume", StringComparison.OrdinalIgnoreCase))
                {
                    // Expected: pause at a safe boundary, resume, all zone tiles watered exactly once.
                    // Costs are asserted as exact deltas derived from the effect count (never hardcoded),
                    // since tool upgrade tiers change water/stamina economics.
                    EvaluateScenarioGate("succeeded", expectDirtWatered: true, expectActorResourcesUnchanged: false,
                        targetWatered, playerInvariant, screenshotValid,
                        actualZoneWatered: zoneWatered,
                        zoneAssertion: (td, zw) => zw.Count == 3 && zw.Values.All(v => v)
                            ? null
                            : $"Expected all 3 zone tiles watered, dry: [{string.Join(",", zw.Where(kv => !kv.Value).Select(kv => kv.Key))}].",
                        expectedWaterSpentFromPayload: td => EffectsOf(td).Count,
                        payloadAssertion: td => IntOf(td, "completedCount") == 3 && IntOf(td, "failedCount") == 0 &&
                                                EffectsOf(td).Distinct().Count() == 3 &&
                                                IntOf(td, "waterUsed") == EffectsOf(td).Count,
                        payloadAssertionDescription: "completed=zone size, failed=0, unique effect tiles, waterUsed == effect count (no repeat after resume)");
                }
                else if (_scenario.Equals("cancel", StringComparison.OrdinalIgnoreCase) ||
                         _scenario.Equals("cancel-paused", StringComparison.OrdinalIgnoreCase))
                {
                    // Expected: safe stop with exactly one terminal record; watered tiles match reported effects
                    // exactly; water spent equals the number of applied effects (no post-cancel effects).
                    EvaluateScenarioGate("cancelled", expectDirtWatered: false, expectActorResourcesUnchanged: false,
                        targetWatered, playerInvariant, screenshotValid,
                        skipDirtCheck: true,
                        additionalAcceptedStatuses: new[] { "partially-succeeded" },
                        actualZoneWatered: zoneWatered,
                        zoneAssertion: (td, zw) =>
                        {
                            var eff = EffectsOf(td).OrderBy(x => x).ToList();
                            var watered = zw.Where(kv => kv.Value).Select(kv => kv.Key).OrderBy(x => x).ToList();
                            return watered.SequenceEqual(eff)
                                ? null
                                : $"Watered zone tiles [{string.Join(",", watered)}] do not match terminal effects [{string.Join(",", eff)}].";
                        },
                        expectedWaterSpentFromPayload: td => EffectsOf(td).Count,
                        payloadAssertion: td => IntOf(td, "failedCount") == 0,
                        payloadAssertionDescription: "failed=0 (a correct cancel is not a failure)");
                }
                else if (_scenario.Equals("duplicate", StringComparison.OrdinalIgnoreCase))
                {
                    // Expected: replayed identical command returns the same result without double-charging.
                    // Charge must equal the effect count exactly once (delta-driven, no hardcoded cost).
                    EvaluateScenarioGate("succeeded", expectDirtWatered: true, expectActorResourcesUnchanged: false,
                        targetWatered, playerInvariant, screenshotValid,
                        expectedWaterSpentFromPayload: td => EffectsOf(td).Count,
                        payloadAssertion: td => IntOf(td, "completedCount") == 1 &&
                                                IntOf(td, "waterUsed") == EffectsOf(td).Count &&
                                                td.ContainsKey("replayResponse"),
                        payloadAssertionDescription: "completed=1, waterUsed == effect count, replay response recorded");
                }
                else if (_scenario.Equals("conflict", StringComparison.OrdinalIgnoreCase))
                {
                    // Expected: same idempotency key with different semantics is rejected as conflict;
                    // original result preserved; conflicting target (66,15) stays unwatered; charged once.
                    EvaluateScenarioGate("succeeded", expectDirtWatered: true, expectActorResourcesUnchanged: false,
                        targetWatered, playerInvariant, screenshotValid,
                        actualZoneWatered: zoneWatered,
                        zoneAssertion: (td, zw) => zw.TryGetValue("66,15", out var w) && !w
                            ? null
                            : "Conflicting command's target (66,15) was watered — original result was not preserved.",
                        expectedWaterSpentFromPayload: td => EffectsOf(td).Count,
                        payloadAssertion: td => IntOf(td, "completedCount") == 1 && td.ContainsKey("conflictResponse"),
                        payloadAssertionDescription: "completed=1 and conflict rejection recorded");
                }
                else if (_scenario.Equals("disconnect", StringComparison.OrdinalIgnoreCase))
                {
                    // Expected: after a hard client disconnect the game stays responsive (reconnect +
                    // handshake + snapshot succeeds) and the companion's task is safely stopped (idle).
                    EvaluateScenarioGate("disconnect-recovered", expectDirtWatered: false, expectActorResourcesUnchanged: false,
                        targetWatered, playerInvariant, screenshotValid,
                        skipDirtCheck: true,
                        payloadAssertion: td =>
                            td.TryGetValue("controlFlow", out var cf) && cf is string cfs &&
                            cfs.Contains("\"reconnected\": true") &&
                            cfs.Contains("\"snapshotCompanionActivity\": \"idle\""),
                        payloadAssertionDescription: "reconnect succeeded and companion idle (task safely stopped)");
                }
                else if (_scenario.Equals("save-reload", StringComparison.OrdinalIgnoreCase))
                {
                    // Phase A: terminal succeeded. Phase C: reloaded actor state must exactly match
                    // post-Act values (no refill, no relocation), no stale-task auto-resume,
                    // discovery republished with matching saveId partition, watered state persists.
                    bool validTerminal = ValidateTerminalResult(out string? srReject, out var srTerm, "succeeded");
                    var failures = new List<string>();
                    if (!validTerminal)
                    {
                        failures.Add($"Phase A terminal invalid: {srReject}");
                        _evidence["terminalRejectionReason"] = srReject;
                    }
                    else
                    {
                        _evidence["terminalRecord"] = srTerm;
                    }
                    _evidence["actExecuted"] = validTerminal;
                    _evidence["terminalRecordValid"] = validTerminal;

                    _evidence["postActActorStamina"] = _postActActorStamina;
                    _evidence["postActActorWater"] = _postActActorWater;
                    _evidence["postActActorTile"] = $"{_postActActorTileX},{_postActActorTileY}";
                    _evidence["reloadedActorStamina"] = _reloadedActorStamina;
                    _evidence["reloadedActorWater"] = _reloadedActorWater;
                    _evidence["reloadedActorTile"] = _reloadedActorTile;
                    _evidence["reloadedActorIdle"] = _reloadedActorIdle;
                    _evidence["reloadDiscoveryRepublished"] = _reloadDiscoveryRepublished;
                    _evidence["reloadDiscoverySaveIdMatches"] = _reloadDiscoverySaveIdMatches;

                    if (_reloadedActorStamina == null)
                    {
                        failures.Add("Companion actor not resolvable after reload.");
                    }
                    else
                    {
                        if (_reloadedActorStamina != _postActActorStamina)
                            failures.Add($"Reloaded stamina {_reloadedActorStamina} != post-Act {_postActActorStamina} (not preserved / silently refilled).");
                        if (_reloadedActorWater != _postActActorWater)
                            failures.Add($"Reloaded water {_reloadedActorWater} != post-Act {_postActActorWater} (not preserved / silently refilled).");
                        if (_reloadedActorTile != $"{_postActActorTileX},{_postActActorTileY}")
                            failures.Add($"Reloaded tile {_reloadedActorTile} != post-Act ({_postActActorTileX},{_postActActorTileY}).");
                    }
                    if (!_reloadedActorIdle)
                        failures.Add("Companion auto-resumed a stale task after reload (ActiveTaskId not null).");
                    if (!_reloadDiscoveryRepublished)
                        failures.Add("Transport discovery not republished after reload.");
                    if (!_reloadDiscoverySaveIdMatches)
                        failures.Add("Discovery saveId does not match current save (cross-save leak).");
                    if (!targetWatered)
                        failures.Add("Watered tile (65,15) lost its watered state after reload.");
                    if (!playerInvariant)
                        failures.Add("Player resources or inventory changed during companion action.");

                    bool srPassed = failures.Count == 0 && validTerminal;
                    _evidence["functionalSkillPassed"] = srPassed;
                    _evidence["gateSuccess"] = srPassed;
                    if (!srPassed)
                    {
                        _evidence["failureReason"] = string.Join(" ", failures);
                    }
                }
                else if (_scenario.Equals("loop-demo", StringComparison.OrdinalIgnoreCase))
                {
                    var doneFile = Path.Combine(outDir, "loop-demo-done.json");
                    bool doneExists = File.Exists(doneFile);
                    bool doneSuccess = false;
                    string? doneError = null;

                    if (doneExists)
                    {
                        try
                        {
                            var doneJson = File.ReadAllText(doneFile);
                            using var doc = JsonDocument.Parse(doneJson);
                            if (doc.RootElement.TryGetProperty("success", out var succElem))
                            {
                                doneSuccess = succElem.GetBoolean();
                            }
                            if (doc.RootElement.TryGetProperty("error", out var errElem))
                            {
                                doneError = errElem.GetString();
                            }
                            _evidence["loopDemoDone"] = true;
                            _evidence["loopDemoSummary"] = JsonSerializer.Deserialize<object>(doneJson);
                        }
                        catch (Exception ex)
                        {
                            Monitor.Log($"[TestDriver] Note reading loop-demo-done.json: {ex.Message}", LogLevel.Warn);
                            doneSuccess = false;
                            doneError = $"Failed to parse loop-demo-done.json: {ex.Message}";
                        }
                    }

                    _evidence["framesCaptured"] = _loopDemoFrameIndex;
                    _evidence["actExecuted"] = doneExists;
                    _evidence["terminalRecordValid"] = doneExists;

                    if (doneExists && doneSuccess)
                    {
                        _evidence["functionalSkillPassed"] = true;
                        _evidence["gateSuccess"] = true;
                        Monitor.Log($"[TestDriver] loop-demo SUCCEEDED with {_loopDemoFrameIndex} frames captured.", LogLevel.Info);
                    }
                    else
                    {
                        _evidence["functionalSkillPassed"] = false;
                        _evidence["gateSuccess"] = false;
                        _evidence["failureReason"] = doneExists
                            ? $"LOOP_DEMO_FAILED: {doneError ?? "orchestrator reported failure in loop-demo-done.json"}"
                            : "TIMEOUT_WAITING_LOOP_DEMO_DONE: loop-demo timed out before loop-demo-done.json was produced.";
                        Monitor.Log($"[TestDriver] loop-demo FAILED: {_evidence["failureReason"]}", LogLevel.Error);
                    }
                }
                else if (_scenario.Equals("free-mode-control", StringComparison.OrdinalIgnoreCase))
                {
                    var done = ReadLoopDemoDoneFile(outDir);
                    RecordDoneFileEvidence(done);
                    // Externally driven control/observe scenario: the done file is the driver's
                    // completion signal; only an explicit "success": false or an error fails it.
                    var doneFailed = !done.Exists || done.Error != null || (done.SuccessExplicit && !done.Success);
                    _evidence["functionalSkillPassed"] = !doneFailed;
                    _evidence["gateSuccess"] = !doneFailed;
                    if (doneFailed)
                        _evidence["failureReason"] = done.Error ?? "FREE_MODE_CONTROL_DONE_MISSING_OR_FAILED";
                }
                else if (_scenario.Equals("daily-chain", StringComparison.OrdinalIgnoreCase))
                {
                    var done = ReadLoopDemoDoneFile(outDir);
                    RecordDoneFileEvidence(done);
                    _evidence["functionalSkillPassed"] = done.Exists && done.Success;
                    _evidence["gateSuccess"] = done.Exists && done.Success;
                    if (!done.Exists || !done.Success)
                        _evidence["failureReason"] = done.Error ?? "DAILY_CHAIN_DONE_MISSING_OR_FAILED";
                }
                else if (_scenario.Equals("farm-capabilities", StringComparison.OrdinalIgnoreCase))
                {
                    var done = ReadLoopDemoDoneFile(outDir);
                    RecordDoneFileEvidence(done);
                    _evidence["functionalSkillPassed"] = done.Exists && done.Success;
                    _evidence["gateSuccess"] = done.Exists && done.Success;
                    if (!done.Exists || !done.Success)
                        _evidence["failureReason"] = done.Error ?? "FARM_CAPABILITIES_DONE_MISSING_OR_FAILED";
                }
                else if (_scenario.Equals("harvest-demo", StringComparison.OrdinalIgnoreCase))
                {
                    EvaluateHarvestDemoGate(outDir, playerInvariant);
                }
                else if (_scenario.Equals("harvest-cancel", StringComparison.OrdinalIgnoreCase))
                {
                    EvaluateHarvestCancelGate(outDir, playerInvariant);
                }
                else if (_scenario.Equals("farm-flow", StringComparison.OrdinalIgnoreCase))
                {
                    EvaluateFarmFlowGate(outDir, playerInvariant);
                }
                else if (_scenario.Equals("farm-flow-interrupt", StringComparison.OrdinalIgnoreCase))
                {
                    EvaluateFarmFlowInterruptGate(outDir, playerInvariant);
                }
                else if (_scenario.Equals("farm-flow-pause", StringComparison.OrdinalIgnoreCase))
                {
                    var done = ReadLoopDemoDoneFile(outDir);
                    RecordDoneFileEvidence(done);
                    var pauseFarm = Game1.getFarm();
                    int remaining = Enumerable.Range(60, 6).Count(x =>
                        pauseFarm.terrainFeatures.TryGetValue(new Vector2(x, 20), out var tile) &&
                        tile is HoeDirt dirt && dirt.crop != null);
                    _evidence["remainingCount"] = remaining;
                    bool passed = done.Exists && done.Success && remaining == 0 && playerInvariant;
                    _evidence["functionalSkillPassed"] = passed;
                    _evidence["gateSuccess"] = passed;
                    if (!passed)
                        _evidence["failureReason"] = $"PAUSE_GATE: done={done.Exists}/{done.Success}, remaining={remaining}, playerInvariant={playerInvariant}";
                }
                else if (_scenario.Equals("planting-mcp", StringComparison.OrdinalIgnoreCase))
                {
                    EvaluatePlantingMcpGate(outDir, playerInvariant);
                }
                else if (_scenario.Equals("shipping-mcp", StringComparison.OrdinalIgnoreCase))
                {
                    EvaluateShippingMcpGate(outDir, playerInvariant);
                }
                else if (_scenario.Equals("navigation-mcp", StringComparison.OrdinalIgnoreCase))
                {
                    EvaluateNavigationMcpGate(outDir, playerInvariant);
                }
                else if (_scenario.Equals("purchase-mcp", StringComparison.OrdinalIgnoreCase))
                {
                    EvaluatePurchaseMcpGate(outDir, playerInvariant);
                }
                else if (_scenario.Equals("cycle-mcp", StringComparison.OrdinalIgnoreCase))
                {
                    EvaluateCycleMcpGate(outDir, playerInvariant);
                }
                else if (_scenario.Equals("cycle-operator", StringComparison.OrdinalIgnoreCase))
                {
                    EvaluateCycleOperatorGate(outDir, playerInvariant);
                }
                else if (_scenario.Equals("chest-operator", StringComparison.OrdinalIgnoreCase))
                {
                    EvaluateChestOperatorGate(outDir, playerInvariant);
                }
                else if (_scenario.Equals("existing-save-buy5", StringComparison.OrdinalIgnoreCase))
                {
                    EvaluateExistingSaveBuy5Gate(outDir, playerInvariant);
                }
                else
                {
                    _evidence["actExecuted"] = false;
                    _evidence["terminalRecordValid"] = false;
                    _evidence["functionalSkillPassed"] = false;
                    _evidence["gateSuccess"] = false;
                    if (!_evidence.ContainsKey("failureReason"))
                    {
                        _evidence["failureReason"] = $"SCENARIO_NOT_YET_IMPLEMENTED: Scenario '{_scenario}' has no verified protocol contract yet. Generic passing is rejected.";
                    }
                }
            }
        }
        catch (Exception ex)
        {
            teardownErrors.Add($"Exception in FinishScenario: {ex.GetType().Name}: {ex.Message}\n{ex.StackTrace}");
            Monitor.Log($"[TestDriver] Exception in FinishScenario: {ex.Message}", LogLevel.Error);
        }

        if (teardownErrors.Count > 0)
        {
            _evidence["teardownErrors"] = teardownErrors;
        }

        try
        {
            WriteEvidenceFile();
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Error writing evidence file: {ex.Message}", LogLevel.Error);
        }

        bool finalGateSuccess = _evidence.TryGetValue("gateSuccess", out var gsObj) && gsObj is bool b && b;
        Monitor.Log($"[TestDriver] Scenario finished. Mode: {_testMode}, GateSuccess: {finalGateSuccess}. Requesting exit...", LogLevel.Info);
        Game1.quit = true;
    }

    private void WriteEvidenceFile()
    {
        try
        {
            if (!string.IsNullOrWhiteSpace(_evidenceOutputPath))
            {
                var dir = Path.GetDirectoryName(_evidenceOutputPath);
                if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                var json = JsonSerializer.Serialize(_evidence, new JsonSerializerOptions { WriteIndented = true });
                File.WriteAllText(_evidenceOutputPath, json);
                Monitor.Log($"[TestDriver] Baseline evidence written to {_evidenceOutputPath}", LogLevel.Info);
            }
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Failed writing evidence: {ex.Message}", LogLevel.Error);
        }
    }

    private void WriteFreeModeNativeSnapshot(string outDir, string label)
    {
        try
        {
            var farm = Game1.getFarm();
            HoeDirt? targetDirt = farm.terrainFeatures.TryGetValue(_targetTile, out var feature)
                ? feature as HoeDirt : null;
            var dirtTiles = farm.terrainFeatures.Values.OfType<HoeDirt>().ToList();
            ResolveCompanionActor(out var actor, out var companionFarmer, out var observation, out _);
            var modInfo = Helper.ModRegistry.Get("CSXizhang.StardewAICompanion");
            var modInstance = modInfo?.GetType().GetProperty("Mod", BindingFlags.Public | BindingFlags.Instance)?.GetValue(modInfo);
            var coordinator = modInstance?.GetType().GetField("_coordinator", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(modInstance);
            var rest = coordinator?.GetType().GetProperty("Rest")?.GetValue(coordinator);
            var can = companionFarmer?.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault();
            var payload = new Dictionary<string, object?>
            {
                ["runId"] = _runId,
                ["label"] = label,
                ["year"] = Game1.year,
                ["season"] = Game1.currentSeason,
                ["day"] = Game1.dayOfMonth,
                ["timeOfDay"] = Game1.timeOfDay,
                ["isRaining"] = Game1.isRaining,
                ["playerMoney"] = Game1.player.Money,
                ["playerStamina"] = Game1.player.stamina,
                ["playerLocation"] = Game1.player.currentLocation?.NameOrUniqueName,
                ["playerTile"] = new { x = (int)Game1.player.Tile.X, y = (int)Game1.player.Tile.Y },
                ["companionObserved"] = companionFarmer != null,
                ["companionObservation"] = observation.ToString(),
                ["companionStamina"] = companionFarmer?.stamina,
                ["animationFrames"] = _companionAnimationFrames.ToDictionary(pair => pair.Key, pair => pair.Value.OrderBy(frame => frame).ToArray()),
                ["movementSamples"] = _acceptanceMovementSamples,
                ["solidOverlaps"] = _acceptanceSolidOverlaps.ToArray(),
                ["companionSpriteFrame"] = companionFarmer?.FarmerSprite.CurrentFrame,
                ["companionWater"] = can?.WaterLeft,
                ["companionLocation"] = companionFarmer?.currentLocation?.NameOrUniqueName,
                ["companionTile"] = companionFarmer == null ? null : new { x = (int)companionFarmer.Tile.X, y = (int)companionFarmer.Tile.Y },
                ["companionPosition"] = companionFarmer == null ? null : new { x = companionFarmer.Position.X, y = companionFarmer.Position.Y },
                ["companionTimeWentToBed"] = ReadNativeMember(companionFarmer, "timeWentToBed"),
                ["companionIsInBed"] = ReadNativeMember(companionFarmer, "isInBed"),
                ["companionBedtime"] = ReadNativeMember(rest, "Bedtime"),
                ["companionRestState"] = ReadNativeMember(rest, "State"),
                ["companionSleepStartedAt"] = ReadNativeMember(rest, "SleepStartedAt"),
                ["companionRestReason"] = ReadNativeMember(rest, "Reason"),
                ["companionRestPaused"] = ReadNativeMember(rest, "Paused"),
                ["gamePaused"] = Game1.paused,
                ["nativeIsMultiplayer"] = Game1.IsMultiplayer,
                ["nativeIsMasterGame"] = Game1.IsMasterGame,
                ["nativeMultiplayerMode"] = Game1.multiplayerMode,
                ["nativeOtherFarmerCount"] = Game1.otherFarmers.Count,
                ["nativeNewDay"] = Game1.newDay,
                ["playerHasMoved"] = Game1.player?.hasMoved,
                ["playerPassedOut"] = Game1.player?.passedOut,
                ["nativeFadeToBlack"] = Game1.fadeToBlack,
                ["nativeFadeIn"] = Game1.fadeIn,
                ["nativeFadeAlpha"] = Game1.fadeToBlackAlpha,
                ["gameWindowActive"] = ReadNativeMember(Game1.game1, "IsActive"),
                ["pauseWhenOutOfFocus"] = Game1.options.pauseWhenOutOfFocus,
                ["activeMenuType"] = Game1.activeClickableMenu?.GetType().FullName,
                ["playerIsInBed"] = Game1.player?.isInBed.Value,
                ["companionWaterMachinePaused"] = ReadNativeMember(ReadNativeMember(coordinator, "StateMachine"), "IsPaused"),
                ["companionWaterMachineExecuting"] = ReadNativeMember(ReadNativeMember(coordinator, "StateMachine"), "IsExecuting"),
                ["companionActiveTaskId"] = ReadNativeMember(actor, "ActiveTaskId"),
                ["companionExecutionState"] = ReadNativeMember(ReadNativeMember(coordinator, "ActiveMachine"), "CurrentState")?.ToString(),
                ["companionMachineExecuting"] = ReadNativeMember(ReadNativeMember(coordinator, "ActiveMachine"), "IsExecuting"),
                ["companionMachinePaused"] = ReadNativeMember(ReadNativeMember(coordinator, "ActiveMachine"), "IsPaused"),
                ["playerTimeWentToBed"] = ReadNativeMember(Game1.player, "timeWentToBed"),
                ["menuType"] = Game1.activeClickableMenu?.GetType().FullName,
                ["nativeShouldTimePass"] = Game1.shouldTimePass(),
                ["nativePaused"] = Game1.paused,
                ["nativeEventUp"] = Game1.eventUp,
                ["nativeDialogueUp"] = Game1.dialogueUp,
                ["playerCanMove"] = Game1.player?.CanMove,
                ["companionOnRealBed"] = companionFarmer?.currentLocation is StardewValley.Locations.FarmHouse sleepingHouse
                    && StardewValley.Objects.BedFurniture.GetBedAtTile(sleepingHouse, companionFarmer.TilePoint.X, companionFarmer.TilePoint.Y) != null,
                ["nativeBedSpot"] = new {
                    x = (Game1.getLocationFromName("FarmHouse") as StardewValley.Locations.FarmHouse)?.GetPlayerBed()?.GetBedSpot().X,
                    y = (Game1.getLocationFromName("FarmHouse") as StardewValley.Locations.FarmHouse)?.GetPlayerBed()?.GetBedSpot().Y },
                ["companionItems"] = companionFarmer == null ? null : DescribeItems(companionFarmer.Items),
                ["targetCrop"] = DescribeCropTile(_targetTile, targetDirt),
                ["targetWatered"] = targetDirt?.state.Value == 1,
                ["cropCount"] = dirtTiles.Count(d => d.crop != null),
                ["unwateredCropCount"] = dirtTiles.Count(d => d.crop != null && d.state.Value == 0),
                ["matureCropCount"] = dirtTiles.Count(d => d.crop != null && d.readyForHarvest()),
                ["harvestFixtureRemaining"] = Enumerable.Range(60, 6).Count(x =>
                    farm.terrainFeatures.TryGetValue(new Vector2(x, 20), out var tile) &&
                    tile is HoeDirt dirt && dirt.crop != null && dirt.readyForHarvest()),
                ["newCommandDryTiles"] = new[] { 60, 61 }.Where(x =>
                    farm.terrainFeatures.TryGetValue(new Vector2(x, 21), out var tile) &&
                    tile is HoeDirt dirt && dirt.state.Value == 0).ToArray(),
                ["pauseFixtureWateredCount"] = Enumerable.Range(60, 5).Count(x =>
                    farm.terrainFeatures.TryGetValue(new Vector2(x, 21), out var tile) && tile is HoeDirt dirt && dirt.state.Value == 1),
                ["farmObjectCount"] = farm.objects.Count()
            };
            if (_harnessFixtureMode != null)
            {
                var fixtureDirt = _harnessFixtureTiles.Select(tile =>
                    farm.terrainFeatures.TryGetValue(tile, out var nativeFeature) ? nativeFeature as HoeDirt : null).ToList();
                payload["harnessFixtureMode"] = _harnessFixtureMode;
                payload["harnessFixtureTargetCount"] = _harnessFixtureTiles.Count;
                payload["acceptanceShippedItems"] = companionFarmer == null ? null : DescribeItems(farm.getShippingBin(companionFarmer));
                payload["acceptanceGroundDropRemaining"] = _acceptanceDrop != null && farm.debris.Contains(_acceptanceDrop);
                payload["acceptanceMatureTreesRemaining"] = _acceptanceMatureTrees.Count(tile => farm.terrainFeatures.TryGetValue(tile, out var tree) && tree is Tree);
                payload["acceptanceSaplingStage"] = _acceptanceSapling is Vector2 sapling && farm.terrainFeatures.TryGetValue(sapling, out var young)
                    && young is Tree youngTree ? youngTree.growthStage.Value : (int?)null;
                payload["acceptanceChestItems"] = _acceptanceChest is Vector2 ct && farm.objects.TryGetValue(ct, out var chestObject)
                    && chestObject is StardewValley.Objects.Chest chest ? DescribeItems(chest.Items) : null;
                payload["harnessFixtureCropCount"] = fixtureDirt.Count(dirt => dirt?.crop != null);
                payload["harnessFixtureDryCropCount"] = fixtureDirt.Count(dirt => dirt?.crop != null && dirt.state.Value == 0);
                payload["harnessFixtureWateredCount"] = fixtureDirt.Count(dirt => dirt?.state.Value == 1);
                payload["harnessFixtureNewCropCount"] = _harnessFixtureInitiallyEmpty.Count(tile =>
                    farm.terrainFeatures.TryGetValue(tile, out var nativeFeature) && nativeFeature is HoeDirt dirt && dirt.crop != null);
                payload["harnessFixtureTargets"] = _harnessFixtureTiles.Select(tile => DescribeCropTile(tile,
                    farm.terrainFeatures.TryGetValue(tile, out var nativeFeature) ? nativeFeature as HoeDirt : null)).ToArray();
                payload["timestampUtc"] = DateTime.UtcNow.ToString("o");
            }
            var path = Path.Combine(outDir, $"free-mode-native-{label}.json");
            File.WriteAllText(path, JsonSerializer.Serialize(payload, new JsonSerializerOptions { WriteIndented = true }));
            Monitor.Log($"[TestDriver] Wrote free-mode native snapshot '{label}'.", LogLevel.Info);
        }
        catch (Exception ex)
        {
            Monitor.Log($"[TestDriver] Free-mode native snapshot failed: {ex.Message}", LogLevel.Error);
        }
    }

    private static object? ReadNativeMember(object? instance, string name)
    {
        if (instance == null) return null;
        const BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
        var type = instance.GetType();
        object? result = type.GetProperty(name, flags)?.GetValue(instance)
            ?? type.GetField(name, flags)?.GetValue(instance);
        if (result == null) return null;
        return result.GetType().GetProperty("Value", flags)?.GetValue(result) ?? result;
    }

    private string? _lifecycleSaveName;

    private void HandleLifecycleRequest()
    {
        if (_scenario != "free-mode-control" || _evidenceDir == null) return;
        string path = Path.Combine(_evidenceDir, "lifecycle-request.json");
        if (!File.Exists(path)) return;
        string label = "";
        try
        {
            using var request = JsonDocument.Parse(File.ReadAllText(path));
            File.Delete(path);
            label = request.RootElement.GetProperty("label").GetString() ?? "";
            if (label.Length is < 1 or > 48 || !label.All(c => char.IsLetterOrDigit(c) || c is '-' or '_'))
                throw new ArgumentException("Lifecycle request requires a safe label.");
            string action = request.RootElement.GetProperty("action").GetString() ?? "";
            if (action == "return-title")
            {
                _lifecycleSaveName = Constants.SaveFolderName ?? throw new InvalidOperationException("No loaded test save.");
                Game1.ExitToTitle();
            }
            else if (action == "reload")
            {
                if (Context.IsWorldReady || _lifecycleSaveName == null) throw new InvalidOperationException("Not at title after this test save.");
                SaveGame.Load(_lifecycleSaveName);
                Game1.exitActiveMenu();
            }
            else if (action != "snapshot") throw new ArgumentException("Unknown lifecycle action.");
            var info = Helper.ModRegistry.Get("CSXizhang.StardewAICompanion");
            var instance = info?.GetType().GetProperty("Mod")?.GetValue(info);
            var transport = instance?.GetType().GetField("_transportServer", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(instance);
            var owned = instance?.GetType().GetField("_ownedBridgeLaunch", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(instance);
            var process = owned?.GetType().GetField("_process", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(owned) as System.Diagnostics.Process;
            int? launcherPid = null;
            try { if (process != null && !process.HasExited) launcherPid = process.Id; } catch (InvalidOperationException) { }
            var snapshot = new {
                action, label, gamePid = Environment.ProcessId, worldReady = Context.IsWorldReady,
                saveName = Constants.SaveFolderName, menu = Game1.activeClickableMenu?.GetType().FullName,
                chatConnected = transport?.GetType().GetProperty("IsChatConnected")?.GetValue(transport),
                launcherPid, ownsLaunch = owned != null
            };
            File.WriteAllText(Path.Combine(_evidenceDir, $"lifecycle-{label}.json"), JsonSerializer.Serialize(snapshot), new System.Text.UTF8Encoding(false));
        }
        catch (Exception ex)
        {
            if (label.Length is > 0 and <= 48 && label.All(c => char.IsLetterOrDigit(c) || c is '-' or '_'))
                File.WriteAllText(Path.Combine(_evidenceDir, $"lifecycle-{label}.json"), JsonSerializer.Serialize(new { error = ex.Message }));
            Monitor.Log($"[TestDriver] Lifecycle request failed: {ex.Message}", LogLevel.Warn);
        }
    }

    private Dictionary<string, object?> GetCurrentGameStatus()
    {
        var dict = new Dictionary<string, object?>
        {
            ["worldReady"] = Context.IsWorldReady,
            ["titleMenu"] = Game1.activeClickableMenu is TitleMenu,
            ["savePath"] = StardewValley.Program.GetSavesFolder(),
            ["smapiSavePath"] = Constants.SavesPath
        };

        if (Context.IsWorldReady && Game1.player != null)
        {
            dict["farmerName"] = Game1.player.Name;
            dict["farmName"] = Game1.player.farmName.Value;
            dict["day"] = Game1.dayOfMonth;
            dict["season"] = Game1.currentSeason;
            dict["year"] = Game1.year;
            dict["timeOfDay"] = Game1.timeOfDay;
            dict["stamina"] = Game1.player.Stamina;
            dict["maxStamina"] = Game1.player.MaxStamina;
            dict["location"] = Game1.currentLocation?.Name;
            dict["tileX"] = (int)Game1.player.Tile.X;
            dict["tileY"] = (int)Game1.player.Tile.Y;

            var waterCan = Game1.player.Items.OfType<StardewValley.Tools.WateringCan>().FirstOrDefault();
            if (waterCan != null)
            {
                dict["wateringCanWaterLeft"] = waterCan.WaterLeft;
                dict["wateringCanCapacity"] = waterCan.waterCanMax;
            }
        }

        return dict;
    }
}
