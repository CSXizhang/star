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
    private static bool PlacePassiveMachine(GameLocation __0, StardewValley.Object __1, int __2, int __3, ref bool __result)
    {
        // Native passive machines initialize output on the placement argument.
        // Emulate that side effect at the native boundary, including depletion.
        __1.heldObject.Value = new StardewValley.Object { ItemId = "340" };
        __0.objects.Add(new Vector2(__2 / 64, __3 / 64), __1);
        Game1.player.Items[Game1.player.CurrentToolIndex].Stack--;
        __result = true;
        return false;
    }

    [Fact]
    public void PassiveMachinePlacementDoesNotContaminateRemainingStackOrUnlockStoredContents()
    {
        var oldGame = AccessTools.Field(typeof(Game1), "game1").GetValue(null);
        var oldWorld = Game1.netWorldState;
        var harmony = new Harmony("StardewAI.Tests.PassiveMachinePlacement");
        var native = AccessTools.Method(typeof(Utility), nameof(Utility.tryToPlaceItem));
        try
        {
            var game = (Game1)RuntimeHelpers.GetUninitializedObject(typeof(Game1));
            AccessTools.Field(typeof(Game1), "game1").SetValue(null, game);
            Game1.netWorldState = new NetRoot<StardewValley.Network.NetWorldState>(new());
            var location = new GameLocation();
            location.name.Value = "Farm";
            AccessTools.Field(typeof(Game1), "instanceGameLocation").SetValue(game, location);
            var farmer = (Farmer)RuntimeHelpers.GetUninitializedObject(typeof(Farmer));
            AccessTools.Field(typeof(Farmer), "netItems").SetValue(farmer,
                new NetRef<StardewValley.Inventories.Inventory>(new()));
            AccessTools.Field(typeof(Farmer), "currentToolIndex").SetValue(farmer, new NetInt(0));
            AccessTools.Field(typeof(Farmer), "netItemStowed").SetValue(farmer, new NetBool(false));
            AccessTools.Field(typeof(Farmer), "temporaryItem").SetValue(farmer, new NetRef<Item>());
            AccessTools.Field(typeof(Character), "currentLocationRef").SetValue(farmer,
                new StardewValley.Network.NetLocationRef { Value = location });
            AccessTools.GetDeclaredFields(typeof(Game1)).Single(f => f.FieldType == typeof(Farmer)).SetValue(game, farmer);
            var source = new NativeRefusal { ItemId = "10", Stack = 3, bigCraftable = { Value = true } };
            farmer.Items.Add(source);
            var actor = new PlacementActor(farmer);
            var monitor = new TestMonitor();
            var adapter = new NormalNativeActionAdapter(new SimulatedWorldObserver(), monitor);
            harmony.Patch(native, prefix: new HarmonyMethod(typeof(NativePlacementLayerTests), nameof(PlacePassiveMachine)));
            foreach (var tile in new[] { new TileCoordinate(10, 11), new TileCoordinate(11, 10) })
            {
                var request = new NativeActionRequest("place", "place", "Farm", NativeActionKind.PlaceItems,
                    "place-items", new[] { new NativeActionTarget(tile) }, ItemId: source.QualifiedItemId);
                var result = adapter.Execute(actor, request, request.Targets[0]);
                Assert.True(result.Success, $"{result.ErrorMessage} {result.SkipReason}\n{string.Join("\n", monitor.LoggedMessages)}");
                Assert.Null(source.heldObject.Value);
                Assert.NotSame(source, location.objects[new Vector2(tile.X, tile.Y)]);
                Assert.NotNull(location.objects[new Vector2(tile.X, tile.Y)].heldObject.Value);
            }
            Assert.Equal(1, source.Stack);
            source.heldObject.Value = new StardewValley.Object { ItemId = "340" };
            var protectedRequest = new NativeActionRequest("protected", "protected", "Farm", NativeActionKind.PlaceItems,
                "place-items", new[] { new NativeActionTarget(new TileCoordinate(9, 10)) }, ItemId: source.QualifiedItemId);
            Assert.Equal("protected-contents", adapter.Execute(actor, protectedRequest, protectedRequest.Targets[0]).SkipReason);
            Assert.Equal(1, source.Stack);
            Assert.Equal(2, location.objects.Count());
        }
        finally
        {
            harmony.UnpatchAll(harmony.Id);
            Game1.netWorldState = oldWorld;
            AccessTools.Field(typeof(Game1), "game1").SetValue(null, oldGame);
        }
    }

    [Theory]
    [InlineData("floor", "native-placement-refused")]
    [InlineData("grass", "native-placement-refused")]
    [InlineData("soil", "native-placement-refused")]
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
                "crop" or "soil" => (HoeDirt)RuntimeHelpers.GetUninitializedObject(typeof(HoeDirt)),
                _ => null
            };
            if (terrain is HoeDirt dirt)
            {
                var nativeCrop = new NetRef<Crop>();
                AccessTools.Field(typeof(HoeDirt), "netCrop").SetValue(dirt, nativeCrop);
                if (ground == "crop")
                {
                    var crop = (Crop)RuntimeHelpers.GetUninitializedObject(typeof(Crop));
                    AccessTools.Field(typeof(Crop), "<NetFields>k__BackingField")
                        .SetValue(crop, new NetFields("placement-test-crop").SetOwner(crop));
                    nativeCrop.Value = crop;
                }
            }
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
            Assert.Equal(ground is "floor" or "grass" or "soil" ? 1 : 0, source.NativeChecks);
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
        int IFarmerActor.GetItemCount(string itemId) => farmer.Items.Where(i => i?.QualifiedItemId == itemId).Sum(i => i.Stack);
    }

    private sealed class NativeRefusal : StardewValley.Object
    {
        public int NativeChecks;
        private NativeRefusal? checkOwner;
        protected override Item GetOneNew() => new NativeRefusal { checkOwner = this, ItemId = ItemId, bigCraftable = { Value = bigCraftable.Value } };
        public override bool canBePlacedHere(GameLocation location, Vector2 tile, CollisionMask collisionMask = CollisionMask.All, bool showError = false)
        {
            (checkOwner ?? this).NativeChecks++;
            return false;
        }
    }
}
