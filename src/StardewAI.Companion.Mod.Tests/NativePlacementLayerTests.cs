using System.Runtime.CompilerServices;
using HarmonyLib;
using Microsoft.Xna.Framework;
using Netcode;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewValley;
using StardewValley.TerrainFeatures;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

[Collection("Native tool ambient location")]
public sealed class NativePlacementLayerTests
{
    [Theory]
    [InlineData("floor", "native-placement-refused")]
    [InlineData("grass", "native-placement-refused")]
    [InlineData("tree", "occupied")]
    [InlineData("crop", "occupied")]
    [InlineData("object", "occupied")]
    [InlineData("entrance", "protected-entrance")]
    public void GroundLayersReachNativePredicateButProtectedOccupantsNeverDo(string ground, string reason)
    {
        var oldGame = AccessTools.Field(typeof(Game1), "game1").GetValue(null);
        try
        {
            var game = (Game1)RuntimeHelpers.GetUninitializedObject(typeof(Game1));
            AccessTools.Field(typeof(Game1), "game1").SetValue(null, game);
            var location = new GameLocation();
            location.name.Value = "Farm";
            // This table deliberately has no rendering/crop-neighbor subscriptions.
            // Live probes cover native placement; this test exercises adapter guards.
            AccessTools.Field(typeof(GameLocation), "terrainFeatures").SetValue(location,
                new StardewValley.Network.NetVector2Dictionary<TerrainFeature, NetRef<TerrainFeature>>());
            AccessTools.Field(typeof(Game1), "instanceGameLocation").SetValue(game, location);
            var farmer = (Farmer)RuntimeHelpers.GetUninitializedObject(typeof(Farmer));
            AccessTools.Field(typeof(Farmer), "netItems").SetValue(farmer,
                new NetRef<StardewValley.Inventories.Inventory>(new()));
            AccessTools.Field(typeof(Character), "currentLocationRef").SetValue(farmer,
                new StardewValley.Network.NetLocationRef { Value = location });
            var source = new NativeRefusal { ItemId = "143", Stack = 2, bigCraftable = { Value = true } };
            farmer.Items.Add(source);
            var tile = new Vector2(10, 11);
            TerrainFeature? terrain = ground switch {
                "floor" => (Flooring)RuntimeHelpers.GetUninitializedObject(typeof(Flooring)),
                "grass" => (Grass)RuntimeHelpers.GetUninitializedObject(typeof(Grass)),
                "tree" => (Tree)RuntimeHelpers.GetUninitializedObject(typeof(Tree)),
                "crop" => (HoeDirt)RuntimeHelpers.GetUninitializedObject(typeof(HoeDirt)),
                _ => null
            };
            if (terrain != null) location.terrainFeatures.Add(tile, terrain);
            var occupied = new StardewValley.Object { ItemId = "390" };
            if (ground == "object") location.objects.Add(tile, occupied);
            var observer = new SimulatedWorldObserver();
            if (ground == "entrance") observer.SetWarpOrDoor(new TileCoordinate(10, 11), true);
            var adapter = new NormalNativeActionAdapter(observer, new TestMonitor());
            var request = new NativeActionRequest("place", "place", "Farm", NativeActionKind.PlaceItems,
                "place-items", new[] { new NativeActionTarget(new TileCoordinate(10, 11)) }, ItemId: source.QualifiedItemId);
            var result = adapter.Execute(new PlacementActor(farmer), request, request.Targets[0]);
            Assert.False(result.Success);
            Assert.Equal(reason, result.SkipReason);
            Assert.Equal(ground is "floor" or "grass" ? 1 : 0, source.NativeChecks);
            Assert.Equal(2, source.Stack);
            if (terrain != null) Assert.Same(terrain, location.terrainFeatures[tile]);
            if (ground == "object") Assert.Same(occupied, location.objects[tile]);
        }
        finally { AccessTools.Field(typeof(Game1), "game1").SetValue(null, oldGame); }
    }

    private sealed class PlacementActor : MechanicsActor, IFarmerActor
    {
        private readonly Farmer farmer;
        public PlacementActor(Farmer farmer) : base("placement-test", initialPose:
            new AuthoritativePose("Farm", new TileCoordinate(10, 10), FacingDirection.Down)) => this.farmer = farmer;
        Farmer? IFarmerActor.GameFarmer => farmer;
    }

    private sealed class NativeRefusal : StardewValley.Object
    {
        public int NativeChecks;
        private NativeRefusal? checkOwner;
        protected override Item GetOneNew() => new NativeRefusal { checkOwner = this };
        public override bool canBePlacedHere(GameLocation location, Vector2 tile, CollisionMask collisionMask = CollisionMask.All, bool showError = false)
        {
            (checkOwner ?? this).NativeChecks++;
            return false;
        }
    }
}
