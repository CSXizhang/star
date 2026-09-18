using Microsoft.Xna.Framework.Graphics;
using StardewValley;
using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Avatar;

/// <summary>
/// Companion Avatar presentation layer tied directly to the authoritative Farmer Mechanics Actor.
/// Renders actual in-game Farmer sprite, animations, and tool actions onto the world canvas.
/// Strictly forbids detached visual-only simulation.
/// </summary>
public sealed class CompanionAvatar
{
    private readonly IFarmerActor _actor;
    private readonly object _lock = new();

    public string CompanionId => _actor.CompanionId;
    public string CurrentAnimation { get; private set; } = "idle";
    public int CurrentFrame => _actor.CurrentFrame;
    public bool IsVisible { get; set; } = true;
    public bool IsDesynced { get; private set; }


    private readonly Action<string>? _log;
    private bool _firstDrawLogged;

    public CompanionAvatar(IFarmerActor actor, Action<string>? log = null)
    {
        _actor = actor ?? throw new ArgumentNullException(nameof(actor));
        _log = log;
        IsDesynced = false;
    }

    /// <summary>
    /// Renders the companion visually during SMAPI Display.RenderedWorld.
    /// Uses the actual Farmer draw routine including clothing, hair, tool animations, and shadow.
    /// </summary>
    public void Draw(SpriteBatch spriteBatch)
    {
        if (!IsVisible) return;

        lock (_lock)
        {
            var farmer = _actor.GameFarmer;
            if (farmer == null) return;

            GameLocation? currLoc = null;
            try { currLoc = Game1.currentLocation; } catch { }
            if (currLoc == null) return;

            string? currLocName = GetLocationName(currLoc);
            if (string.Equals(currLocName, _actor.LocationName, StringComparison.OrdinalIgnoreCase))
            {
                // Synchronize farmer location and event actor flags so Farmer.draw won't abort
                farmer.currentLocation = currLoc;
                farmer.isFakeEventActor = true;
                farmer.hidden.Value = false;
                farmer.viewingLocation.Value = null;

                if (!_firstDrawLogged)
                {
                    _firstDrawLogged = true;
                    try
                    {
                        var s = farmer.FarmerSprite;
                        _log?.Invoke($"[CompanionAvatar] First Draw: Loc={currLocName}, Pos={farmer.Position}, LocalPos={farmer.getLocalPosition(Game1.viewport)}, " +
                            $"Frame={s?.CurrentFrame}, spriteNull={s == null}, texNull={s?.Texture == null}, texSize={(s?.Texture == null ? "n/a" : $"{s.Texture.Width}x{s.Texture.Height}")}, " +
                            $"srcRect={s?.sourceRect}, rendererNull={farmer.FarmerRenderer == null}.");
                    }
                    catch { }
                }

                farmer.draw(spriteBatch);
            }
        }
    }

    private static string? GetLocationName(GameLocation? loc)
    {
        if (loc == null) return null;
        try
        {
            return !string.IsNullOrWhiteSpace(loc.NameOrUniqueName) ? loc.NameOrUniqueName : loc.Name;
        }
        catch
        {
            return loc.Name;
        }
    }

    /// <summary>
    /// Verifies that visual presentation is synchronized with the authoritative Mechanics Actor.
    /// </summary>
    public bool SyncToActor(out string? deviationReason)
    {
        lock (_lock)
        {
            try
            {
                GameLocation? currLoc = null;
                try { currLoc = Game1.currentLocation; } catch { }

                GameLocation? farmerLoc = null;
                try { farmerLoc = _actor.GameFarmer?.currentLocation; } catch { }

                string? currLocName = GetLocationName(currLoc);
                string? farmerLocName = GetLocationName(farmerLoc);

                if (currLoc != null &&
                    !string.Equals(currLocName, _actor.LocationName, StringComparison.OrdinalIgnoreCase) &&
                    farmerLoc != null &&
                    !string.Equals(farmerLocName, _actor.LocationName, StringComparison.OrdinalIgnoreCase))
                {
                    IsDesynced = true;
                    deviationReason = $"Map deviation detected between game and companion actor '{_actor.LocationName}'.";
                    return false;
                }
            }
            catch
            {
                // Headless test or pre-load safe guard
            }

            IsDesynced = false;
            deviationReason = null;
            return true;
        }
    }

    /// <summary>
    /// Triggers specific visual animations on the companion (e.g. "watering", "idle").
    /// Note: Presentation layer does NOT overwrite authoritative FarmerSprite animation frames.
    /// </summary>
    public void SetAnimation(string animation)
    {
        lock (_lock)
        {
            CurrentAnimation = animation;
        }
    }
}
