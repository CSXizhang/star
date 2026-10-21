using StardewModdingAPI;
using StardewValley;

namespace StardewAI.Companion.Mod.PlayerMode;

/// <summary>Who currently holds authority to drive the bound player character.</summary>
public enum PlayerControlOwner
{
    /// <summary>The human player is in control. This is the initial and unload state.</summary>
    Human,

    /// <summary>
    /// The agent holds control, granted via
    /// <see cref="PlayerControlAuthority.RequestAgent"/> and revocable at any time by
    /// <see cref="PlayerControlAuthority.Takeover"/> (human) or
    /// <see cref="PlayerControlAuthority.Release"/> (agent/runtime).
    /// </summary>
    Agent
}

/// <summary>
/// Single-writer authority ledger for player mode. It decides — and records why — whether
/// the human or the agent may drive the bound player character. Authority is intentionally
/// separate from actor binding (<c>ActorGuards.IsPlayerBound</c> proves WHICH farmer an
/// actor wraps) and from decision contracts: it answers "who may act", not "how to act".
/// Phase 1 has exactly one controllable actor (the local player <c>Game1.player</c>), so a
/// single instance lives in <c>ModEntry</c>; future farmhands each get their own authority
/// instance, and gates must key off the actor's authority rather than a global flag.
/// All methods are game-thread only (SMAPI <c>UpdateTicked</c> / event handlers); the
/// single writer makes the lock a cheap safety net, and <see cref="Changed"/> fires
/// synchronously on the caller's thread right after each state change.
/// </summary>
public sealed class PlayerControlAuthority
{
    private readonly object _stateLock = new();
    private readonly Action<string, LogLevel>? _log;

    /// <summary>The owner that currently holds control. Defaults to <see cref="PlayerControlOwner.Human"/>.</summary>
    public PlayerControlOwner Owner { get; private set; } = PlayerControlOwner.Human;

    /// <summary>Reason string recorded by the last ownership change, or null before the first change.</summary>
    public string? OwnerReason { get; private set; }

    /// <summary>UTC timestamp of the last ownership change (initialised at construction).</summary>
    public DateTime ChangedAtUtc { get; private set; } = DateTime.UtcNow;

    /// <summary>True while the agent owns control; execution-class skills must refuse otherwise.</summary>
    public bool AgentOwns => Owner == PlayerControlOwner.Agent;

    /// <summary>
    /// Fired synchronously, on the game thread, immediately after every actual ownership
    /// change (never for idempotent no-ops), with the new owner and the recorded reason.
    /// </summary>
    public event Action<PlayerControlOwner, string>? Changed;

    /// <summary>Creates the authority ledger with an optional SMAPI-style log sink.</summary>
    public PlayerControlAuthority(Action<string, LogLevel>? log = null)
    {
        _log = log;
    }

    private void Log(string message, LogLevel level = LogLevel.Info) => _log?.Invoke(message, level);

    /// <summary>
    /// Requests agent ownership (Human → Agent). Returns false — without changing state —
    /// when ownership is unavailable: in multiplayer (<c>Game1.IsMultiplayer</c>; player
    /// mode is single-player only in phase 1 and fails explicitly) or when the agent
    /// already owns control (the caller should reconcile instead of double-claiming).
    /// Success records the reason and fires <see cref="Changed"/>.
    /// </summary>
    public bool RequestAgent(string reason)
    {
        lock (_stateLock)
        {
            if (Game1.IsMultiplayer)
            {
                Log($"player-mode agent control request denied: multiplayer is not supported in phase 1 (reason='{reason}').", LogLevel.Warn);
                return false;
            }

            if (Owner == PlayerControlOwner.Agent)
            {
                Log($"player-mode agent control request denied: agent already owns control (current reason='{OwnerReason}', new reason='{reason}').", LogLevel.Debug);
                return false;
            }

            Owner = PlayerControlOwner.Agent;
            OwnerReason = reason;
            ChangedAtUtc = DateTime.UtcNow;
        }

        Log($"player-mode control granted to the agent (reason='{reason}').");
        Changed?.Invoke(PlayerControlOwner.Agent, reason);
        return true;
    }

    /// <summary>
    /// Human takeover: any state → Human. The human always wins, even in multiplayer and
    /// even mid-action; integration cancels active state machines, halts the player and
    /// releases thinking pauses around this call. Idempotent: when the human already owns
    /// control this logs and fires no event.
    /// </summary>
    public void Takeover(string reason)
    {
        lock (_stateLock)
        {
            if (Owner == PlayerControlOwner.Human)
            {
                Log($"player takeover requested while the human already owns control (reason='{reason}'); no-op.", LogLevel.Debug);
                return;
            }

            Owner = PlayerControlOwner.Human;
            OwnerReason = reason;
            ChangedAtUtc = DateTime.UtcNow;
        }

        Log($"player-mode control taken over by the human (reason='{reason}').", LogLevel.Warn);
        Changed?.Invoke(PlayerControlOwner.Human, reason);
    }

    /// <summary>
    /// Runtime-initiated release (Agent → Human), used for transport disconnects, day
    /// end/save/title transitions and orderly shutdown. A no-op — logging at Debug level —
    /// when the agent does not currently own control.
    /// </summary>
    public void Release(string reason)
    {
        lock (_stateLock)
        {
            if (Owner != PlayerControlOwner.Agent)
            {
                Log($"player-mode release requested but the agent does not own control (reason='{reason}'); no-op.", LogLevel.Debug);
                return;
            }

            Owner = PlayerControlOwner.Human;
            OwnerReason = reason;
            ChangedAtUtc = DateTime.UtcNow;
        }

        Log($"player-mode control released back to the human (reason='{reason}').");
        Changed?.Invoke(PlayerControlOwner.Human, reason);
    }
}
