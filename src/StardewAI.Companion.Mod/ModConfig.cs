namespace StardewAI.Companion.Mod;

/// <summary>
/// SMAPI configuration for the mod (config.json in the mod folder), loaded via
/// <c>Helper.ReadConfig&lt;ModConfig&gt;()</c>. SMAPI maps config.json keys
/// case-insensitively onto these PascalCase properties.
/// </summary>
public sealed class ModConfig
{
    /// <summary>
    /// Operating mode: <c>"companion"</c> (default; the separate companion farmer with
    /// checkpoints, avatar and rest controller — legacy behavior, zero change) or
    /// <c>"player"</c> (the agent binds to the real local player <c>Game1.player</c>
    /// and drives it exclusively through native mechanics). Multiplayer sessions are
    /// rejected explicitly in player mode.
    /// </summary>
    public string Mode { get; set; } = "companion";

    /// <summary>
    /// Keyboard button (SMAPI <c>SButton</c> name, e.g. <c>"F9"</c>) that toggles player
    /// authority between the agent and the human. It is the ONLY input that transfers
    /// control: ordinary mouse clicks and movement keys never revoke the agent, and
    /// world-directed tool/action/movement input is suppressed while the agent owns
    /// the player (menus and other UI keep working).
    /// </summary>
    public string TakeoverKey { get; set; } = "F9";

    /// <summary>
    /// Single-player policy: while the model is thinking (an active thinking lease
    /// exists), pause the world clock via <c>Game1.isTimePaused</c>. The world, rendering
    /// and SMAPI ticks keep running, so transport heartbeats, snapshot publishing and
    /// command receipts all continue. This is a policy flag; the capability and the
    /// preexisting-state safety rules live in <see cref="PlayerMode.DecisionPauseController"/>.
    /// </summary>
    public bool PauseClockWhileThinking { get; set; } = true;

    /// <summary>
    /// Hard cap, in seconds, on a single thinking-clock-pause lease. When a lease ages
    /// past this duration it is released automatically so a stuck or cancelled provider
    /// call can never pause the world clock indefinitely. Default 120.
    /// </summary>
    public int ThinkingPauseMaxSeconds { get; set; } = 120;
}
