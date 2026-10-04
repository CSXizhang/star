using System.Text.Json.Nodes;
using HarmonyLib;
using StardewValley;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewAI.Companion.Mod.Transport;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

public class ProductionCommandTests
{
    private sealed class Adapter : INativeActionAdapter
    {
        public NativeActionRequest? Request;
        public NativeActionStepResult Execute(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
        {
            Request=request;
            if(request.Kind==NativeActionKind.EatFood) actor.Stamina=30;
            return NativeActionStepResult.Succeeded("native-done",totalCost:750);
        }
    }

    [Theory]
    [InlineData("build-building",NativeActionKind.BuildBuilding)]
    [InlineData("upgrade-building",NativeActionKind.UpgradeBuilding)]
    [InlineData("purchase-animal",NativeActionKind.PurchaseAnimal)]
    public void ServicesStayAtCounterAndCarryExplicitDestinationAndBudget(string skill,NativeActionKind kind)
    {
        var actor=new MechanicsActor("service",initialPose:new AuthoritativePose("ScienceHouse",5,7,FacingDirection.Up));
        var observer=new SimulatedWorldObserver();
        var adapter=new Adapter();
        var coordinator=new CompanionMechanicsCoordinator(actor,observer,new SameMapNavigator(observer),new TestWateringCanAdapter(observer),nativeActionAdapter:adapter);
        var payload=new SkillExecutePayload("service-command","service-task",skill,"0.1",1,
            new WaterZoneParameters("Farm",null) { BuildingType="Coop",BuildingName="building-guid",AnimalType="White Chicken",AnimalName="A",Tile=new(30,25),BudgetLimit=5000 },
            new ExecutionBudgets(60,50,0),"safe-point","test");
        var envelope=new EnvelopeDto("0.1","skill.execute","message","runtime",1,1,DateTimeOffset.UtcNow,new JsonObject());
        Assert.True(coordinator.TryAcceptSkillExecute(payload,envelope,out var rejection),rejection?.Error?.Message);
        for(int i=0;i<100;i++) coordinator.Update(null,i);
        Assert.NotNull(adapter.Request);
        Assert.Equal(kind,adapter.Request!.Kind);
        Assert.Equal("Farm",adapter.Request.LocationId);
        Assert.Equal(new TileCoordinate(30,25),adapter.Request.DestinationTile);
        Assert.Equal(5000,adapter.Request.BudgetLimit);
        Assert.Equal("ScienceHouse",actor.LocationName);
        Assert.Equal(new TileCoordinate(5,7),actor.Tile);
    }

    [Fact]
    public void NativeCostIsPreservedOnFailedTerminal()
    {
        var result=new NativeActionResult("c","t","build-building",ExecutionState.Failed,
            Array.Empty<NativeActionEffect>(),Array.Empty<NativeActionEffect>(),Array.Empty<NativeActionEffect>(),0,0,0,1,totalCost:6000);
        Assert.Equal(6000,result.ToTransportPayload().Details!["totalCost"]);
        Assert.Equal(6000,NativeActionStepResult.Failed("committed but verification failed",totalCost:6000).TotalCost);
    }

    [Fact]
    public void OvernightReversePatchCompilesOnlyNativeResourceBlock()
    {
        var recovery=typeof(NormalNativeActionAdapter).Assembly.GetType("StardewAI.Companion.Mod.Adapters.NativeOvernightRecovery")!;
        var replacement=new Harmony("StardewAI.Tests.Overnight").CreateReversePatcher(
            AccessTools.Method(typeof(Farmer),nameof(Farmer.dayupdate)),new HarmonyMethod(AccessTools.Method(recovery,"Recover"))).Patch();
        Assert.NotNull(replacement);
    }
}
