using System.Runtime.CompilerServices;
using HarmonyLib;
using Microsoft.Xna.Framework;
using Netcode;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewValley;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

[Collection("Native tool ambient location")]
public sealed class NativeForestryReceiptTests
{
    private static int collectionCalls;
    private static bool refuseSecondCollection;

    // Isolate the native inventory acceptance boundary; the production adapter
    // still selects real Debris/Chunk objects on this tile and removes only
    // accepted chunks. No game content manager is needed for this receipt test.
    private static bool AcceptCollection(ref bool __result)
    {
        __result = !(refuseSecondCollection && ++collectionCalls == 2);
        return false;
    }

    private static bool NotHostPaused(ref bool __result) { __result = false; return false; }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void MixedDropsOnOneTileKeepTheirIdentitiesThroughTheTerminalReceipt(bool rejectSecond)
    {
        using var world = new ForestryWorld();
        var harmony = new Harmony("StardewAI.Tests.MixedPickupReceipt");
        var collect = AccessTools.Method(typeof(Debris), nameof(Debris.collect), new[] { typeof(Farmer), typeof(Chunk) });
        harmony.Patch(collect, prefix: new HarmonyMethod(typeof(NativeForestryReceiptTests), nameof(AcceptCollection)));
        var hostPaused = AccessTools.PropertyGetter(typeof(Game1), nameof(Game1.HostPaused));
        harmony.Patch(hostPaused, prefix: new HarmonyMethod(typeof(NativeForestryReceiptTests), nameof(NotHostPaused)));
        var oldPlayer = Game1.player;
        var playerField = AccessTools.GetDeclaredFields(typeof(Game1)).Single(field => field.FieldType == typeof(Farmer));
        var game = AccessTools.Field(typeof(Game1), "game1").GetValue(null);
        try
        {
            collectionCalls = 0;
            refuseSecondCollection = rejectSecond;
            playerField.SetValue(game, ((IFarmerActor)world.Actor).GameFarmer);
            var wood = new Debris { item = new StardewValley.Object { ItemId = "388", Stack = 3, Quality = 0 } };
            var forage = new Debris { item = new StardewValley.Object { ItemId = "16", Stack = 2, Quality = 2 } };
            foreach (var drop in new[] { wood, forage })
            {
                drop.debrisType.Value = Debris.DebrisType.OBJECT;
                var chunk = new Chunk();
                chunk.position.Value = world.Tile * 64 + new Vector2(32, 32);
                drop.Chunks.Add(chunk);
                world.Location.debris.Add(drop);
            }
            var request = world.Request with { Kind = NativeActionKind.PickupItems, SkillId = "pickup-items" };
            var result = world.Adapter.Execute(world.Actor, request, request.Targets[0]);
            Assert.True(result.CollectedItems is not null, result.ErrorMessage + string.Join("\n", world.Monitor.LoggedMessages));
            Assert.Equal(!rejectSecond, result.Success);
            Assert.Null(result.ItemId); // No first-item identity carrying a mixed total.
            Assert.Equal(0, result.ItemCount);
            Assert.Equal(new NativeCollectedItem("(O)388", 0, 3), result.CollectedItems![0]);
            Assert.DoesNotContain(wood, world.Location.debris);
            if (rejectSecond)
            {
                Assert.Single(result.CollectedItems);
                Assert.Contains(forage, world.Location.debris);
            }
            else
            {
                Assert.Equal(new NativeCollectedItem("(O)16", 2, 2), result.CollectedItems[1]);
                Assert.Empty(world.Location.debris);
            }

            // Run the actual adapter receipt through the normal target-loop and
            // wire projection, including an inventory refusal after partial pickup.
            playerField.SetValue(game, oldPlayer);
            var machine = new NativeActionStateMachine(world.Actor, world.Observer,
                new SameMapNavigator(world.Observer), new ReturnedReceiptAdapter(result));
            Assert.True(machine.Start(request, out _));
            machine.StepTicks(150);
            var payload = machine.FinalResult!.ToTransportPayload();
            Assert.Equal(rejectSecond ? 0 : 1, payload.CompletedCount);
            var effect = Assert.Single(payload.Effects);
            Assert.False(effect.ContainsKey("itemId"));
            var items = Assert.IsType<List<Dictionary<string, object>>>(effect["collectedItems"]);
            Assert.Equal(rejectSecond ? 1 : 2, items.Count);
            Assert.Equal("(O)388", items[0]["itemId"]);
            Assert.Equal(3, items[0]["stack"]);
            if (!rejectSecond) Assert.Equal(2, items[1]["quality"]);
        }
        finally
        {
            playerField.SetValue(game, oldPlayer);
            harmony.Unpatch(collect, HarmonyPatchType.Prefix, harmony.Id);
            harmony.Unpatch(hostPaused, HarmonyPatchType.Prefix, harmony.Id);
        }
    }

    private sealed class ReturnedReceiptAdapter : INativeActionAdapter
    {
        private readonly NativeActionStepResult receipt;
        public ReturnedReceiptAdapter(NativeActionStepResult receipt) => this.receipt = receipt;
        public NativeActionStepResult Execute(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target) => receipt;
    }

    [Theory]
    [InlineData("weed")]
    [InlineData("chest")]
    [InlineData("machine")]
    [InlineData("empty")]
    public void DisappearedDropDoesNotRedirectPickupToAnUnderlyingObject(string underlying)
    {
        using var world = new ForestryWorld();
        StardewValley.Object? placed = underlying switch
        {
            "weed" => new StardewValley.Object { ItemId = "784", Name = "Weeds", Category = -999, CanBeGrabbed = true },
            "chest" => new StardewValley.Objects.Chest { CanBeGrabbed = true },
            "machine" => new StardewValley.Object { CanBeGrabbed = true, bigCraftable = { Value = true } },
            _ => null
        };
        if (placed is not null) world.Location.objects.Add(world.Tile, placed);
        var request = world.Request with { Kind = NativeActionKind.PickupItems, SkillId = "pickup-items" };
        var result = world.Adapter.Execute(world.Actor, request, request.Targets[0]);
        Assert.True(result.SkipReason == "no-item", result.ErrorMessage + string.Join("\n", world.Monitor.LoggedMessages));
        Assert.False(result.Success);
        if (placed is not null) Assert.Same(placed, world.Location.objects[world.Tile]);
        Assert.Equal(0, world.Axe.Swings);
    }

    [Fact]
    public void GroundForageAndAnimalProduceRemainEligibleForPickup()
    {
        Assert.True(NormalNativeActionAdapter.IsPickupGroundObject(new StardewValley.Object
            { ItemId = "16", Name = "Wild Horseradish", CanBeGrabbed = true }));
        Assert.True(NormalNativeActionAdapter.IsPickupGroundObject(new StardewValley.Object
            { ItemId = "176", Name = "Egg", Category = -5, CanBeGrabbed = true }));
    }

    [Fact]
    public void FinalNativeSwingCountsOnceAndReleasesExecution()
    {
        using var world = new ForestryWorld();
        world.Axe.OnSwing = () => world.Location.terrainFeatures.Remove(world.Tile);
        var machine = new NativeActionStateMachine(world.Actor, world.Observer,
            new SameMapNavigator(world.Observer), world.Adapter);
        Assert.True(machine.Start(world.Request, out _));
        machine.StepTicks(150);
        Assert.Equal(ExecutionState.Succeeded, machine.FinalResult!.FinalState);
        Assert.Equal("tree-felled", Assert.Single(machine.FinalResult.Effects).State);
        Assert.Equal(1, machine.FinalResult.ToTransportPayload().CompletedCount);
        Assert.Equal(1, world.Axe.Swings);
        Assert.False(machine.IsExecuting);
        Assert.False(world.Location.terrainFeatures.ContainsKey(world.Tile));
        // A new command must not claim the tree removed by the completed command.
        Assert.True(machine.Start(world.Request with { CommandId = "next", TaskId = "next" }, out _));
        machine.StepTicks(150);
        Assert.Empty(machine.FinalResult!.Effects);
        Assert.Equal("no-tree", Assert.Single(machine.FinalResult.Skipped).Reason);
        Assert.False(machine.IsExecuting);
        Assert.Equal(1, world.Axe.Swings);
    }

    [Fact]
    public void NativeFallRemovalBetweenCyclesBelongsOnlyToTheCommandThatDamagedIt()
    {
        using var world = new ForestryWorld();
        world.Axe.OnSwing = () => { world.Tree.health.Value = 5; world.Tree.stump.Value = true; world.Tree.falling.Value = true; };
        Assert.True(world.Execute().InProgress);
        Assert.True(world.Execute().InProgress);
        Assert.Equal(1, world.Axe.Swings);
        world.Location.terrainFeatures.Remove(world.Tile); // native terrain tick settles the fall
        var result = world.Execute();
        Assert.True(result.Success);
        Assert.Equal("tree-felled", result.State);
        Assert.Equal(0, result.StaminaCost);
        Assert.Equal("no-tree", world.Execute().SkipReason);
    }

    [Fact]
    public void CancelledCommandCannotCreditItsRemovedTreeToNewWork()
    {
        using var world = new ForestryWorld();
        world.Axe.OnSwing = () => { world.Tree.stump.Value = true; world.Tree.falling.Value = true; };
        var machine = new NativeActionStateMachine(world.Actor, world.Observer,
            new SameMapNavigator(world.Observer), world.Adapter);
        Assert.True(machine.Start(world.Request, out _));
        for (int tick = 0; tick < 150 && world.Axe.Swings == 0; tick++) machine.StepTicks(1);
        machine.RequestCancel("player takeover");
        machine.StepTicks(150);
        Assert.False(machine.IsExecuting);
        world.Location.terrainFeatures.Remove(world.Tile);
        var result = world.Adapter.Execute(world.Actor, world.Request with { CommandId = "new" }, world.Request.Targets[0]);
        Assert.Equal("no-tree", result.SkipReason);
        Assert.Equal(1, world.Axe.Swings);
    }

    private sealed class ForestryWorld : IDisposable
    {
        private readonly object? oldGame = AccessTools.Field(typeof(Game1), "game1").GetValue(null);
        public readonly Vector2 Tile = new(10, 11);
        public readonly GameLocation Location = new();
        public readonly Tree Tree = (Tree)RuntimeHelpers.GetUninitializedObject(typeof(Tree));
        public readonly ControlledAxe Axe = (ControlledAxe)RuntimeHelpers.GetUninitializedObject(typeof(ControlledAxe));
        public readonly SimulatedWorldObserver Observer = new();
        public readonly ForestryActor Actor;
        public readonly NormalNativeActionAdapter Adapter;
        public readonly TestMonitor Monitor = new();
        public readonly NativeActionRequest Request = new("chop", "chop", "Farm", NativeActionKind.ChopTree,
            "chop-tree", new[] { new NativeActionTarget(new TileCoordinate(10, 11)) });

        public ForestryWorld()
        {
            var game = (Game1)RuntimeHelpers.GetUninitializedObject(typeof(Game1));
            AccessTools.Field(typeof(Game1), "game1").SetValue(null, game);
            AccessTools.Field(typeof(Game1), "instanceGameLocation").SetValue(game, Location);
            Location.name.Value = "Farm";
            AccessTools.Field(typeof(Tree), "health").SetValue(Tree, new NetFloat(10));
            AccessTools.Field(typeof(Tree), "stump").SetValue(Tree, new NetBool());
            AccessTools.Field(typeof(Tree), "falling").SetValue(Tree, new NetBool());
            Location.terrainFeatures.Add(Tile, Tree);
            Actor = new ForestryActor(Axe);
            Adapter = new NormalNativeActionAdapter(Observer, Monitor);
        }
        public NativeActionStepResult Execute() => Adapter.Execute(Actor, Request, Request.Targets[0]);
        public void Dispose() => AccessTools.Field(typeof(Game1), "game1").SetValue(null, oldGame);
    }

    private sealed class ForestryActor : MechanicsActor, IFarmerActor
    {
        private readonly Axe axe;
        public ForestryActor(Axe axe) : base("forestry-test", initialPose:
            new AuthoritativePose("Farm", new TileCoordinate(10, 10), FacingDirection.Down)) => this.axe = axe;
        Farmer? IFarmerActor.GameFarmer { get; } = (Farmer)RuntimeHelpers.GetUninitializedObject(typeof(Farmer));
        T? IFarmerActor.FindTool<T>() where T : class => axe as T;
    }

    private sealed class ControlledAxe : Axe
    {
        public Action? OnSwing;
        public int Swings;
        public override void DoFunction(GameLocation location, int x, int y, int power, Farmer who)
        {
            Swings++;
            OnSwing!();
        }
    }
}
