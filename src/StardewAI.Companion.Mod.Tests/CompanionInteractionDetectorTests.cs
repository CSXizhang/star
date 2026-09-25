using StardewAI.Companion.Mod.Domain;
using Xunit;

namespace StardewAI.Companion.Mod.Tests;

/// <summary>
/// Contract §3: the life menu opens only when the player is within distance ≤ 1
/// (Manhattan; diagonal adjacency does NOT count) of the companion tile, presses
/// the interaction key, and either faces the companion or points the cursor at it.
/// </summary>
public class CompanionInteractionDetectorTests
{
    private readonly CompanionInteractionDetector _detector = new();

    private bool Opens(
        (int X, int Y) player, int facing, (int X, int Y) cursor, (int X, int Y) companion,
        bool pressed = true)
        => _detector.ShouldOpenLifeMenu(
            new TileCoordinate(player.X, player.Y), facing,
            new TileCoordinate(cursor.X, cursor.Y), new TileCoordinate(companion.X, companion.Y),
            pressed);

    [Fact]
    public void AdjacentFacingPlayer_OpensMenu()
    {
        // Player at (10,10) facing Down (2), companion directly below.
        Assert.True(Opens((10, 10), 2, (10, 10), (10, 11)));
        // Facing Right (1), companion to the right.
        Assert.True(Opens((10, 10), 1, (10, 10), (11, 10)));
        // Facing Up (0), companion above.
        Assert.True(Opens((10, 10), 0, (10, 10), (10, 9)));
        // Facing Left (3), companion to the left.
        Assert.True(Opens((10, 10), 3, (10, 10), (9, 10)));
    }

    [Fact]
    public void DiagonalAdjacency_DoesNotOpen_EvenFacingToward()
    {
        // Companion at (11,11): Manhattan distance 2, outside contract ≤1.
        Assert.False(Opens((10, 10), 2, (11, 11), (11, 11)), "diagonal is distance 2, not ≤1");
        Assert.False(Opens((10, 10), 1, (11, 11), (11, 11)));
        Assert.False(Opens((10, 10), 2, (10, 10), (11, 11)));
        Assert.False(Opens((10, 10), 2, (10, 10), (9, 9)));
    }

    [Fact]
    public void TwoTilesAway_DoesNotOpen_EvenWithCursorOnCompanion()
    {
        Assert.False(Opens((10, 10), 2, (10, 12), (10, 12)));
        Assert.False(Opens((10, 10), 1, (12, 10), (12, 10)));
    }

    [Fact]
    public void AdjacentButFacingAway_DoesNotOpen_UnlessCursorOnCompanion()
    {
        // Facing Up while companion is below.
        Assert.False(Opens((10, 10), 0, (10, 10), (10, 11)));
        // ...but pointing the cursor at the companion still opens (mouse interaction).
        Assert.True(Opens((10, 10), 0, (10, 11), (10, 11)));
    }

    [Fact]
    public void SameTileWithCursorOnCompanion_Opens()
    {
        Assert.True(Opens((10, 10), 2, (10, 10), (10, 10)));
    }

    [Fact]
    public void NoButtonPress_NeverOpens()
    {
        Assert.False(Opens((10, 10), 2, (10, 11), (10, 11), pressed: false));
        Assert.False(Opens((10, 10), 2, (10, 10), (10, 10), pressed: false));
    }

    [Fact]
    public void InvalidFacingDirection_RelyOnCursorOnly()
    {
        // Unknown facing (9) cannot satisfy the facing check; cursor must do it.
        Assert.False(Opens((10, 10), 9, (10, 10), (10, 11)));
        Assert.True(Opens((10, 10), 9, (10, 11), (10, 11)));
    }
}
