namespace StardewAI.Companion.Mod.Domain;

/// <summary>
/// Pure-logic detector: determines whether the player's current position, facing
/// direction, and cursor tile are close enough to the companion tile, and whether
/// the interaction button has been pressed, to warrant opening the life menu.
/// No StardewValley types are referenced; all inputs are primitives or value types.
/// </summary>
public sealed class CompanionInteractionDetector
{
    /// <summary>
    /// Maximum Manhattan distance (in tiles) between the player and the companion
    /// for an interaction to be valid (contract §3: distance ≤ 1, i.e. the four
    /// orthogonally adjacent tiles).
    /// </summary>
    public const int MaxInteractionDistance = 1;

    /// <summary>
    /// Returns <see langword="true"/> when the player is close enough to the companion
    /// and the interaction input has been asserted.
    /// </summary>
    /// <param name="playerTile">The tile the player currently occupies.</param>
    /// <param name="playerFacing">The direction the player is currently facing (0=Up,1=Right,2=Down,3=Left).</param>
    /// <param name="cursorTile">The game tile the mouse cursor is hovering over (may equal <paramref name="playerTile"/>).</param>
    /// <param name="companionTile">The tile the companion currently occupies.</param>
    /// <param name="interactionButtonPressed">
    /// <see langword="true"/> when the game's Action/Interact button was pressed this frame.
    /// </param>
    /// <returns><see langword="true"/> when the life menu should be opened.</returns>
    public bool ShouldOpenLifeMenu(
        TileCoordinate playerTile,
        int playerFacing,
        TileCoordinate cursorTile,
        TileCoordinate companionTile,
        bool interactionButtonPressed)
    {
        if (!interactionButtonPressed)
            return false;

        // Check if the companion is directly adjacent to the player, or the cursor
        // is pointing at the companion's tile.
        int distanceFromPlayer = playerTile.ManhattanDistanceTo(companionTile);
        bool playerAdjacent = distanceFromPlayer <= MaxInteractionDistance;

        if (!playerAdjacent)
            return false;

        // The companion must be reachable in the player's facing direction, OR the
        // cursor must be pointing at the companion (allowing mouse-based interaction).
        bool cursorOnCompanion = cursorTile == companionTile;
        bool facingCompanion = IsFacingTile(playerTile, playerFacing, companionTile);

        return cursorOnCompanion || facingCompanion;
    }

    /// <summary>
    /// Returns <see langword="true"/> when the player at <paramref name="from"/> is
    /// roughly facing <paramref name="target"/> given <paramref name="facingDirection"/>.
    /// </summary>
    private static bool IsFacingTile(TileCoordinate from, int facingDirection, TileCoordinate target)
    {
        int dx = target.X - from.X;
        int dy = target.Y - from.Y;

        return facingDirection switch
        {
            0 => dy < 0,           // Up: companion is above
            1 => dx > 0,           // Right: companion is to the right
            2 => dy > 0,           // Down: companion is below
            3 => dx < 0,           // Left: companion is to the left
            _ => false
        };
    }
}
