using System;
using System.Buffers.Binary;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Objects;
using StardewValley.TerrainFeatures;

namespace StardewAI.Companion.TestDriver;

public sealed partial class TestDriverMod
{
    private const int CreativeTimeOfDay = 1000;
    private const int CreativeOutputSlots = 12;
    private static readonly string[] CreativeMaterialIds = {
        "(O)388", "(O)390", "(O)771", "(O)709", "(O)382",
        "(O)378", "(O)334", "(O)335", "(O)338", "(O)336",
        "(O)330", "(O)725", "(O)724", "(O)86", "(O)726",
        // Flower seeds are crafting ingredients, not instant mature crops.
        // Omit unused sap/hay and iridium-sprinkler ingredients to retain 12 output slots.
        "(O)427", "(O)429", "(O)453", "(O)455"
    };
    private static readonly (string Name, string[] Categories)[] CreativeRecipeCandidates = {
        ("Wood Path", new[] { "path" }), ("Gravel Path", new[] { "path" }),
        ("Cobblestone Path", new[] { "path" }), ("Stepping Stone Path", new[] { "path" }),
        ("Wood Floor", new[] { "path" }), ("Stone Floor", new[] { "path" }),
        ("Stone Walkway Floor", new[] { "path" }), ("Rustic Plank Floor", new[] { "path" }),
        ("Wood Fence", new[] { "decoration" }), ("Stone Fence", new[] { "decoration" }),
        ("Hardwood Fence", new[] { "decoration" }), ("Gate", new[] { "decoration" }),
        ("Chest", new[] { "chest" }), ("Torch", new[] { "light" }),
        ("Campfire", new[] { "rest", "light" }),
        ("Wooden Brazier", new[] { "rest", "light" }),
        ("Stone Brazier", new[] { "rest", "light" }),
        ("Barrel Brazier", new[] { "rest", "light" }),
        ("Stump Brazier", new[] { "rest", "light" }),
        ("Furnace", new[] { "production" }),
        ("Charcoal Kiln", new[] { "production" }),
        ("Recycling Machine", new[] { "production" }),
        ("Weathered Floor", new[] { "path" }), ("Crystal Floor", new[] { "path" }),
        ("Brick Floor", new[] { "path" }), ("Straw Floor", new[] { "path" }),
        ("Crystal Path", new[] { "path" }),
        ("Wood Lamp-post", new[] { "light" }), ("Iron Lamp-post", new[] { "light" }),
        ("Wood Sign", new[] { "decoration" }), ("Stone Sign", new[] { "decoration" }),
        ("Scarecrow", new[] { "decoration", "farming" }), ("Deluxe Scarecrow", new[] { "decoration", "farming" }),
        ("Sprinkler", new[] { "farming" }), ("Quality Sprinkler", new[] { "farming" }),
        ("Iridium Sprinkler", new[] { "farming" }),
        ("Keg", new[] { "production" }), ("Preserves Jar", new[] { "production" }),
        ("Bee House", new[] { "production" }), ("Mayonnaise Machine", new[] { "production" }),
        ("Cheese Press", new[] { "production" }), ("Loom", new[] { "production" }),
        ("Seed Maker", new[] { "production" }),
        ("Grass Starter", new[] { "vegetation" }),
        ("Tub o' Flowers", new[] { "flower", "decoration" })
    };

    private sealed record CreativeLayoutItem(string kind, string itemId, string name,
        int x, int y, int width, int height, string origin, string[] categories);
    private sealed record CreativeEntrance(string Id, int X, int Y, string Target, bool InitiallyReachable = false);

    private bool _creativeFarmActive;
    private string? _creativeFarmError;
    private object? _creativeModEntry;
    private Farmer? _creativeFarmer;
    private string? _creativeSaveName;
    private int _creativeDay, _creativeSequence, _creativeSupplyEvents;
    private int _creativeMaintenanceTicks;
    private string? _creativeControlSignal;
    private Point _creativeAnchor;
    private Rectangle _creativeWorkArea;
    private string _creativeWaterBaseline = "";
    private readonly Dictionary<string, int> _creativeMaterialTargets = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string[]> _creativeItemCategories = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _creativeItemNames = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _creativeBuildings = new(StringComparer.Ordinal);
    private readonly HashSet<string> _creativeBaselineLayout = new(StringComparer.Ordinal);
    private readonly HashSet<string> _creativeCommands = new(StringComparer.Ordinal);
    private readonly List<CreativeEntrance> _creativeEntrances = new();

    private void CreativeEvent(string kind, object payload)
    {
        File.AppendAllText(Path.Combine(_evidenceDir!, "creative-farm-ledger.jsonl"), JsonSerializer.Serialize(new {
            seq = ++_creativeSequence, at = DateTime.UtcNow.ToString("o"), type = kind,
            runId = _runId, saveName = Constants.SaveFolderName,
            year = Game1.year, season = Game1.currentSeason, day = Game1.dayOfMonth,
            timeOfDay = Game1.timeOfDay, payload
        }) + Environment.NewLine);
    }

    private void ArrangeCreativeFarmFixture(object modEntry, string outDir, string label)
    {
        AssertHarnessFixtureBoundary(modEntry);
        ResolveCompanionActor(out _, out var farmer, out _, out _);
        if (_creativeFarmer != null || farmer == null || !ReferenceEquals(farmer, Game1.player)
            || farmer.currentLocation is not Farm farm || Game1.whichFarm != 0
            || Game1.IsMultiplayer || !Game1.IsMasterGame)
            throw new InvalidOperationException("Creative farm requires a standard-farm, single-player isolated clone with the native player actor, once only.");
        if (Game1.currentSeason is not ("spring" or "summer"))
            throw new InvalidOperationException("Creative farm needs an original spring/summer save so native flower tubs are visibly in season; the fixture never changes the season.");
        var tools = farmer.Items.OfType<Tool>().ToList();
        if (!tools.OfType<StardewValley.Tools.Axe>().Any()) tools.Add(new StardewValley.Tools.Axe());
        if (!tools.OfType<StardewValley.Tools.Pickaxe>().Any()) tools.Add(new StardewValley.Tools.Pickaxe());
        if (!tools.OfType<StardewValley.Tools.Hoe>().Any()) tools.Add(new StardewValley.Tools.Hoe());
        if (!tools.OfType<StardewValley.Tools.MeleeWeapon>().Any(tool => tool.isScythe())) tools.Add(new StardewValley.Tools.MeleeWeapon("47"));
        int requiredSlots = farmer.Items.Count(item => item != null) + tools.Count(tool => !farmer.Items.Contains(tool))
            + CreativeMaterialIds.Count(id => !farmer.Items.Any(item => item?.QualifiedItemId == id));
        if (requiredSlots > 36 - CreativeOutputSlots)
            throw new InvalidOperationException("Creative fixture cannot reserve twelve output slots alongside the existing tools and material whitelist.");

        // Validate the real recipes and native placement support before changing the clone.
        // Preview factories do not insert their outputs into any inventory or world.
        var materials = CreativeMaterialIds.Select(id => ItemRegistry.Create(id)).ToArray();
        var recipes = new List<object>();
        var acceptedNames = new List<string>();
        var rejected = new List<object>();
        foreach (var candidate in CreativeRecipeCandidates)
        {
            if (!CraftingRecipe.craftingRecipes.ContainsKey(candidate.Name))
            { rejected.Add(new { name = candidate.Name, reason = "native-recipe-absent" }); continue; }
            var recipe = new CraftingRecipe(candidate.Name, false);
            var output = recipe.createItem();
            if (output is not StardewValley.Object item || item is StorageFurniture || item.heldObject.Value is not null
                || !(item is Furniture || item.bigCraftable.Value || item.IsFloorPathItem() || item.IsFenceItem() || item.IsSprinkler()
                    || item.QualifiedItemId == "(O)297"))
            { rejected.Add(new { name = candidate.Name, reason = "not-supported-by-current-place-items" }); continue; }
            if (CreativeMaterialIds.Contains(output.QualifiedItemId, StringComparer.Ordinal)
                || recipe.recipeList.Count == 0 || recipe.recipeList.Any(ingredient => ingredient.Value <= 0
                    || !materials.Any(material => CraftingRecipe.ItemMatchesForCrafting(material, ingredient.Key))))
            { rejected.Add(new { name = candidate.Name, reason = "outside-basic-material-whitelist" }); continue; }
            acceptedNames.Add(candidate.Name);
            _creativeItemCategories[output.QualifiedItemId] = candidate.Categories;
            _creativeItemNames[output.QualifiedItemId] = output.DisplayName;
            recipes.Add(new { name = candidate.Name, displayName = recipe.DisplayName,
                itemId = output.QualifiedItemId, outputName = output.DisplayName,
                countPerCraft = recipe.numberProducedPerCraft, categories = candidate.Categories,
                ingredients = recipe.recipeList.Select(p => new { itemId = p.Key, count = p.Value }).ToArray() });
        }
        if (new[] { "path", "chest", "rest", "light" }.Any(category =>
                !_creativeItemCategories.Values.Any(categories => categories.Contains(category))))
            throw new InvalidOperationException("Native recipe/placement preflight did not provide every required creative category: path, chest, rest, light.");
        if (!acceptedNames.Contains("Grass Starter") || !acceptedNames.Contains("Tub o' Flowers"))
            throw new InvalidOperationException("Native grass starter and flower tub recipes/materials must both pass preflight.");

        _creativeModEntry = modEntry;
        _creativeFarmer = farmer;
        _creativeSaveName = Constants.SaveFolderName;
        _creativeDay = Game1.Date.TotalDays;
        _creativeAnchor = farmer.TilePoint;
        var map = farm.Map.Layers[0];
        _creativeWorkArea = new Rectangle(0, 0, map.LayerWidth, map.LayerHeight);
        _creativeWaterBaseline = CreativeWaterFingerprint(farm);
        foreach (var pair in CreativeBuildingSignatures(farm)) _creativeBuildings.Add(pair.Key, pair.Value);
        var initialSpace = CreativeSpace();
        var initialInventory = DescribeItems(farmer.Items);
        var originalRecipes = farmer.craftingRecipes.Keys.ToArray();

        // Retain the exact source ground. Only the model's native actions may clear it.
        var originalGround = new { objects = farm.objects.Count(), terrain = farm.terrainFeatures.Count(),
            furniture = farm.furniture.Count, largeTerrain = farm.largeTerrainFeatures.Count,
            resourceClumps = farm.resourceClumps.Count, debris = farm.debris.Count };
        _harnessFixtureTiles.Clear();
        _harnessFixtureInitiallyEmpty.Clear();
        _acceptanceChest = null;
        _acceptanceDrop = null;
        _acceptanceMatureTrees.Clear();
        _acceptanceSapling = null;

        farmer.MaxItems = 36; // One-time clone setup; never overwrite capacity every tick.
        foreach (var tool in tools)
        {
            if (!farmer.Items.Contains(tool)) farmer.addItemToInventory(tool);
            if (tool is StardewValley.Tools.Axe or StardewValley.Tools.Pickaxe or StardewValley.Tools.Hoe or StardewValley.Tools.WateringCan)
                tool.UpgradeLevel = 4;
        }
        while (farmer.Items.Count < farmer.MaxItems) farmer.Items.Add(null);
        farmer.craftingRecipes.Clear();
        foreach (string name in acceptedNames) farmer.craftingRecipes.Add(name, 0);
        foreach (var material in materials)
            _creativeMaterialTargets.Add(material.QualifiedItemId, Math.Min(999, material.maximumStackSize()));
        farmer.maxStamina.Value = Math.Max(farmer.MaxStamina, 1000);
        Game1.timeOfDay = CreativeTimeOfDay;
        Game1.gameTimeInterval = 0;
        _freezeTime = true;
        _harnessFixtureMode = "creative-farm";
        RefillCreativeMaterials("initial");

        foreach (var row in CreativeLayout(farm)) _creativeBaselineLayout.Add(CreativeLayoutKey(row));
        DiscoverCreativeEntrances(farm);
        var coordinator = ReadNativeMember(modEntry, "_coordinator")!;
        // Capture real construction receipts at completion, before a following
        // observation can overwrite LastSkillResultPayload in the same tick.
        coordinator.GetType().GetEvent("OnExecutionCompleted")!.AddEventHandler(coordinator,
            (Action)(() => {
                if (!_creativeFarmActive) return;
                try { RecordCreativeNativeResult(); }
                catch (Exception error) { StopCreativeFarm(error); }
            }));
        _creativeFarmActive = true;
        PublishCreativeControl();
        PublishCreativeSnapshot();
        var initial = ObserveCreativeFarm();
        var fixture = new {
            runId = _runId, label, mode = "creative-farm", actorMode = "player", actorId = farmer.UniqueMultiplayerID,
            saveName = _creativeSaveName, isolatedSavesPath = StardewValley.Program.GetSavesFolder(),
            locationId = "Farm", workArea = new { x = 0, y = 0, width = map.LayerWidth, height = map.LayerHeight },
            description = "未经预先清场的隔离标准农场，完整保留原始树草、杂物和大型障碍；先观察并记录整体设计及保留区域，再自行用原生工具清理施工区域。基础材料在空闲边界补足，已有物品不被覆盖，成品须亲手制作摆放；自行拾取、入箱并管理背包。",
            materialPolicy = new { mode = "refill-basic-materials-at-native-idle-boundaries", inventoryCapacity = 36,
                reservedInitialOutputSlots = CreativeOutputSlots, materials = materials.Select(item => new {
                    itemId = item.QualifiedItemId, name = item.DisplayName, target = _creativeMaterialTargets[item.QualifiedItemId] }).ToArray(),
                finishedItemsGranted = false, chestMaterialsConsumedAutomatically = false,
                tools = tools.Select(tool => new { name = tool.GetType().Name, upgradeLevel = tool.UpgradeLevel }).ToArray() },
            environment = new { fixedTimeOfDay = CreativeTimeOfDay, clockFrozen = true,
                stamina = "restored-to-fixture-maximum-at-idle-boundaries", maxStamina = farmer.MaxStamina,
                scope = "creative-farm-isolated-test-only" },
            recipeWhitelist = recipes, rejectedRecipes = rejected, originalRecipes, originalInventory = initialInventory,
            groundPrecleared = false, originalGround, initialSpace, initialLayout = CreativeLayout(farm),
            protectedBuildings = _creativeBuildings.Select(pair => JsonSerializer.Deserialize<JsonElement>(pair.Value)).ToArray(),
            accessAnchor = new { x = _creativeAnchor.X, y = _creativeAnchor.Y },
            entrances = _creativeEntrances.Select(entrance => new { id = entrance.Id,
                tile = new { x = entrance.X, y = entrance.Y }, target = entrance.Target,
                initiallyReachable = entrance.InitiallyReachable }).ToArray(),
            initial
        };
        Directory.CreateDirectory(outDir);
        File.WriteAllText(Path.Combine(outDir, $"harness-fixture-{label}.json"), JsonSerializer.Serialize(fixture, new JsonSerializerOptions { WriteIndented = true }));
        File.WriteAllText(Path.Combine(outDir, "creative-farm-initial-layout.json"), JsonSerializer.Serialize(initial));
        _evidence["harnessFixture"] = fixture;
        CreativeEvent("initial", initial);
        WriteFreeModeNativeSnapshot(outDir, label);
        Monitor.Log($"[TestDriver] Creative farm ready: {recipes.Count} native recipes, materials only, fixed daylight, twelve output slots reserved.", LogLevel.Info);
    }

    private void MaintainCreativeFarm()
    {
        try
        {
            if (_creativeModEntry == null || _creativeFarmer == null || !ReferenceEquals(_creativeFarmer, Game1.player)
                || Constants.SaveFolderName != _creativeSaveName || Game1.Date.TotalDays != _creativeDay)
                throw new InvalidOperationException("Creative fixture actor/save/day changed; automatic supply stopped.");
            var coordinator = ReadNativeMember(_creativeModEntry, "_coordinator");
            PublishCreativeControl();
            RecordCreativeNativeResult();
            // The scenario counter also advances inside WaitingForExternalAct;
            // its parity is unsuitable for scheduling maintenance on update ticks.
            if (++_creativeMaintenanceTicks % 6 != 0 || ReadNativeMember(coordinator, "ActiveMachine") != null
                || ReadNativeMember(ReadNativeMember(_creativeModEntry, "_actor"), "ActiveTaskId") != null
                || Game1.activeClickableMenu != null || Game1.eventUp || Game1.newDay || Game1.fadeToBlack
                || Game1.fadeIn || _creativeFarmer.UsingTool || _creativeFarmer.isEating || _creativeFarmer.isInBed.Value)
                return;
            AssertHarnessFixtureBoundary(_creativeModEntry);
            if (RefillCreativeMaterials("native-idle")) PublishCreativeSnapshot();
        }
        catch (Exception error)
        {
            StopCreativeFarm(error);
        }
    }

    private void RecordCreativeNativeResult()
    {
        var result = ReadNativeMember(ReadNativeMember(_creativeModEntry, "_coordinator"), "LastSkillResultPayload");
        if (ReadNativeMember(result, "CommandId") is string command && _creativeCommands.Add(command))
            CreativeEvent("native-result", result!);
    }

    private void StopCreativeFarm(Exception error)
    {
        _creativeFarmError = error.GetBaseException().Message;
        _creativeFarmActive = false;
        _freezeTime = false;
        PublishCreativeControl();
        Monitor.Log($"[TestDriver] Creative supply stopped: {_creativeFarmError}", LogLevel.Error);
    }

    private bool RefillCreativeMaterials(string reason)
    {
        var farmer = _creativeFarmer!;
        var supplied = new List<object>();
        foreach (var pair in _creativeMaterialTargets)
        {
            int before = farmer.Items.Where(item => item?.QualifiedItemId == pair.Key).Sum(item => item.Stack);
            if (before >= pair.Value) continue;
            // Never remove products or replace occupied slots to make supply fit.
            farmer.addItemToInventory(ItemRegistry.Create(pair.Key, pair.Value - before));
            int after = farmer.Items.Where(item => item?.QualifiedItemId == pair.Key).Sum(item => item.Stack);
            if (after > before) supplied.Add(new { itemId = pair.Key, before, added = after - before, after, target = pair.Value });
        }
        float staminaBefore = farmer.Stamina;
        farmer.Stamina = farmer.MaxStamina;
        if (supplied.Count > 0 || staminaBefore != farmer.Stamina)
        {
            _creativeSupplyEvents++;
            CreativeEvent("material-supply", new { reason, items = supplied, staminaBefore, staminaAfter = farmer.Stamina,
                source = "isolated-test-fixture", finishedItemsGranted = false });
            return true;
        }
        return false;
    }

    private void PublishCreativeControl()
    {
        string signal = JsonSerializer.Serialize(new { runId = _runId, active = _creativeFarmActive,
            error = _creativeFarmError, agentOwns = ReadNativeMember(ReadNativeMember(_creativeModEntry, "_authority"), "AgentOwns") });
        if (signal == _creativeControlSignal) return;
        string path = Path.Combine(_evidenceDir!, "creative-farm-control.json");
        File.WriteAllText(path + ".tmp", signal);
        File.Move(path + ".tmp", path, overwrite: true);
        _creativeControlSignal = signal;
    }

    private void PublishCreativeSnapshot()
    {
        var observer = ReadNativeMember(_creativeModEntry, "_observer")!;
        observer.GetType().GetMethod("BumpRevision")!.Invoke(observer, null);
        var revision = ReadNativeMember(observer, "WorldRevision");
        var coordinator = ReadNativeMember(_creativeModEntry, "_coordinator");
        var snapshot = coordinator?.GetType().GetMethod("CaptureCurrentSnapshot")?.Invoke(coordinator, new object?[] { revision });
        var transport = ReadNativeMember(_creativeModEntry, "_transportServer");
        if (snapshot != null) transport?.GetType().GetMethod("SendSnapshotAsync")?.Invoke(transport, new object?[] { snapshot, revision });
    }

    private static Dictionary<string, string> CreativeBuildingSignatures(Farm farm) => farm.buildings.ToDictionary(
        building => building.id.Value.ToString(), building => JsonSerializer.Serialize(new {
            id = building.id.Value.ToString(), type = building.buildingType.Value,
            x = building.tileX.Value, y = building.tileY.Value,
            width = building.tilesWide.Value, height = building.tilesHigh.Value
        }), StringComparer.Ordinal);

    private static string CreativeWaterFingerprint(Farm farm)
    {
        var water = new StringBuilder();
        var layer = farm.Map.Layers[0];
        for (int y = 0; y < layer.LayerHeight; y++)
            for (int x = 0; x < layer.LayerWidth; x++)
                if (farm.isWaterTile(x, y)) water.Append(x).Append(',').Append(y).Append(';');
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(water.ToString())));
    }

    private static string CreativeLayoutKey(CreativeLayoutItem item) => $"{item.kind}:{item.x}:{item.y}:{item.itemId}";

    private CreativeLayoutItem[] CreativeLayout(Farm farm)
    {
        var result = new List<CreativeLayoutItem>();
        void Add(string kind, string id, string name, int x, int y, int width = 1, int height = 1)
        {
            var row = new CreativeLayoutItem(kind, id, name, x, y, width, height, "agent-or-native",
                _creativeItemCategories.TryGetValue(id, out var categories) ? categories : Array.Empty<string>());
            result.Add(_creativeBaselineLayout.Contains(CreativeLayoutKey(row)) ? row with { origin = "baseline" } : row);
        }
        foreach (var pair in farm.objects.Pairs)
            Add(pair.Value is Chest ? "chest" : "object", pair.Value.QualifiedItemId, pair.Value.DisplayName, (int)pair.Key.X, (int)pair.Key.Y);
        foreach (var pair in farm.terrainFeatures.Pairs)
        {
            if (pair.Value is not Flooring floor) continue;
            string id = floor.GetData()?.ItemId is string raw ? ItemRegistry.QualifyItemId(raw) : "unknown";
            Add("flooring", id, _creativeItemNames.GetValueOrDefault(id, id), (int)pair.Key.X, (int)pair.Key.Y);
        }
        foreach (var furniture in farm.furniture)
        {
            var box = furniture.GetBoundingBox();
            Add("furniture", furniture.QualifiedItemId, furniture.DisplayName, box.X / 64, box.Y / 64,
                Math.Max(1, box.Width / 64), Math.Max(1, box.Height / 64));
        }
        return result.OrderBy(item => item.y).ThenBy(item => item.x).ThenBy(item => item.kind, StringComparer.Ordinal).ToArray();
    }

    private void DiscoverCreativeEntrances(Farm farm)
    {
        var candidates = new Dictionary<string, CreativeEntrance>(StringComparer.Ordinal);
        void Add(int x, int y, string target) => candidates.TryAdd($"{x},{y}", new($"entrance-{x}-{y}", x, y, target));
        foreach (var warp in farm.warps.Where(warp => !warp.npcOnly.Value)) Add(warp.X, warp.Y, warp.TargetName);
        foreach (var door in farm.doors.Pairs) Add(door.Key.X, door.Key.Y, door.Value);
        foreach (var building in farm.buildings)
        {
            if (building.daysOfConstructionLeft.Value > 0) continue;
            var door = building.getPointForHumanDoor();
            if (door.X >= 0 && door.Y >= 0) Add(door.X, door.Y, building.GetIndoorsName() ?? building.buildingType.Value);
        }
        foreach (var entrance in candidates.Values)
            _creativeEntrances.Add(entrance with { InitiallyReachable = CreativeEntranceRoute(entrance).Reachable });
        if (!_creativeEntrances.Any(entrance => entrance.InitiallyReachable))
            throw new InvalidOperationException("Creative fixture found no entrance reachable by the production navigator from the initial player position.");
    }

    private (bool Reachable, object? Standing, string? Error) CreativeEntranceRoute(CreativeEntrance entrance, bool allowTargetObject = false)
    {
        var farm = Game1.getFarm();
        var entranceTile = new Vector2(entrance.X, entrance.Y);
        var entranceBounds = new Rectangle(entrance.X * 64, entrance.Y * 64, 64, 64);
        if (!allowTargetObject && (farm.objects.TryGetValue(entranceTile, out var obstacle) && !obstacle.isPassable()
            || farm.furniture.Any(item => !item.isPassable() && item.GetBoundingBox().Intersects(entranceBounds))))
            return (false, null, "entrance-blocked-by-placed-item");
        var observer = ReadNativeMember(_creativeModEntry, "_observer")!;
        var navigator = ReadNativeMember(_creativeModEntry, "_navigator")!;
        var assembly = _creativeModEntry!.GetType().Assembly;
        var tileType = assembly.GetType("StardewAI.Companion.Mod.Domain.TileCoordinate")!;
        var poseType = assembly.GetType("StardewAI.Companion.Mod.Domain.AuthoritativePose")!;
        var facingType = assembly.GetType("StardewAI.Companion.Mod.Domain.FacingDirection")!;
        object Tile(int x, int y) => Activator.CreateInstance(tileType, x, y)!;
        var anchor = Tile(_creativeAnchor.X, _creativeAnchor.Y);
        if (observer.GetType().GetMethod("IsTilePassable")!.Invoke(observer, new[] { (object)"Farm", anchor }) is not true)
            return (false, null, "initial-access-anchor-blocked");
        var pose = Activator.CreateInstance(poseType, "Farm", anchor, Enum.ToObject(facingType, 2))!;
        var method = navigator.GetType().GetMethods().Single(candidate => candidate.Name == "FindPathToInteract"
            && candidate.GetParameters()[0].ParameterType == poseType);
        var path = method.Invoke(navigator, new object?[] { pose, "Farm", Tile(entrance.X, entrance.Y), null });
        var steps = (ReadNativeMember(path, "Steps") as IEnumerable)?.Cast<object>().ToArray();
        var tile = ReadNativeMember(steps?.LastOrDefault(), "Tile");
        return (ReadNativeMember(path, "Success") is true,
            tile == null ? null : new { x = ReadNativeMember(tile, "X"), y = ReadNativeMember(tile, "Y") },
            ReadNativeMember(path, "ErrorMessage")?.ToString());
    }

    private object ObserveCreativeFarm()
    {
        var farm = Game1.getFarm();
        var layout = CreativeLayout(farm);
        var added = layout.Where(item => item.origin != "baseline").ToArray();
        int Count(string category) => added.Count(item => item.categories.Contains(category));
        var currentBuildings = CreativeBuildingSignatures(farm);
        var changed = _creativeBuildings.Where(pair => !currentBuildings.TryGetValue(pair.Key, out var value)
            || value != pair.Value).Select(pair => pair.Key).ToArray();
        var entrances = _creativeEntrances.Select(entrance => {
            var route = CreativeEntranceRoute(entrance);
            return new { id = entrance.Id, tile = new { x = entrance.X, y = entrance.Y },
                target = entrance.Target, initiallyReachable = entrance.InitiallyReachable,
                reachable = route.Reachable, standingTile = route.Standing, error = route.Error };
        }).ToArray();
        var farmer = _creativeFarmer;
        var operationAccess = added.Where(item => item.categories.Contains("chest") || item.categories.Contains("production"))
            .Select(item => {
                var route = CreativeEntranceRoute(new("placed-item", item.x, item.y, item.name), allowTargetObject: true);
                return new { item.itemId, item.x, item.y, reachable = route.Reachable, standingTile = route.Standing, error = route.Error };
            }).ToArray();
        return new {
            fixtureId = _runId, active = _creativeFarmActive, error = _creativeFarmError, actorMode = "player",
            actorId = farmer?.UniqueMultiplayerID, actorLocation = farmer?.currentLocation?.NameOrUniqueName,
            agentOwns = ReadNativeMember(ReadNativeMember(_creativeModEntry, "_authority"), "AgentOwns"),
            inventory = farmer == null ? null : DescribeItems(farmer.Items),
            freeSlots = farmer == null ? (int?)null : farmer.Items.Take(farmer.MaxItems).Count(item => item == null),
            inventoryCapacity = farmer?.MaxItems, reservedInitialOutputSlots = CreativeOutputSlots,
            workArea = new { x = _creativeWorkArea.X, y = _creativeWorkArea.Y, width = _creativeWorkArea.Width, height = _creativeWorkArea.Height },
            counts = new { pathTiles = Count("path"), chests = Count("chest"), restObjects = Count("rest"), lights = Count("light") },
            layout, space = CreativeSpace(), operationAccess, buildingsPreserved = changed.Length == 0, changedOriginalBuildings = changed,
            extraBuildings = currentBuildings.Keys.Except(_creativeBuildings.Keys).ToArray(),
            waterTilesPreserved = CreativeWaterFingerprint(farm) == _creativeWaterBaseline,
            accessAnchor = new { x = _creativeAnchor.X, y = _creativeAnchor.Y }, entrances,
            entrancesReachable = entrances.Any(entrance => entrance.initiallyReachable)
                && entrances.Where(entrance => entrance.initiallyReachable).All(entrance => entrance.reachable),
            supplyLedgerSequence = _creativeSequence, supplyEvents = _creativeSupplyEvents,
            nativeResultCount = _creativeCommands.Count, harnessTimeFrozen = _freezeTime,
            fixedTimeOfDay = CreativeTimeOfDay, timeOfDay = Game1.timeOfDay,
            resourcePolicy = "Only whitelisted materials and stamina are replenished by the test fixture; crafting and placement use production native actions.",
            recipeCraftCounts = farmer?.craftingRecipes.Keys.ToDictionary(name => name, name => farmer.craftingRecipes[name])
        };
    }

    private object CreativeSpace()
    {
        var observer = ReadNativeMember(_creativeModEntry, "_observer")!;
        return observer.GetType().GetMethod("InspectLocation")!.Invoke(observer, new object?[] { "Farm", null })!;
    }

    private void CaptureCreativeFarm(object modEntry, string outDir, string label, Rectangle? region = null)
    {
        if (_harnessFixtureMode != "creative-farm" || _creativeModEntry == null || !Context.IsWorldReady
            || Constants.SaveFolderName != _creativeSaveName || Game1.currentLocation is not Farm
            || Game1.activeClickableMenu != null || Game1.fadeToBlack || Game1.fadeIn || Game1.fadeToBlackAlpha > 0)
            throw new InvalidOperationException("Creative farm image requires the original isolated farm with no active menu or fade; it never moves the player.");
        AssertHarnessFixtureBoundary(modEntry);
        var observer = ReadNativeMember(modEntry, "_observer")!;
        var map = Game1.currentLocation.Map;
        if (region is Rectangle bounds && (bounds.Width <= 0 || bounds.Height <= 0
            || bounds.X < 0 || bounds.Y < 0 || bounds.Right > map.Layers[0].LayerWidth || bounds.Bottom > map.Layers[0].LayerHeight
            || ReadNativeMember(ReadNativeMember(modEntry, "_authority"), "AgentOwns") is not false))
            throw new InvalidOperationException("Detail capture requires valid map bounds and settled player control.");
        bool hadRegion = map.Properties.TryGetValue("ScreenshotRegion", out var originalRegion);
        Dictionary<string, object> image;
        try
        {
            // Native ScreenshotRegion uses inclusive tile corners. Restore it
            // immediately; no world objects, player position or pixels are edited.
            if (region is Rectangle area)
                map.Properties["ScreenshotRegion"] = $"{area.X} {area.Y} {area.Right - 1} {area.Bottom - 1}";
            image = observer.GetType().GetMethod("InspectMapImage")!.Invoke(observer, new object[] { "Farm" })
                as Dictionary<string, object> ?? throw new InvalidOperationException("Native map image metadata is missing.");
        }
        finally
        {
            if (hadRegion) map.Properties["ScreenshotRegion"] = originalRegion;
            else map.Properties.Remove("ScreenshotRegion");
        }
        string source = image.GetValueOrDefault("path") as string
            ?? throw new InvalidOperationException("Native map image did not report its output path.");
        // Stardew 1.6.15 writes synchronously but leaves its FileStream open
        // until finalization. Share that writer handle while copying the PNG.
        byte[] bytes;
        using (var input = new FileStream(source, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        using (var buffer = new MemoryStream())
        {
            input.CopyTo(buffer);
            bytes = buffer.ToArray();
        }
        if (bytes.Length < 24 || !bytes.AsSpan(0, 8).SequenceEqual(new byte[] { 137, 80, 78, 71, 13, 10, 26, 10 }))
            throw new InvalidOperationException("Native farm screenshot is not a complete PNG header.");
        if (!bytes.AsSpan(bytes.Length - 12).SequenceEqual(new byte[] { 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130 }))
            throw new InvalidOperationException("Native farm screenshot has not finished writing its PNG trailer.");
        int width = BinaryPrimitives.ReadInt32BigEndian(bytes.AsSpan(16, 4));
        int height = BinaryPrimitives.ReadInt32BigEndian(bytes.AsSpan(20, 4));
        if (width <= 0 || height <= 0) throw new InvalidOperationException("Native farm screenshot has invalid dimensions.");
        string destination = Path.Combine(outDir, $"harness-farm-{label}.png");
        using (var output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write))
            output.Write(bytes);
        string sha256 = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(destination))).ToLowerInvariant();
        var state = ObserveCreativeFarm();
        var metadata = new { runId = _runId, label, captured = true, path = destination,
            sha256, width, height, timestampUtc = DateTime.UtcNow.ToString("o"),
            locationId = "Farm", nativeImage = image, detailRegion = region, creativeFarm = state, layout = CreativeLayout(Game1.getFarm()),
            provenance = "Native GameWorldObserver.InspectMapImage / takeMapScreenshot; original PNG copied without image editing." };
        File.WriteAllText(Path.Combine(outDir, $"harness-farm-{label}.json"), JsonSerializer.Serialize(metadata, new JsonSerializerOptions { WriteIndented = true }));
        File.WriteAllText(Path.Combine(outDir, $"creative-farm-layout-{label}.json"), JsonSerializer.Serialize(state));
        _evidence["creativeFarmLastCapture"] = metadata;
        CreativeEvent("farm-image", new { label, path = destination, sha256, width, height });
    }
}
