using System.Reflection;
using System.Runtime.Serialization;
using HarmonyLib;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewAI.Companion.Mod.Adapters;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Navigation;
using StardewValley.Menus;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

[CollectionDefinition("Companion rest globals", DisableParallelization = true)]
public sealed class CompanionRestGlobalsCollection { }

[Collection("Companion rest globals")]
public class CompanionRestRecoveryTests
{
    private static CompanionRestController CreateRest(MechanicsActor actor)
    {
        var observer = new SimulatedWorldObserver();
        return new(actor, new NavigationStateMachine(actor, observer, new SameMapNavigator(observer), new WorldMapGraph()));
    }

    [Theory]
    [InlineData(0, 270, true)] [InlineData(27, 270, true)] [InlineData(28, 270, false)]
    [InlineData(20, 100, true)] [InlineData(21, 100, false)] [InlineData(0, 0, false)]
    public void LowStaminaTriggersBeforeExhaustion(float stamina, int maximum, bool expected) =>
        Assert.Equal(expected, CompanionBedtime.NeedsDaytimeRest(stamina, maximum));

    [Fact]
    public void DaytimeSleepRestoresAsRestAndOnlyWakesWhenRecovered()
    {
        int previous = Game1.timeOfDay;
        try
        {
            Game1.timeOfDay = 1200;
            var actor = new MechanicsActor { Stamina = 2 };
            var rest = CreateRest(actor);
            rest.RestoreSleep(1150, daytime: true);
            rest.Update(null, 1, null);
            Assert.Equal("resting", rest.State);
            Assert.True(rest.IsDaytimeRest);
            Assert.Equal(1150, rest.SleepStartedAt);
            Assert.Equal(2, actor.Stamina);
            actor.Stamina = actor.MaxStamina;
            rest.Update(null, 2, null);
            Assert.Equal("awake", rest.State);
            Assert.False(rest.IsDaytimeRest);
            Assert.Null(rest.SleepStartedAt);
            Assert.Null(actor.ActiveTaskId);
        }
        finally { Game1.timeOfDay = previous; }
    }

    [Fact]
    public void PauseKeepsDaytimeSleepAndBedtimePromotesItToOvernightSleep()
    {
        int previous = Game1.timeOfDay;
        try
        {
            Game1.timeOfDay = 1200;
            var actor = new MechanicsActor();
            var rest = CreateRest(actor);
            rest.RestoreSleep(1150, daytime: true);
            rest.Paused = true;
            rest.Update(null, 1, null);
            Assert.Equal("resting", rest.State);
            rest.Paused = false;
            Game1.timeOfDay = 2330;
            rest.Update(null, 2, null);
            Assert.Equal("sleeping", rest.State);
            Assert.False(rest.IsDaytimeRest);
            Assert.Equal(2330, rest.SleepStartedAt);
            rest.Update(null, 3, null);
            Assert.Equal("sleeping", rest.State);
        }
        finally { Game1.timeOfDay = previous; }
    }

    [Fact]
    public void EventPauseAndBlockingMenuKeepTheRestBoundaryClosed()
    {
        int previous = Game1.timeOfDay;
        bool previousPause = Game1.paused;
        bool previousEvent = Game1.eventUp;
        var previousMenu = Game1.activeClickableMenu;
        var menuField = AccessTools.Field(typeof(Game1), "_activeClickableMenu");
        try
        {
            Game1.timeOfDay = 1200;
            var rest = CreateRest(new MechanicsActor());
            rest.RestoreSleep(1150, daytime: true);
            Game1.paused = true;
            rest.Update(null, 1, null);
            Assert.Equal("resting", rest.State);
            Game1.paused = false;
            Game1.eventUp = true;
            rest.Update(null, 2, null);
            Assert.Equal("resting", rest.State);
            Game1.eventUp = false;
            // The live setter updates text entry and audio UI. This test only
            // needs the menu reference consumed by the rest boundary.
            menuField.SetValue(null, FormatterServices.GetUninitializedObject(typeof(InventoryMenu)));
            rest.Update(null, 3, null);
            Assert.Equal("resting", rest.State);
            menuField.SetValue(null, null);
            rest.Update(null, 4, null);
            Assert.Equal("awake", rest.State);
        }
        finally
        {
            Game1.timeOfDay = previous;
            Game1.paused = previousPause;
            Game1.eventUp = previousEvent;
            menuField.SetValue(null, previousMenu);
        }
    }

    private sealed class Work : ISkillExecutionMachine
    {
        public bool IsExecuting => true;
        public bool IsPaused { get; set; }
        public int TotalTargets => 3;
        public int CurrentTargetIndex => 1;
        public string? ActiveTaskId => "unfinished-work";
        public int PauseRequests;
        public List<string> Cancellations = new();
        public void Update(GameTime? time, long tickCount) { }
        public void RequestPause() => PauseRequests++;
        public void RequestCancel(string reason) => Cancellations.Add(reason);
        public void Resume() => IsPaused = false;
    }

    [Fact]
    public void DaytimeRestWaitsForSafePointAndPreservesUnfinishedWorkReason()
    {
        var rest = CreateRest(new MechanicsActor());
        rest.RestoreSleep(1200, daytime: true);
        var work = new Work();
        var finish = AccessTools.Method(typeof(CompanionRestController), "FinishWorkAtSafePoint");
        finish.Invoke(rest, new object[] { work });
        Assert.Equal(1, work.PauseRequests);
        Assert.Empty(work.Cancellations);
        work.IsPaused = true;
        finish.Invoke(rest, new object[] { work });
        finish.Invoke(rest, new object[] { work });
        Assert.Contains("LOW_STAMINA_REST", Assert.Single(work.Cancellations));
        Assert.Contains("until awake", work.Cancellations[0]);
        Assert.Equal(1, work.CurrentTargetIndex);
    }

    [Fact]
    public void BedRecoveryReversePatchBindsOnlyNativeResourceBlock()
    {
        var original = AccessTools.Method(typeof(Farmer), nameof(Farmer.Update), new[] { typeof(GameTime), typeof(GameLocation) });
        var block = NativeBedRecovery.ResourceBlock(PatchProcessor.GetOriginalInstructions(original)).ToList();
        Assert.DoesNotContain(block, i => i.operand is MethodInfo method && method.DeclaringType == typeof(Game1));
        Assert.DoesNotContain(block, i => Equals(i.operand, AccessTools.Field(typeof(Farmer), nameof(Farmer.isInBed))));
        Assert.Equal(System.Reflection.Emit.OpCodes.Ret, block[^1].opcode);
        var replacement = new Harmony("StardewAI.Tests.NativeBedRecovery").CreateReversePatcher(original,
            new HarmonyMethod(typeof(NativeBedRecovery), "Recover")).Patch();
        Assert.NotNull(replacement);
    }

    [Fact]
    public void NativeBedResourceTickRecoversGraduallyAndStopsAtCapacity()
    {
        var original = AccessTools.Method(typeof(Farmer), nameof(Farmer.Update), new[] { typeof(GameTime), typeof(GameLocation) });
        new Harmony("StardewAI.Tests.NativeBedTick").CreateReversePatcher(original,
            new HarmonyMethod(typeof(NativeBedRecovery), "Recover")).Patch();
        // Set up only resource fields; the full Farmer constructor needs game
        // content and a team UI, which the isolated recovery never reads.
        var farmer = (Farmer)FormatterServices.GetUninitializedObject(typeof(Farmer));
        AccessTools.Field(typeof(Farmer), "netStamina").SetValue(farmer, new Netcode.NetFloat());
        AccessTools.Field(typeof(Farmer), "maxStamina").SetValue(farmer, new Netcode.NetInt(270));
        AccessTools.Field(typeof(Farmer), nameof(Farmer.buffs)).SetValue(farmer, new StardewValley.Buffs.BuffManager());
        AccessTools.Field(typeof(StardewValley.Buffs.BuffManager), "Dirty").SetValue(farmer.buffs, false);
        farmer.Stamina = 2;
        farmer.health = 40;
        farmer.maxHealth = 100;
        farmer.regenTimer = 500;
        var recover = AccessTools.Method(typeof(NativeBedRecovery), "Recover");
        void Tick(int milliseconds) => recover.Invoke(null, new object?[] {
            farmer, new GameTime(TimeSpan.Zero, TimeSpan.FromMilliseconds(milliseconds)), null });
        Tick(250);
        Assert.Equal(2, farmer.Stamina);
        Assert.Equal(40, farmer.health);
        Tick(251);
        Assert.Equal(3, farmer.Stamina);
        Assert.Equal(41, farmer.health);
        farmer.Stamina = farmer.MaxStamina;
        farmer.health = farmer.maxHealth;
        Tick(501);
        Assert.Equal(farmer.MaxStamina, farmer.Stamina);
        Assert.Equal(farmer.maxHealth, farmer.health);
    }
}
