using StardewAI.Companion.Mod.Domain;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>Contract §1.7/§3 pure logic: eventKey de-dup, defer-while-blocked, day expiry, capacity ≤5.</summary>
public class CareHintControllerTests
{
    private const string Day1 = "1:spring:1";
    private const string Day2 = "1:spring:2";

    [Fact]
    public void UnblockedHint_IsReturnedImmediately_AndQueuedAsPending()
    {
        var controller = new CareHintController();
        controller.OnDayStarted(Day1);
        var hint = controller.ReceiveCareHint("morning:1:spring:1:d1", Day1, "morning", "早安，今天也一起加油吧", isBlocked: false);
        Assert.NotNull(hint);
        Assert.Equal("morning:1:spring:1:d1", hint!.EventKey);
        Assert.Single(controller.PendingHints);
    }

    [Fact]
    public void DuplicateEventKey_IsDropped()
    {
        var controller = new CareHintController();
        controller.OnDayStarted(Day1);
        Assert.NotNull(controller.ReceiveCareHint("k:1", Day1, "morning", "第一条", isBlocked: false));
        Assert.Null(controller.ReceiveCareHint("k:1", Day1, "morning", "重复条目", isBlocked: false));
        Assert.Single(controller.PendingHints);
        Assert.Equal("第一条", controller.PendingHints[0].Text);
    }

    [Fact]
    public void BlockedHint_IsDeferred_NotShownUntilDrained()
    {
        var controller = new CareHintController();
        controller.OnDayStarted(Day1);
        Assert.Null(controller.ReceiveCareHint("k:1", Day1, "work-done", "辛苦啦", isBlocked: true));
        // The Mod only drains once no menu/event is active.
        var drained = controller.TryDrainDeferred().ToList();
        Assert.Single(drained);
        Assert.Equal("k:1", drained[0].EventKey);
        Assert.Empty(controller.TryDrainDeferred());   // drained exactly once
    }

    [Fact]
    public void DayChange_DiscardsUndrainedDeferredHints()
    {
        var controller = new CareHintController();
        controller.OnDayStarted(Day1);
        Assert.Null(controller.ReceiveCareHint("k:1", Day1, "evening", "昨天的晚安", isBlocked: true));
        controller.OnDayStarted(Day2);
        Assert.Empty(controller.TryDrainDeferred());
        // The stale deferred hint must not resurface as pending either.
        Assert.DoesNotContain(controller.PendingHints, h => h.EventKey == "k:1");
    }

    [Fact]
    public void DayChange_DiscardsUnreadPendingHints_NoBackfill()
    {
        var controller = new CareHintController();
        controller.OnDayStarted(Day1);
        controller.ReceiveCareHint("k:1", Day1, "morning", "早安", isBlocked: false);
        controller.OnDayStarted(Day2);
        Assert.Empty(controller.PendingHints);
    }

    [Fact]
    public void HintForNonCurrentDay_IsDiscarded()
    {
        var controller = new CareHintController();
        controller.OnDayStarted(Day2);
        Assert.Null(controller.ReceiveCareHint("k:old", Day1, "morning", "昨天的消息", isBlocked: false));
        Assert.Empty(controller.PendingHints);
    }

    [Fact]
    public void PendingList_CapsAtFive_DroppingOldest()
    {
        var controller = new CareHintController();
        controller.OnDayStarted(Day1);
        for (int i = 1; i <= CareHintController.MaxPendingCount + 2; i++)
            controller.ReceiveCareHint($"k:{i}", Day1, "morning", $"第{i}条", isBlocked: false);
        Assert.Equal(CareHintController.MaxPendingCount, controller.PendingHints.Count);
        Assert.DoesNotContain(controller.PendingHints, h => h.EventKey == "k:1");
        Assert.DoesNotContain(controller.PendingHints, h => h.EventKey == "k:2");
        Assert.Equal("k:3", controller.PendingHints[0].EventKey);
        Assert.Equal("k:7", controller.PendingHints[^1].EventKey);
    }

    [Fact]
    public void MarkRead_RemovesOnlyThatHint()
    {
        var controller = new CareHintController();
        controller.OnDayStarted(Day1);
        controller.ReceiveCareHint("k:1", Day1, "morning", "一", isBlocked: false);
        controller.ReceiveCareHint("k:2", Day1, "evening", "二", isBlocked: false);
        controller.MarkRead("k:1");
        var remaining = Assert.Single(controller.PendingHints);
        Assert.Equal("k:2", remaining.EventKey);
    }
}
