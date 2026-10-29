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
