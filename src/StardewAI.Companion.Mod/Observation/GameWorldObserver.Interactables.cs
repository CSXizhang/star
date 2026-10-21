using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Objects;
using StardewAI.Companion.Mod.Domain;

namespace StardewAI.Companion.Mod.Observation;

public sealed partial class GameWorldObserver
{
    private const int MaxInteractablesPerMap = 32;

    /// <summary>
    /// Lists natively interactable fixtures on one map (beds, TVs, the farm mailbox,
    /// giftbox parcels) so the agent can find them without guessing tiles. Detection
    /// uses only native object collections; read-only, bounded per map.
    /// </summary>
    public IReadOnlyList<Dictionary<string, object>> ScanInteractables(string locationName, int maxItems = MaxInteractablesPerMap)
    {
        var result = new List<Dictionary<string, object>>();
        try
        {
            if (!IsMainThread) return result;
            var location = Game1.getLocationFromName(locationName);
            if (location is null) return result;

            foreach (var furniture in location.furniture)
            {
                if (result.Count >= maxItems) return FinishInteractables(result);
                switch (furniture)
                {
                    case BedFurniture bed:
                        AddInteractable(result, "bed", (int)bed.TileLocation.X, (int)bed.TileLocation.Y, "bed");
                        break;
                    case TV tv:
                        AddInteractable(result, "tv", (int)tv.TileLocation.X, (int)tv.TileLocation.Y, "tv");
                        break;
                }
            }

            if (location is Farm farm)
            {
                if (result.Count < maxItems)
                {
                    Point mailbox = farm.GetMainMailboxPosition();
                    AddInteractable(result, "mailbox", mailbox.X, mailbox.Y, "mailbox");
                }
            }

            foreach (var pair in location.objects.Pairs)
            {
                if (result.Count >= maxItems) break;
                if (pair.Value is Chest { giftbox.Value: true } chest)
                {
                    string label = chest.giftboxIsStarterGift.Value ? "starter-gift" : "giftbox";
                    AddInteractable(result, "parcel", (int)pair.Key.X, (int)pair.Key.Y, label);
                }
            }
        }
        catch
        {
            // Observation only: a partially readable map degrades to fewer entries.
        }
        return FinishInteractables(result);
    }

    private static void AddInteractable(List<Dictionary<string, object>> list, string type, int x, int y, string label)
    {
        list.Add(new Dictionary<string, object>
        {
            ["type"] = type,
            ["tile"] = new Dictionary<string, object> { ["x"] = x, ["y"] = y },
            ["label"] = label,
        });
    }

    private static IReadOnlyList<Dictionary<string, object>> FinishInteractables(List<Dictionary<string, object>> list) =>
        list.OrderBy(i => (int)((Dictionary<string, object>)i["tile"])["y"]).ThenBy(i => (int)((Dictionary<string, object>)i["tile"])["x"]).ToList();
}
