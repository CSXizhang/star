using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Navigation;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class WorldMapGraphTests
{
    [Fact]
    public void FindLocationRoute_SameLocation_ReturnsSingleLocationRoute()
    {
        var graph = new WorldMapGraph();
        var route = graph.FindLocationRoute("Farm", "Farm", out var failureReason);

        Assert.NotNull(route);
        Assert.Null(failureReason);
        Assert.Single(route);
        Assert.Equal("Farm", route[0]);
    }

    [Fact]
    public void FindLocationRoute_DirectConnectedLocations_ReturnsTwoHops()
    {
        var graph = new WorldMapGraph();
        graph.AddEdge(new MapEdge(
            SourceLocation: "Farm",
            TargetLocation: "BusStop",
            SourceTile: new TileCoordinate(64, 15),
            TargetTile: new TileCoordinate(0, 10),
            EdgeKind: MapEdgeKind.Warp
        ));

        var route = graph.FindLocationRoute("Farm", "BusStop", out var failureReason);

        Assert.NotNull(route);
        Assert.Null(failureReason);
        Assert.Equal(2, route.Count);
        Assert.Equal(new[] { "Farm", "BusStop" }, route);
    }

    [Fact]
    public void FindLocationRoute_MultiHopChain_ResolvesFullSequence()
    {
        var graph = new WorldMapGraph();
        graph.AddEdge(new MapEdge("Farm", "BusStop", new TileCoordinate(64, 15), new TileCoordinate(0, 10), MapEdgeKind.Warp));
        graph.AddEdge(new MapEdge("BusStop", "Town", new TileCoordinate(35, 10), new TileCoordinate(0, 50), MapEdgeKind.Warp));
        graph.AddEdge(new MapEdge("Town", "SeedShop", new TileCoordinate(43, 58), new TileCoordinate(5, 20), MapEdgeKind.LockedDoorWarp, 900, 1700));

        var route = graph.FindLocationRoute("Farm", "SeedShop", out var failureReason);

        Assert.NotNull(route);
        Assert.Null(failureReason);
        Assert.Equal(new[] { "Farm", "BusStop", "Town", "SeedShop" }, route);
    }

    [Fact]
    public void FindLocationRoute_ChoosesShortestBFSPath()
    {
        var graph = new WorldMapGraph();
        // Path A: Farm -> BusStop -> Town (2 hops)
        graph.AddEdge(new MapEdge("Farm", "BusStop", new TileCoordinate(64, 15), new TileCoordinate(0, 10), MapEdgeKind.Warp));
        graph.AddEdge(new MapEdge("BusStop", "Town", new TileCoordinate(35, 10), new TileCoordinate(0, 50), MapEdgeKind.Warp));

        // Path B: Farm -> Backwoods -> Mountain -> Town (3 hops)
        graph.AddEdge(new MapEdge("Farm", "Backwoods", new TileCoordinate(30, 0), new TileCoordinate(15, 40), MapEdgeKind.Warp));
        graph.AddEdge(new MapEdge("Backwoods", "Mountain", new TileCoordinate(40, 15), new TileCoordinate(0, 20), MapEdgeKind.Warp));
        graph.AddEdge(new MapEdge("Mountain", "Town", new TileCoordinate(15, 40), new TileCoordinate(30, 0), MapEdgeKind.Warp));

        var route = graph.FindLocationRoute("Farm", "Town", out var failureReason);

        Assert.NotNull(route);
        Assert.Null(failureReason);
        Assert.Equal(new[] { "Farm", "BusStop", "Town" }, route);
    }

    [Fact]
    public void FindLocationRoute_DisconnectedLocation_ReturnsNullWithFailureReason()
    {
        var graph = new WorldMapGraph();
        graph.AddEdge(new MapEdge("Farm", "BusStop", new TileCoordinate(64, 15), new TileCoordinate(0, 10), MapEdgeKind.Warp));

        var route = graph.FindLocationRoute("Farm", "SkullCave", out var failureReason);

        Assert.Null(route);
        Assert.NotNull(failureReason);
        Assert.Contains("No route found between map 'Farm' and 'SkullCave'", failureReason);
    }

    [Theory]
    [InlineData("", "Town")]
    [InlineData("Farm", "")]
    [InlineData("   ", "Town")]
    public void FindLocationRoute_InvalidArguments_ThrowsArgumentException(string start, string target)
    {
        var graph = new WorldMapGraph();
        Assert.Throws<ArgumentException>(() => graph.FindLocationRoute(start, target, out _));
    }

    [Fact]
    public void GetOutgoingEdges_FiltersBySourceAndTarget()
    {
        var graph = new WorldMapGraph();
        var edge1 = new MapEdge("Farm", "BusStop", new TileCoordinate(64, 15), new TileCoordinate(0, 10), MapEdgeKind.Warp);
        var edge2 = new MapEdge("Farm", "Forest", new TileCoordinate(30, 60), new TileCoordinate(30, 0), MapEdgeKind.Warp);
        var edge3 = new MapEdge("Town", "SeedShop", new TileCoordinate(43, 58), new TileCoordinate(5, 20), MapEdgeKind.Door);

        graph.AddEdge(edge1);
        graph.AddEdge(edge2);
        graph.AddEdge(edge3);

        var farmOutgoing = graph.GetOutgoingEdges("Farm");
        Assert.Equal(2, farmOutgoing.Count);
        Assert.Contains(edge1, farmOutgoing);
        Assert.Contains(edge2, farmOutgoing);

        var toBusStop = graph.GetEdges("Farm", "BusStop");
        Assert.Single(toBusStop);
        Assert.Equal(edge1, toBusStop[0]);

        var toSeedShopFromFarm = graph.GetEdges("Farm", "SeedShop");
        Assert.Empty(toSeedShopFromFarm);
    }

    [Fact]
    public void CheckEdgeTraversable_RegularWarp_AlwaysTraversable()
    {
        var graph = new WorldMapGraph();
        var edge = new MapEdge("Farm", "BusStop", new TileCoordinate(64, 15), new TileCoordinate(0, 10), MapEdgeKind.Warp);

        bool traversable = graph.CheckEdgeTraversable(edge, currentTime: 200, farmer: null, out var failureReason, out var lockDetails);

        Assert.True(traversable);
        Assert.Null(failureReason);
        Assert.Null(lockDetails);
    }

    [Theory]
    [InlineData(600, false)]   // Before opening 0900 -> locked
    [InlineData(850, false)]   // 10 minutes before opening -> locked
    [InlineData(900, true)]    // Right at opening -> open
    [InlineData(1200, true)]   // Noon -> open
    [InlineData(1650, true)]   // Just before closing -> open
    [InlineData(1700, false)]  // Right at closing -> locked
    [InlineData(2200, false)]  // Night -> locked
    public void CheckEdgeTraversable_LockedDoorWarp_RespectsOperatingHours(int currentTime, bool expectedTraversable)
    {
        var graph = new WorldMapGraph();
        var edge = new MapEdge(
            SourceLocation: "Town",
            TargetLocation: "SeedShop",
            SourceTile: new TileCoordinate(43, 58),
            TargetTile: new TileCoordinate(5, 20),
            EdgeKind: MapEdgeKind.LockedDoorWarp,
            OpenTime: 900,
            CloseTime: 1700
        );

        bool traversable = graph.CheckEdgeTraversable(edge, currentTime, farmer: null, out var failureReason, out var lockDetails);

        Assert.Equal(expectedTraversable, traversable);
        if (!expectedTraversable)
        {
            Assert.NotNull(failureReason);
            Assert.Contains("is locked. Operating hours: 0900-1700", failureReason);
            Assert.NotNull(lockDetails);
            Assert.Equal("door-locked-hours", lockDetails["reason"]);
            Assert.Equal(900, lockDetails["openTime"]);
            Assert.Equal(1700, lockDetails["closeTime"]);
            Assert.Equal(currentTime, lockDetails["currentTime"]);
        }
        else
        {
            Assert.Null(failureReason);
            Assert.Null(lockDetails);
        }
    }

    [Fact]
    public void FindLocationRoute_DynamicBuildingIndoors_ResolvesBidirectionalRouteBetweenFarmAndIndoors()
    {
        var farm = new StardewValley.GameLocation();
        farm.name.Value = "Farm";

        var building = new StardewValley.Buildings.Building();
        building.tileX.Value = 58;
        building.tileY.Value = 12;
        building.humanDoor.Value = new Microsoft.Xna.Framework.Point(1, 2);
        building.daysOfConstructionLeft.Value = 0;
        building.parentLocationName.Value = "Farm";

        var oldNetWorldState = StardewValley.Game1.netWorldState;
        try
        {
            StardewValley.Game1.netWorldState = new Netcode.NetRoot<StardewValley.Network.NetWorldState>(new StardewValley.Network.NetWorldState());
            farm.buildings.Add(building);
        }
        finally
        {
            StardewValley.Game1.netWorldState = oldNetWorldState;
        }

        string coopUuidName = "Coop02ee4f58-34b0-424b-9a71-d6aaa608cf40";
        var coopIndoors = new StardewValley.GameLocation();
        coopIndoors.name.Value = "Coop";
        coopIndoors.uniqueName.Value = coopUuidName;
        coopIndoors.parentLocationName.Value = "Farm";

        // Interior return warp back to farm human door landing tile (59, 15)
        var returnWarp = new StardewValley.Warp(3, 14, "Farm", 59, 15, false);
        coopIndoors.warps.Add(returnWarp);

        building.indoors.Value = coopIndoors;

        var graph = new WorldMapGraph(locationsProvider: () => new[] { farm, coopIndoors });

            // 1. Route from Farm to dynamic indoors
            var forwardRoute = graph.FindLocationRoute("Farm", coopUuidName, out var failForward);
            Assert.NotNull(forwardRoute);
            Assert.Null(failForward);
            Assert.Equal(new[] { "Farm", coopUuidName }, forwardRoute);

            var forwardEdges = graph.GetEdges("Farm", coopUuidName);
            Assert.Single(forwardEdges);
            var forwardEdge = forwardEdges[0];
            Assert.Equal("Farm", forwardEdge.SourceLocation);
            Assert.Equal(coopUuidName, forwardEdge.TargetLocation);
            Assert.Equal(new TileCoordinate(59, 14), forwardEdge.SourceTile);
            Assert.Equal(new TileCoordinate(3, 13), forwardEdge.TargetTile);
            Assert.Equal(MapEdgeKind.Door, forwardEdge.EdgeKind);

            // 2. Route from dynamic indoors back to Farm
            var returnRoute = graph.FindLocationRoute(coopUuidName, "Farm", out var failReturn);
            Assert.NotNull(returnRoute);
            Assert.Null(failReturn);
            Assert.Equal(new[] { coopUuidName, "Farm" }, returnRoute);

            var returnEdges = graph.GetEdges(coopUuidName, "Farm");
            Assert.Single(returnEdges);
            var retEdge = returnEdges[0];
            Assert.Equal(coopUuidName, retEdge.SourceLocation);
            Assert.Equal("Farm", retEdge.TargetLocation);
            Assert.Equal(new TileCoordinate(3, 14), retEdge.SourceTile);
            Assert.Equal(new TileCoordinate(59, 15), retEdge.TargetTile);
            Assert.Equal(MapEdgeKind.Warp, retEdge.EdgeKind);

            // 3. Cache auto-updates when map changes (e.g. building under construction)
            building.daysOfConstructionLeft.Value = 1;
            var lockedRoute = graph.FindLocationRoute("Farm", coopUuidName, out var failLocked);
            Assert.Null(lockedRoute);
            Assert.NotNull(failLocked);

            // 4. Cache auto-updates when construction completes
            building.daysOfConstructionLeft.Value = 0;
            var reopenedRoute = graph.FindLocationRoute("Farm", coopUuidName, out var failReopened);
            Assert.NotNull(reopenedRoute);
            Assert.Null(failReopened);
            Assert.Equal(new[] { "Farm", coopUuidName }, reopenedRoute);
    }

    [Fact]
    public void FindLocationRoute_DualDynamicBuildingIndoors_DistinguishesInteriorsWithoutCrossContamination()
    {
        var farm = new StardewValley.GameLocation();
        farm.name.Value = "Farm";

        // Building 1: Coop Alpha
        var coop1Building = new StardewValley.Buildings.Building();
        coop1Building.tileX.Value = 58;
        coop1Building.tileY.Value = 12;
        coop1Building.humanDoor.Value = new Microsoft.Xna.Framework.Point(1, 2);
        coop1Building.daysOfConstructionLeft.Value = 0;
        coop1Building.parentLocationName.Value = "Farm";

        // Building 2: Coop Beta
        var coop2Building = new StardewValley.Buildings.Building();
        coop2Building.tileX.Value = 70;
        coop2Building.tileY.Value = 12;
        coop2Building.humanDoor.Value = new Microsoft.Xna.Framework.Point(1, 2);
        coop2Building.daysOfConstructionLeft.Value = 0;
        coop2Building.parentLocationName.Value = "Farm";

        var oldNetWorldState = StardewValley.Game1.netWorldState;
        try
        {
            StardewValley.Game1.netWorldState = new Netcode.NetRoot<StardewValley.Network.NetWorldState>(new StardewValley.Network.NetWorldState());
            farm.buildings.Add(coop1Building);
            farm.buildings.Add(coop2Building);
        }
        finally
        {
            StardewValley.Game1.netWorldState = oldNetWorldState;
        }

        string coop1Uuid = "Coop11111111-1111-1111-1111-111111111111";
        var coop1Indoors = new StardewValley.GameLocation();
        coop1Indoors.name.Value = "Coop"; // Shared base name
        coop1Indoors.uniqueName.Value = coop1Uuid;
        coop1Indoors.parentLocationName.Value = "Farm";
        coop1Indoors.warps.Add(new StardewValley.Warp(3, 14, "Farm", 59, 15, false));
        coop1Building.indoors.Value = coop1Indoors;

        string coop2Uuid = "Coop22222222-2222-2222-2222-222222222222";
        var coop2Indoors = new StardewValley.GameLocation();
        coop2Indoors.name.Value = "Coop"; // Shared base name
        coop2Indoors.uniqueName.Value = coop2Uuid;
        coop2Indoors.parentLocationName.Value = "Farm";
        coop2Indoors.warps.Add(new StardewValley.Warp(3, 14, "Farm", 71, 15, false));
        coop2Building.indoors.Value = coop2Indoors;

        var graph = new WorldMapGraph(locationsProvider: () => new[] { farm, coop1Indoors, coop2Indoors });

        // 1. Verify Farm -> Coop 1 route resolves to coop1 door (59, 14) and coop1 UUID
        var route1 = graph.FindLocationRoute("Farm", coop1Uuid, out var fail1);
        Assert.NotNull(route1);
        Assert.Null(fail1);
        Assert.Equal(new[] { "Farm", coop1Uuid }, route1);

        var edges1 = graph.GetEdges("Farm", coop1Uuid);
        Assert.Single(edges1);
        Assert.Equal(new TileCoordinate(59, 14), edges1[0].SourceTile);
        Assert.Equal(coop1Uuid, edges1[0].TargetLocation);

        // 2. Verify Farm -> Coop 2 route resolves to coop2 door (71, 14) and coop2 UUID
        var route2 = graph.FindLocationRoute("Farm", coop2Uuid, out var fail2);
        Assert.NotNull(route2);
        Assert.Null(fail2);
        Assert.Equal(new[] { "Farm", coop2Uuid }, route2);

        var edges2 = graph.GetEdges("Farm", coop2Uuid);
        Assert.Single(edges2);
        Assert.Equal(new TileCoordinate(71, 14), edges2[0].SourceTile);
        Assert.Equal(coop2Uuid, edges2[0].TargetLocation);

        // 3. Verify cross-coop route passes through Farm without mixing interiors
        var crossRoute = graph.FindLocationRoute(coop1Uuid, coop2Uuid, out var failCross);
        Assert.NotNull(crossRoute);
        Assert.Null(failCross);
        Assert.Equal(new[] { coop1Uuid, "Farm", coop2Uuid }, crossRoute);

        // 4. Verify FarmerMechanicsActor retains full unique name and does not truncate to "Coop"
        var actor = new FarmerMechanicsActor("test-companion", initialLocation: "Farm");

        // SetLocation with coop1 GameLocation (which has name="Coop" and uniqueName=coop1Uuid)
        actor.SetLocation(coop1Indoors, new TileCoordinate(2, 9));
        Assert.Equal(coop1Uuid, actor.LocationName);
        Assert.NotEqual("Coop", actor.LocationName);
        Assert.NotEqual(coop2Uuid, actor.LocationName);

        // Transition actor to coop2 GameLocation (which also has name="Coop" and uniqueName=coop2Uuid)
        actor.SetLocation(coop2Indoors, new TileCoordinate(6, 4));
        Assert.Equal(coop2Uuid, actor.LocationName);
        Assert.NotEqual("Coop", actor.LocationName);
        Assert.NotEqual(coop1Uuid, actor.LocationName);

        // Exported state must also persist full unique identity
        var state = actor.CapturePersistentState();
        Assert.Equal(coop2Uuid, state.Pose.LocationName);
    }
}
