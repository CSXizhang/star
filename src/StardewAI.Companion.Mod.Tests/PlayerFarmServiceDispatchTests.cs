using System.Runtime.CompilerServices;
using System.Text.Json.Nodes;
using HarmonyLib;
using Netcode;
using StardewAI.Companion.Mod.Avatar;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.PlayerMode;
using StardewAI.Companion.Mod.Transport;
using StardewValley;
using StardewValley.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

[Collection("Native tool ambient location")]
public sealed class PlayerFarmServiceDispatchTests
{
    [Fact]
    public void PlayerBuildingPurchaseAndCareReachSharedAdapterButTakeoverStillRejects()
    {
        using var world = new World();
        var adapter = new TestNativeActionAdapter();
        var coordinator = world.Coordinator(adapter);
        var parameters = new WaterZoneParameters("Farm") { BuildingType = "Coop", Tile = new(20,20),
            BuildingName = "observed-coop", AnimalType = "White Chicken", AnimalName = "Hen", BudgetLimit = 4000 };
        var request = Request("build-building", parameters);
        Assert.False(coordinator.TryAcceptSkillExecute(request, Envelope(), out var blocked));
        Assert.Equal("PLAYER_CONTROL_NOT_HELD", blocked!.Error?.Code);
        Assert.True(coordinator.PlayerAuthority!.RequestAgent("test"));
        foreach (var skill in new[] { "build-building", "purchase-animal", "feed-animals" })
        {
            request = Request(skill, skill == "feed-animals" ? parameters with { Tile = null, BuildingName = "Farm" } : parameters);
            Assert.True(coordinator.TryAcceptSkillExecute(request, Envelope(), out blocked), blocked?.Error?.Message);
            for (int tick = 0; tick < 100 && coordinator.ActiveMachine != null; tick++) coordinator.Update(null, tick);
            Assert.True(coordinator.LastSkillResultPayload!.TerminalState == "succeeded", skill + ": " + coordinator.LastSkillResultPayload.Error);
        }
        Assert.Equal(new[] { NativeActionKind.BuildBuilding, NativeActionKind.PurchaseAnimal, NativeActionKind.FeedAnimals },
            adapter.Calls.Select(c => c.Kind));
        coordinator.PlayerAuthority.Takeover("F9");
        Assert.False(coordinator.TryAcceptSkillExecute(request, Envelope(), out blocked));
        Assert.Equal("PLAYER_CONTROL_NOT_HELD", blocked!.Error?.Code);
        Assert.Equal(3, adapter.CallCount);
    }

    [Fact]
    public void NativeShopQuoteReusesMatchingOpenMenuWithoutClosingItAndRejectsOtherShop()
    {
        using var world = new World();
        var observer = new SimulatedWorldObserver();
        observer.SetShopCounterTiles("AnimalShop", new[] { new TileCoordinate(10, 9) });
        var menu = (ShopMenu)RuntimeHelpers.GetUninitializedObject(typeof(ShopMenu));
        AccessTools.Field(typeof(ShopMenu), "ShopId").SetValue(menu, "AnimalShop");
        menu.itemPriceAndStock = new();
        SetMenu(menu);
        var adapter = new NormalPurchaseAdapter(observer);
        Assert.NotNull(adapter.GetRawShopStock("AnimalShop", world.Actor));
        Assert.Same(menu, Game1.activeClickableMenu);

        var coordinator = world.Coordinator(new TestNativeActionAdapter(), observer);
        coordinator.PlayerAuthority!.RequestAgent("test");
        var request = Request("purchase-items", new WaterZoneParameters("Farm")
        { ShopId = "AnimalShop", BudgetLimit = 100, Items = new() { new("178", 1) } });
        Assert.True(coordinator.TryAcceptSkillExecute(request, Envelope(), out var rejected), rejected?.Error?.Message);
        for (int tick = 0; tick < 10 && coordinator.ActiveMachine != null; tick++) coordinator.Update(null, tick);
        Assert.Equal("ITEM_UNAVAILABLE", coordinator.LastSkillResultPayload!.Error!.Code);

        AccessTools.Field(typeof(ShopMenu), "ShopId").SetValue(menu, "SeedShop");
        Assert.Null(adapter.GetRawShopStock("AnimalShop", world.Actor));
        Assert.Same(menu, Game1.activeClickableMenu);
        Assert.False(coordinator.TryAcceptSkillExecute(request, Envelope(), out rejected));
        Assert.Equal("MENU_OPEN", rejected!.Error!.Code);
    }

    [Fact]
    public void ServiceQuestionUsesNativeResponseLifecycleAndKeepsReplacementMenu()
    {
        using var world = new World();
        var coordinator = world.Coordinator(new TestNativeActionAdapter());
        coordinator.PlayerAuthority!.RequestAgent("test");
        var question = (ServiceQuestion)RuntimeHelpers.GetUninitializedObject(typeof(ServiceQuestion));
        question.isQuestion = true;
        question.responses = new[] { new Response("Shop", "Shop"), new Response("Construct", "Build") };
        Game1.currentLocation.lastQuestionKey = "carpenter";
        SetMenu(question);
        Game1.dialogueUp = true;
        var request = Request("native-answer", new WaterZoneParameters("ScienceHouse") { ChoiceIndex = 1 });
        // A first native click may only settle text: do not falsely report an answer.
        Assert.False(coordinator.TryAcceptSkillExecute(request, Envelope(), out var pending));
        Assert.Equal("QUESTION_NOT_READY", pending!.Error?.Code);
        Assert.Same(question, Game1.activeClickableMenu);
        question.Ready = true;
        Assert.True(coordinator.TryAcceptSkillExecute(request, Envelope(), out var error), error?.Error?.Message);
        Assert.Equal(1, question.ClickedChoice);
        Assert.Equal(2, question.Clicks);
        Assert.False(Game1.dialogueUp);
        Assert.Same(question.Replacement, Game1.activeClickableMenu);
    }

    private static void SetMenu(IClickableMenu? menu) => AccessTools.Field(typeof(Game1), "_activeClickableMenu").SetValue(null, menu);

    private static SkillExecutePayload Request(string skill, WaterZoneParameters parameters) => new(
        "command-" + skill, "task-" + skill, skill, "0.1", 1, parameters, new ExecutionBudgets(0,0,0), "safe-point", "test");
    private static EnvelopeDto Envelope() => new("0.1", "skill.execute", "message", "runtime-test", 1, 1,
        DateTimeOffset.UtcNow, new JsonObject(), "key");

    private sealed class BoundActor : MechanicsActor, IFarmerActor
    {
        public BoundActor(Farmer farmer) : base("bound-test", initialPose: new AuthoritativePose("Farm", 10, 10, FacingDirection.Down)) => Farmer = farmer;
        public Farmer Farmer { get; }
        Farmer? IFarmerActor.GameFarmer => Farmer;
    }
    private sealed class World : IDisposable
    {
        private readonly object? oldGame = AccessTools.Field(typeof(Game1), "game1").GetValue(null);
        private readonly IClickableMenu? oldMenu = Game1.activeClickableMenu;
        private readonly bool oldDialogue = Game1.dialogueUp;
        private readonly bool oldEvent = Game1.eventUp;
        private readonly NetRootDictionary<long, Farmer>? oldFarmers = Game1.otherFarmers;
        public readonly BoundActor Actor;
        public World()
        {
            var game = (Game1)RuntimeHelpers.GetUninitializedObject(typeof(Game1));
            AccessTools.Field(typeof(Game1), "game1").SetValue(null, game);
            var location = new GameLocation(); location.name.Value = "Farm";
            AccessTools.Field(typeof(Game1), "instanceGameLocation").SetValue(game, location);
            var farmer = (Farmer)RuntimeHelpers.GetUninitializedObject(typeof(Farmer));
            AccessTools.Field(typeof(Farmer), "netItems").SetValue(farmer, new NetRef<StardewValley.Inventories.Inventory>(new()));
            AccessTools.GetDeclaredFields(typeof(Game1)).Single(f => f.FieldType == typeof(Farmer)).SetValue(game, farmer);
            Game1.eventUp = false; Game1.dialogueUp = false; SetMenu(null);
            Game1.otherFarmers = new();
            Actor = new BoundActor(farmer);
        }
        public CompanionMechanicsCoordinator Coordinator(TestNativeActionAdapter adapter, SimulatedWorldObserver? worldObserver = null)
        {
            var observer = worldObserver ?? new SimulatedWorldObserver { CurrentLocationName = "Farm" };
            return new(Actor, observer, new SameMapNavigator(observer), new TestWateringCanAdapter(observer),
                new CompanionAvatar(Actor), nativeActionAdapter: adapter, purchaseAdapter: new NormalPurchaseAdapter(observer)) { PlayerAuthority = new PlayerControlAuthority() };
        }
        public void Dispose()
        {
            SetMenu(oldMenu); Game1.dialogueUp = oldDialogue; Game1.eventUp = oldEvent;
            Game1.otherFarmers = oldFarmers;
            AccessTools.Field(typeof(Game1), "game1").SetValue(null, oldGame);
        }
    }
    private sealed class ServiceQuestion : DialogueBox
    {
        private ServiceQuestion() : base("") { }
        public bool Ready;
        public int Clicks;
        public int ClickedChoice = -1;
        public IClickableMenu? Replacement;
        public override void receiveLeftClick(int x, int y, bool playSound = true)
        {
            Clicks++;
            if (!Ready) return;
            ClickedChoice = selectedResponse;
            // Stub only the native UI boundary; the coordinator must invoke it,
            // not call answerDialogueAction directly and strand dialogueUp.
            Game1.dialogueUp = false;
            Replacement = (IClickableMenu)RuntimeHelpers.GetUninitializedObject(typeof(CarpenterMenu));
            SetMenu(Replacement);
        }
    }
}
