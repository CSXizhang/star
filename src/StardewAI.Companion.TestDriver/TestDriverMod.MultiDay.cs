using System;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using System.Text.Json;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Internal;
using StardewValley.TerrainFeatures;

namespace StardewAI.Companion.TestDriver;

public sealed partial class TestDriverMod
{
    private const string MultiDayTreeKey = "CSXizhang.StardewAICompanion.TestDriver/MultiDayTree";
    private bool _multiDayActive;
    private int _multiSequence, _multiMoney, _multiDay, _multiCropId;
    private string _multiShipping = "[]";
    private readonly Dictionary<Vector2, (Crop Crop, int Id, bool Ready)> _multiCrops = new();
    private readonly Dictionary<Vector2, string> _multiProtected = new();
    private readonly HashSet<string> _multiLost = new(), _multiCommands = new();

    private void MultiEvent(string kind, object payload)
    {
        File.AppendAllText(Path.Combine(_evidenceDir!, "multi-day-ledger.jsonl"), JsonSerializer.Serialize(new {
            seq = ++_multiSequence, at = DateTime.UtcNow.ToString("o"), type = kind,
            year = Game1.year, season = Game1.currentSeason, day = Game1.dayOfMonth,
            time = Game1.timeOfDay, payload }) + Environment.NewLine);
    }

    private void ArrangeMultiDayFixture(object modEntry, string outDir, string label)
    {
        AssertHarnessFixtureBoundary(modEntry);
        ResolveCompanionActor(out var actor, out var farmer, out _, out _);
        if (_multiDayActive || farmer == null || !ReferenceEquals(farmer, Game1.player)
            || farmer.currentLocation is not Farm farm || Game1.year != 1
            || Game1.currentSeason != "spring" || Game1.dayOfMonth != 1)
            throw new InvalidOperationException("Multi-day fixture requires a fresh spring-1 isolated player, once only.");

        // Initial environment only. No subsequent reset, calendar assignment or crop growth override.
        foreach (var pair in farm.terrainFeatures.Pairs.ToArray())
        {
            if (pair.Value is Tree) farm.terrainFeatures.Remove(pair.Key);
            else if (pair.Value is HoeDirt dirt) dirt.crop = null;
        }
        var cleared = new Rectangle(49, 20, 19, 19);
        foreach (var tile in farm.objects.Keys.Where(v => cleared.Contains((int)v.X, (int)v.Y)).ToArray()) farm.objects.Remove(tile);
        foreach (var tile in farm.terrainFeatures.Keys.Where(v => cleared.Contains((int)v.X, (int)v.Y)).ToArray()) farm.terrainFeatures.Remove(tile);
        farm.debris.Clear();
        _harnessFixtureTiles.Clear();
        for (int y = 22; y < 28; y++) for (int x = 60; x < 66; x++) _harnessFixtureTiles.Add(new Vector2(x,y));
        _acceptanceChest = new Vector2(59,22);
        farm.objects[_acceptanceChest.Value] = new StardewValley.Objects.Chest(true, _acceptanceChest.Value, "130");
        _acceptanceMatureTrees.Clear();
        int treeId = 0;
        foreach (var tile in new[] {new Vector2(50,30),new Vector2(54,30),new Vector2(58,30),new Vector2(50,34),new Vector2(54,34),new Vector2(58,34),new Vector2(50,37),new Vector2(54,37)})
        {
            bool young = treeId >= 6;
            var tree = new Tree("1", young ? 2 : 5);
            string id = $"{_runId}-tree-{++treeId}";
            tree.modData[MultiDayTreeKey] = id;
            farm.terrainFeatures[tile] = tree;
            if (young) _multiProtected[tile] = id; else _acceptanceMatureTrees.Add(tile);
        }
        for (int i = 0; i < farmer.Items.Count; i++) if (farmer.Items[i] is not Tool) farmer.Items[i] = null;
        if (!farmer.Items.OfType<StardewValley.Tools.Axe>().Any()) farmer.addItemToInventory(new StardewValley.Tools.Axe());
        if (!farmer.Items.OfType<StardewValley.Tools.Hoe>().Any()) farmer.addItemToInventory(new StardewValley.Tools.Hoe());
        if (!farmer.Items.OfType<StardewValley.Tools.WateringCan>().Any()) farmer.addItemToInventory(new StardewValley.Tools.WateringCan());
        farmer.Money = 500;
        farmer.Stamina = farmer.MaxStamina;
        foreach (var can in farmer.Items.OfType<StardewValley.Tools.WateringCan>()) can.WaterLeft = can.waterCanMax;
        farm.getShippingBin(farmer).Clear();

        var observer = ReadNativeMember(modEntry, "_observer")!;
        var navigator = ReadNativeMember(modEntry, "_navigator")!;
        var tileType = modEntry.GetType().Assembly.GetType("StardewAI.Companion.Mod.Domain.TileCoordinate")!;
        var pathMethod = navigator.GetType().GetMethods().Single(m => m.Name == "FindPathToInteract" && m.GetParameters()[0].ParameterType.IsInstanceOfType(actor));
        var reachable = new List<object>();
        foreach (var tile in _harnessFixtureTiles.Concat(_acceptanceMatureTrees).Append(_acceptanceChest.Value))
        {
            var nativeTile = Activator.CreateInstance(tileType, (int)tile.X, (int)tile.Y)!;
            var path = pathMethod.Invoke(navigator, new object?[] {actor, "Farm", nativeTile, null});
            bool success = ReadNativeMember(path, "Success") is true;
            if (!success || _harnessFixtureTiles.Contains(tile) && farm.doesTileHaveProperty((int)tile.X,(int)tile.Y,"Diggable","Back") == null)
                throw new InvalidOperationException($"Multi-day initial target is not usable: {tile}");
            reachable.Add(new {x=(int)tile.X,y=(int)tile.Y,nativePathSuccess=success});
        }
        var cropData = new Crop("472",60,22,farm);
        var seed = (StardewValley.Object)ItemRegistry.Create("(O)472");
        var produce = (StardewValley.Object)ItemRegistry.Create("(O)24");
        // Feasibility evidence stays in the evaluator; it is not injected into player observations.
        var nativeStock = ShopBuilder.GetShopStock("SeedShop", DataLoader.Shops(Game1.content)["SeedShop"]);
        var nativeShop = new {
            shopId="SeedShop",source="native ShopBuilder catalog; evaluator only",
            items=nativeStock.Where(p=>p.Key.QualifiedItemId=="(O)472").Select(p=>new{
                itemId=p.Key.QualifiedItemId,price=p.Value.Price,stock=p.Value.Stock,
                isInfiniteStock=p.Value.Stock==int.MaxValue||p.Key.IsInfiniteStock()}).ToArray()
        };
        _harnessFixtureMode = "multi-day";
        _freezeTime = false;
        _multiDay = Game1.dayOfMonth; _multiMoney = farmer.Money;
        _multiDayActive = true;
        observer.GetType().GetMethod("BumpRevision")!.Invoke(observer,null);
        var fixture = new {
            runId=_runId, label, mode="multi-day", actorMode="player", actorId=farmer.UniqueMultiplayerID,
            saveName=Constants.SaveFolderName, isolatedSavesPath=StardewValley.Program.GetSavesFolder(),
            year=Game1.year,season=Game1.currentSeason,day=Game1.dayOfMonth,timeOfDay=Game1.timeOfDay,
            initialMoney=farmer.Money,initialInventory=DescribeItems(farmer.Items),
            cropData=new {seedId="(O)472",harvestId="(O)24",phaseDays=cropData.phaseDays.ToArray(),seedBasePrice=seed.Price,normalHarvestSellPrice=produce.sellToStorePrice()},
            nativeShop,
            plots=_harnessFixtureTiles.Select(v=>new{x=(int)v.X,y=(int)v.Y}),
            chest=new{x=59,y=22},nativeReachability=reachable,
            protectedTrees=_multiProtected.Select(p=>new{x=(int)p.Key.X,y=(int)p.Key.Y,identity=p.Value}),
            provenance="Initial isolated environment only; growth, purchase, shipping and days must advance natively."
        };
        File.WriteAllText(Path.Combine(outDir,$"harness-fixture-{label}.json"),JsonSerializer.Serialize(fixture));
        _evidence["harnessFixture"]=fixture;
        Helper.Events.GameLoop.DayEnding += (_, _) => { if (_multiDayActive) MultiEvent("day-ending",ObserveMultiDay()); };
        MultiEvent("initial", ObserveMultiDay());
    }

    private void ResumeMultiDayObservation()
    {
        ResolveCompanionActor(out _, out var farmer, out _, out _);
        if (_multiDayActive || farmer == null || !ReferenceEquals(farmer, Game1.player))
            throw new InvalidOperationException("Multi-day continuation requires the original loaded player, once only.");
        using var document = JsonDocument.Parse(File.ReadAllText(Path.Combine(_evidenceDir!, "harness-fixture-startup.json")));
        var fixture = document.RootElement;
        if (fixture.GetProperty("runId").GetString() != _runId
            || fixture.GetProperty("saveName").GetString() != Constants.SaveFolderName
            || fixture.GetProperty("mode").GetString() != "multi-day")
            throw new InvalidOperationException("Multi-day observer metadata belongs to another run/save.");
        foreach (var tree in fixture.GetProperty("protectedTrees").EnumerateArray())
            _multiProtected[new Vector2(tree.GetProperty("x").GetInt32(), tree.GetProperty("y").GetInt32())]
                = tree.GetProperty("identity").GetString()!;
        foreach (var line in File.ReadLines(Path.Combine(_evidenceDir!, "multi-day-ledger.jsonl")))
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            using var record = JsonDocument.Parse(line);
            var row = record.RootElement;
            _multiSequence = Math.Max(_multiSequence, row.GetProperty("seq").GetInt32());
            var payload = row.GetProperty("payload");
            string? kind = row.GetProperty("type").GetString();
            if (kind is "crop-planted" or "crop-removed" or "crop-restored")
                _multiCropId = Math.Max(_multiCropId, payload.GetProperty("id").GetInt32());
            if (kind == "native-result" && payload.TryGetProperty("commandId", out var command))
                _multiCommands.Add(command.GetString()!);
            if (kind == "protected-tree-lost") _multiLost.Add(payload.GetProperty("identity").GetString()!);
        }
        // Seed observation baselines from the loaded world. Restored crops must
        // never appear in the ledger as newly planted crops for a second cycle.
        var farm = Game1.getFarm();
        foreach (var pair in farm.terrainFeatures.Pairs)
            if (pair.Value is HoeDirt { crop: not null } dirt)
            {
                int id = ++_multiCropId;
                _multiCrops[pair.Key] = (dirt.crop, id, dirt.readyForHarvest());
                MultiEvent("crop-restored", new { id, x=(int)pair.Key.X, y=(int)pair.Key.Y });
            }
        _multiDay = Game1.dayOfMonth;
        _multiMoney = farmer.Money;
        _multiShipping = JsonSerializer.Serialize(DescribeItems(farm.getShippingBin(farmer)));
        _freezeTime = false;
        _multiDayActive = true;
        Helper.Events.GameLoop.DayEnding += (_, _) => { if (_multiDayActive) MultiEvent("day-ending", ObserveMultiDay()); };
        MultiEvent("resume", ObserveMultiDay());
    }

    private object ObserveMultiDay()
    {
        var farm=Game1.getFarm(); var farmer=Game1.player;
        var mod=Helper.ModRegistry.Get("CSXizhang.StardewAICompanion")!;
        var entry=mod.GetType().GetProperty("Mod")!.GetValue(mod)!;
        var authority=ReadNativeMember(entry,"_authority");
        return new {
            fixtureId=_runId,actorMode="player",actorId=farmer.UniqueMultiplayerID,
            actorTile=new{x=farmer.TilePoint.X,y=farmer.TilePoint.Y},actorLocation=farmer.currentLocation?.NameOrUniqueName,
            agentOwns=ReadNativeMember(authority,"AgentOwns"),money=farmer.Money,inventory=DescribeItems(farmer.Items),raining=Game1.isRaining,
            chests=farm.objects.Pairs.Where(p=>p.Value is StardewValley.Objects.Chest).Select(p=>new{
                tile=new{x=(int)p.Key.X,y=(int)p.Key.Y},items=DescribeItems(((StardewValley.Objects.Chest)p.Value).Items)}).ToArray(),
            crops=farm.terrainFeatures.Pairs.Where(p=>p.Value is HoeDirt {crop:not null}).Select(p=>DescribeCropTile(p.Key,(HoeDirt)p.Value)).ToArray(),
            trees=farm.terrainFeatures.Pairs.Where(p=>p.Value is Tree).Select(p=>new{
                tile=new{x=(int)p.Key.X,y=(int)p.Key.Y},identity=p.Value.modData.TryGetValue(MultiDayTreeKey,out var id)?id:null,
                stage=((Tree)p.Value).growthStage.Value,stump=((Tree)p.Value).stump.Value}).ToArray(),
            protectedTreesLost=_multiLost.ToArray(),shippedItems=DescribeItems(farm.getShippingBin(farmer)),
            ledgerSequence=_multiSequence,harnessTimeFrozen=_freezeTime
        };
    }

    private void TrackMultiDay()
    {
        var farm=Game1.getFarm();
        if (Game1.dayOfMonth!=_multiDay) {MultiEvent("day-started",ObserveMultiDay());_multiDay=Game1.dayOfMonth;}
        if (Game1.player.Money!=_multiMoney) {MultiEvent("money",new{before=_multiMoney,after=Game1.player.Money});_multiMoney=Game1.player.Money;}
        var shipped=DescribeItems(farm.getShippingBin(Game1.player));
        string shippingJson=JsonSerializer.Serialize(shipped);
        if (shippingJson!=_multiShipping) {MultiEvent("shipping-bin",shipped);_multiShipping=shippingJson;}
        foreach (var p in _multiProtected)
            if ((!farm.terrainFeatures.TryGetValue(p.Key,out var f)||f is not Tree tree||tree.stump.Value
                ||!tree.modData.TryGetValue(MultiDayTreeKey,out var id)||id!=p.Value) && _multiLost.Add(p.Value))
                MultiEvent("protected-tree-lost",new{identity=p.Value,x=(int)p.Key.X,y=(int)p.Key.Y});
        var current=farm.terrainFeatures.Pairs.Where(p=>p.Value is HoeDirt {crop:not null}).ToDictionary(p=>p.Key,p=>(HoeDirt)p.Value);
        foreach (var p in _multiCrops.ToArray())
            if (!current.TryGetValue(p.Key,out var dirt)||!ReferenceEquals(dirt.crop,p.Value.Crop)) {
                MultiEvent("crop-removed",new{id=p.Value.Id,x=(int)p.Key.X,y=(int)p.Key.Y,ready=p.Value.Ready,seedId=p.Value.Crop.netSeedIndex.Value});
                _multiCrops.Remove(p.Key);
            }
        foreach (var p in current) {
            if (!_multiCrops.TryGetValue(p.Key,out var previous)) {
                previous=(p.Value.crop!,++_multiCropId,p.Value.readyForHarvest());
                MultiEvent("crop-planted",new{id=previous.Id,x=(int)p.Key.X,y=(int)p.Key.Y,crop=DescribeCropTile(p.Key,p.Value)});
            }
            _multiCrops[p.Key]=(p.Value.crop!,previous.Id,p.Value.readyForHarvest());
        }
        var mod=Helper.ModRegistry.Get("CSXizhang.StardewAICompanion")!;
        var entry=mod.GetType().GetProperty("Mod")!.GetValue(mod)!;
        var result=ReadNativeMember(ReadNativeMember(entry,"_coordinator"),"LastSkillResultPayload");
        if (ReadNativeMember(result,"CommandId") is string command && _multiCommands.Add(command)) MultiEvent("native-result",result!);
    }
}
