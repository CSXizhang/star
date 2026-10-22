using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Buildings;
using StardewValley.GameData.Buildings;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;

namespace StardewAI.Companion.Mod.Adapters;

public sealed partial class NormalNativeActionAdapter
{
    // These are the non-UI transactions used by CarpenterMenu/PurchaseAnimalsMenu in
    // 1.6.15. Never construct those menus: they manipulate the human farmer and viewport.
    // The companion visits the service counter; the farm is the menu's remote target.
    private NativeActionStepResult BuildBuilding(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
        => OrderBuilding(actor, request, upgrade: false);

    private NativeActionStepResult UpgradeBuilding(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
        => OrderBuilding(actor, request, upgrade: true);

    private NativeActionStepResult OrderBuilding(IFarmerActor actor, NativeActionRequest request, bool upgrade)
    {
        var gate = CheckService(actor, animal: false);
        if (gate is not null) return gate;
        var farm = Game1.getFarm();
        if (request.LocationId != farm.NameOrUniqueName)
            return NativeActionStepResult.Precondition("Construction services currently target the farm.", "unsupported-building-location");
        if (string.IsNullOrWhiteSpace(request.BuildingType)
            || !Game1.buildingData.TryGetValue(request.BuildingType, out var data)
            || !SupportedBlueprint(data))
            return NativeActionStepResult.Precondition("Only ordinary Robin construction blueprints are supported.", "unsupported-blueprint");
        if (!GameStateQuery.CheckConditions(data.BuildCondition, farm, Game1.player))
            return NativeActionStepResult.Precondition("The farm has not unlocked this blueprint.", "blueprint-locked");
        if (Game1.IsThereABuildingUnderConstruction("Robin") || Game1.player.daysUntilHouseUpgrade.Value >= 0)
            return NativeActionStepResult.Precondition("Robin already has a construction or house upgrade order. Wait for normal game days.", "builder-busy");

        Building? existing = null;
        if (upgrade)
        {
            if (!Guid.TryParse(request.BuildingId, out var id) || (existing = farm.getBuildingById(id)) is null)
                return NativeActionStepResult.Precondition("buildingName must identify an observed farm building.", "building-not-found");
            if (existing.isUnderConstruction() || existing.isMoving)
                return NativeActionStepResult.Precondition("The building is still under construction or moving.", "building-unavailable");
            if (string.IsNullOrEmpty(data.BuildingToUpgrade) || existing.buildingType.Value != data.BuildingToUpgrade)
                return NativeActionStepResult.Precondition("The requested blueprint does not upgrade this building's current type.", "wrong-upgrade-source");
        }
        else
        {
            if (!string.IsNullOrEmpty(data.BuildingToUpgrade))
                return NativeActionStepResult.Precondition("This blueprint requires an existing building and upgrade-building.", "upgrade-required");
            if (request.DestinationTile is not { } tile)
                return NativeActionStepResult.Precondition("A farm destination tile is required.", "missing-destination");
            // Native construction clears terrain. Require an explicitly cleared footprint so
            // crops, paths and objects are not silently destroyed by ordering a building.
            var areas = new List<Rectangle> { new(tile.X, tile.Y, data.Size.X, data.Size.Y) };
            if (data.AdditionalPlacementTiles is not null)
                areas.AddRange(data.AdditionalPlacementTiles.Select(p => new Rectangle(tile.X + p.TileArea.X, tile.Y + p.TileArea.Y, p.TileArea.Width, p.TileArea.Height)));
            foreach (var area in areas)
            for (int y = area.Top; y < area.Bottom; y++)
            for (int x = area.Left; x < area.Right; x++)
            {
                var point = new Vector2(x, y);
                if (farm.terrainFeatures.ContainsKey(point) || farm.objects.ContainsKey(point)
                    || farm.GetFurnitureAt(point) is not null || !_observer.IsTilePassable(farm.NameOrUniqueName, new(x, y))
                    || _observer.IsWarpOrDoorTile(farm.NameOrUniqueName, new(x, y)))
                {
                    string blocker = farm.terrainFeatures.TryGetValue(point, out var feature) ? feature.GetType().Name
                        : farm.objects.ContainsKey(point) ? "object" : farm.GetFurnitureAt(point) is not null ? "furniture" : "terrain";
                    return NativeActionStepResult.Precondition($"Farm({x},{y}) has {blocker} in the proposed building footprint. Observe nearby clear sites before choosing whether to clear or relocate.", "occupied-footprint");
                }
            }
        }

        var materials = (data.BuildMaterials ?? new List<BuildingMaterial>())
            .GroupBy(m => ItemRegistry.QualifyItemId(m.ItemId))
            .ToDictionary(g => g.Key, g => g.Sum(m => m.Amount));
        foreach (var material in materials)
            if (material.Value < 0 || actor.GetItemCount(material.Key) < material.Value)
                return NativeActionStepResult.Precondition($"Companion needs {material.Value} of {material.Key}.", "missing-materials");
        var moneyGate = CheckServiceBudget(actor, request, data.BuildCost);
        if (moneyGate is not null) return moneyGate;
        int beforeCount = farm.buildings.Count;
        bool committed = false;
        return RunServiceTransaction(actor, data.BuildCost, materials, () =>
        {
            if (upgrade)
            {
                // This is the native menu's upgrade transaction, not FinishConstruction.
                existing!.upgradeName.Value = request.BuildingType;
                existing.daysUntilUpgrade.Value = Math.Max(data.BuildDays, 1);
                committed = true;
                Game1.netWorldState.Value.MarkUnderConstruction(data.Builder, existing);
            }
            else
            {
                var tile = request.DestinationTile!.Value;
                if (!farm.buildStructure(request.BuildingType, new Vector2(tile.X, tile.Y), actor.GameFarmer!, out var built, false, false))
                    return false;
                committed = true;
                if (built.isUnderConstruction()) Game1.netWorldState.Value.MarkUnderConstruction(data.Builder, built);
            }
            return true;
        }, () => committed || farm.buildings.Count != beforeCount,
            upgrade ? "building-upgrade-ordered" : "building-construction-ordered", request.BuildingType);
    }

    private NativeActionStepResult PurchaseAnimal(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var gate = CheckService(actor, animal: true);
        if (gate is not null) return gate;
        var farm = Game1.getFarm();
        if (request.LocationId != farm.NameOrUniqueName)
            return NativeActionStepResult.Precondition("Animal purchases currently target the farm.", "unsupported-building-location");
        if (!Guid.TryParse(request.BuildingId, out var id) || farm.getBuildingById(id) is not { } building
            || building.GetIndoors() is not AnimalHouse house)
            return NativeActionStepResult.Precondition("Choose an observed animal house on the farm.", "building-not-found");
        if (building.isUnderConstruction() || building.isMoving)
            return NativeActionStepResult.Precondition("Wait until the animal house is ready.", "building-under-construction");
        if (house.isFull()) return NativeActionStepResult.Precondition("The animal house is full (including residents currently outside).", "animal-house-full");
        var stock = Utility.getPurchaseAnimalStock(farm).FirstOrDefault(s => s.Name == request.AnimalType && s.Type is null);
        if (stock is null)
            return NativeActionStepResult.Precondition("The requested animal is not currently unlocked for sale.", "animal-not-for-sale");
        string name = request.AnimalName?.Trim() ?? "";
        if (name.Length == 0 || name.Length > 64 || name.Any(char.IsControl) || Utility.areThereAnyOtherAnimalsWithThisName(name))
            return NativeActionStepResult.Precondition("Supply a unique animal name of 1–64 characters.", "invalid-animal-name");
        int price = stock.salePrice();
        var moneyGate = CheckServiceBudget(actor, request, price);
        if (moneyGate is not null) return moneyGate;
        // Match the native alternate-purchase selection (e.g. ordinary chicken colors).
        string animalType = stock.Name;
        if (Game1.farmAnimalData.TryGetValue(animalType, out var animalData) && animalData.AlternatePurchaseTypes is not null)
        {
            foreach (var alternate in animalData.AlternatePurchaseTypes)
            {
                if (!GameStateQuery.CheckConditions(alternate.Condition)) continue;
                if (alternate.AnimalIds.Count > 0) animalType = alternate.AnimalIds[Game1.random.Next(alternate.AnimalIds.Count)];
                break;
            }
        }
        // Game1.multiplayer is internal; use its existing instance and native ID allocator,
        // never a guessed/random animal ID. No configurable reflection entry is exposed.
        var multiplayer = typeof(Game1).GetField("multiplayer", System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic)?.GetValue(null) as Multiplayer;
        if (multiplayer is null) return NativeActionStepResult.Precondition("Native animal ID allocator is unavailable.", "native-id-unavailable");
        var animal = new FarmAnimal(animalType, multiplayer.getNewID(), actor.GameFarmer!.UniqueMultiplayerID);
        if (!animal.CanLiveIn(building))
            return NativeActionStepResult.Precondition("This animal cannot live in the selected building.", "incompatible-animal-house");
        animal.Name = name;
        animal.displayName = name;
        return RunServiceTransaction(actor, price, new Dictionary<string, int>(), () =>
        {
            house.adoptAnimal(animal);
            return house.animals.ContainsKey(animal.myID.Value) && house.animalsThatLiveHere.Contains(animal.myID.Value)
                && ReferenceEquals(animal.homeInterior, house);
        }, () => house.animals.ContainsKey(animal.myID.Value), "animal-purchased", animal.type.Value);
    }

    private NativeActionStepResult? CheckService(IFarmerActor actor, bool animal)
    {
        if (Game1.IsMultiplayer || !Game1.IsMasterGame || actor.GameFarmer is null)
            return NativeActionStepResult.Precondition("Building and animal services currently require a single-player master game.", "unsupported-service-session");
        if (Game1.eventUp || Game1.isFestival())
            return NativeActionStepResult.Precondition("Services are unavailable during an event or festival.", "service-closed");
        if (GameLocation.AreStoresClosedForFestival())
            return NativeActionStepResult.Precondition("Services are closed for today's festival.", "service-closed");
        string locationName = animal ? "AnimalShop" : "ScienceHouse";
        var location = Game1.getLocationFromName(locationName);
        if (location is null || !ReferenceEquals(actor.GameFarmer.currentLocation, location))
            return NativeActionStepResult.Precondition($"Navigate to the {(animal ? "Marnie" : "Robin")} service counter in {locationName}.", "wrong-service-location");
        if (ServiceDoorWindow(locationName) is { } window && (Game1.timeOfDay < window.Open || Game1.timeOfDay >= window.Close))
            return NativeActionStepResult.Precondition("The service building is closed now.", "service-closed");
        string action = animal ? "AnimalShop" : "Carpenter";
        var counters = FindServiceCounters(location, action);
        // performAction requires the buyer south of the clicked counter for both services.
        var counter = counters.FirstOrDefault(t => actor.Tile.Y > t.Y && actor.Tile.IsAdjacentTo(t));
        if (!counters.Any(t => actor.Tile.Y > t.Y && actor.Tile.IsAdjacentTo(t)))
            return NativeActionStepResult.Precondition("Stand directly south of the observed service counter.", "not-at-service-counter");
        if (!ServiceOwnerAvailable(location, counter, animal))
            return NativeActionStepResult.Precondition("The native service owner is away from the counter. Return when service is available.", "service-owner-away");
        return null;
    }

    private static bool ServiceOwnerAvailable(GameLocation location, TileCoordinate counter, bool animal)
    {
        var owner = location.characters.FirstOrDefault(n => n.Name == (animal ? "Marnie" : "Robin"));
        if (!animal) return owner is not null && Vector2.Distance(owner.Tile, new(counter.X, counter.Y)) <= 3;
        // animalShop permits catalogue self-service; island honor-box only sells supplies.
        if (owner is null && Game1.IsVisitingIslandToday("Marnie")) return false;
        return Game1.player.stats.Get("Book_AnimalCatalogue") > 0
            || owner is not null && (owner.Tile == new Vector2(counter.X, counter.Y - 1)
                || owner.Tile == new Vector2(counter.X - 1, counter.Y - 1));
    }

    private static List<TileCoordinate> FindServiceCounters(GameLocation location, string action)
    {
        var found = new List<TileCoordinate>();
        var layer = location.Map?.GetLayer("Buildings");
        if (layer is null) return found;
        for (int y = 0; y < layer.LayerHeight; y++)
        for (int x = 0; x < layer.LayerWidth; x++)
            if (location.doesTileHaveProperty(x, y, "Action", "Buildings")?.Split(' ')[0] == action)
                found.Add(new(x, y));
        return found;
    }

    private static bool SupportedBlueprint(BuildingData data)
        => data.Builder == "Robin" && !data.MagicalConstruction && data.BuildDays > 0 && data.BuildCost >= 0;

    private static NativeActionStepResult? CheckServiceBudget(IFarmerActor actor, NativeActionRequest request, int cost)
    {
        if (cost < 0 || request.BudgetLimit is null || request.BudgetLimit < cost)
            return NativeActionStepResult.Precondition($"Explicit budget_limit must cover the current native price ({cost}).", "budget-exceeded");
        if (Game1.player.team.GetMoney(actor.GameFarmer!).Value < cost)
            return NativeActionStepResult.Precondition($"The native wallet needs {cost} gold.", "insufficient-funds");
        return null;
    }

    private NativeActionStepResult RunServiceTransaction(IFarmerActor actor, int cost, Dictionary<string, int> materials,
        Func<bool> apply, Func<bool> hasCommitted, string state, string itemId)
    {
        return InvokeIsolated(actor, state, () =>
        {
            var farmer = actor.GameFarmer!;
            var originals = farmer.Items.ToArray();
            var stacks = originals.Select(i => i?.Stack ?? 0).ToArray();
            int balanceBefore = Game1.player.team.GetMoney(farmer).Value;
            bool paid = false;
            void Rollback()
            {
                for (int i = 0; i < originals.Length; i++)
                {
                    farmer.Items[i] = originals[i];
                    if (originals[i] is not null) originals[i].Stack = stacks[i];
                }
                if (paid) Game1.player.team.AddIndividualMoney(farmer, cost);
            }
            try
            {
                foreach (var material in materials)
                    if (!actor.TryConsumeItem(material.Key, material.Value))
                    {
                        Rollback();
                        return NativeActionStepResult.Precondition("Materials changed before the order.", "missing-materials");
                    }
                Game1.player.team.AddIndividualMoney(farmer, -cost);
                paid = true;
                if (Game1.player.team.GetMoney(farmer).Value != balanceBefore - cost)
                    throw new InvalidOperationException("Native wallet deduction did not match the quoted price.");
                if (!apply())
                {
                    if (hasCommitted())
                        return NativeActionStepResult.Failed("Native transaction changed the world but verification failed; re-observe before retrying.", totalCost: cost);
                    Rollback();
                    return NativeActionStepResult.Precondition("Native placement rules refused the order; payment and materials restored.", "native-service-refused");
                }
                return NativeActionStepResult.Succeeded(state, itemId: itemId, itemCount: 1, totalCost: cost);
            }
            catch (Exception ex)
            {
                bool committed = hasCommitted();
                if (!committed) Rollback();
                return NativeActionStepResult.Failed($"Service transaction failed: {ex.Message}. {(committed ? "World changed; re-observe before retrying." : "Payment and materials restored.")}", totalCost: committed && paid ? cost : 0);
            }
        });
    }

    public static Dictionary<string, object> InspectBuildingServices(string locationId)
    {
        var farm = Game1.getFarm();
        if (locationId != farm.NameOrUniqueName)
            return new() { ["status"] = "unsupported-building-location" };
        object DescribeCounter(bool animal)
        {
            var location = Game1.getLocationFromName(animal ? "AnimalShop" : "ScienceHouse");
            var owner = Game1.getCharacterFromName(animal ? "Marnie" : "Robin");
            var counters = FindServiceCounters(location, animal ? "AnimalShop" : "Carpenter");
            var doorWindow = ServiceDoorWindow(location.NameOrUniqueName);
            bool festivalClosed = Game1.eventUp || Game1.isFestival() || GameLocation.AreStoresClosedForFestival();
            bool doorOpenNow = doorWindow is null || Game1.timeOfDay >= doorWindow.Value.Open && Game1.timeOfDay < doorWindow.Value.Close;
            bool ownerAtCounter = counters.Any(t => ServiceOwnerAvailable(location, t, animal));
            return new
            {
                locationId = location.NameOrUniqueName,
                counterTiles = counters.Select(t => new { x = t.X, y = t.Y }).ToArray(),
                approachTiles = counters.Select(t => new { x = t.X, y = t.Y + 1 }).ToArray(),
                currentTime = Game1.timeOfDay,
                doorOpenTime = doorWindow?.Open,
                doorCloseTime = doorWindow?.Close,
                doorOpenNow,
                festivalClosed,
                ownerAtCounter,
                ownerLocationId = owner?.currentLocation?.NameOrUniqueName,
                ownerTile = owner is null ? null : new { x = (int)owner.Tile.X, y = (int)owner.Tile.Y },
                // These are today's resolved native schedule, including special
                // dates, rain and island visits. Times start a route, not a promise
                // that the owner has reached the destination yet.
                scheduleKnown = owner?.Schedule is not null,
                scheduleStops = owner?.Schedule?.OrderBy(s => s.Key).Select(s => new
                {
                    departAt = s.Key, targetLocationId = s.Value.targetLocationName,
                    targetTile = new { x = s.Value.targetTile.X, y = s.Value.targetTile.Y },
                    targetNearCounter = s.Value.targetLocationName == location.NameOrUniqueName
                        && counters.Any(t => Vector2.Distance(new(t.X, t.Y), new(s.Value.targetTile.X, s.Value.targetTile.Y)) <= (animal ? 2 : 3))
                }).ToArray(),
                scheduleNote = "Native departure times; allow for travel and check the counter again on arrival.",
                available = !festivalClosed && doorOpenNow && ownerAtCounter,
                busy = !animal && (Game1.IsThereABuildingUnderConstruction("Robin") || Game1.player.daysUntilHouseUpgrade.Value >= 0)
            };
        }
        return new()
        {
            ["status"] = "ok",
            ["locationId"] = farm.NameOrUniqueName,
            ["constructionService"] = DescribeCounter(false),
            ["animalService"] = DescribeCounter(true),
            ["buildings"] = Game1.buildingData.Where(p => SupportedBlueprint(p.Value)).Select(p => new
            {
                buildingType = p.Key, price = p.Value.BuildCost, buildDays = p.Value.BuildDays,
                upgradeFrom = p.Value.BuildingToUpgrade, width = p.Value.Size.X, height = p.Value.Size.Y,
                capacity = p.Value.MaxOccupants,
                clearSiteCandidates = string.IsNullOrEmpty(p.Value.BuildingToUpgrade)
                    ? FindClearBuildingSites(farm, p.Value) : Array.Empty<TileCoordinate>(),
                unlocked = GameStateQuery.CheckConditions(p.Value.BuildCondition, farm, Game1.player)
                    && (string.IsNullOrEmpty(p.Value.BuildingToUpgrade) || farm.getNumberBuildingsConstructed(p.Value.BuildingToUpgrade, false) > 0),
                materials = (p.Value.BuildMaterials ?? new List<BuildingMaterial>()).Select(m => new { itemId = ItemRegistry.QualifyItemId(m.ItemId), count = m.Amount }).ToArray()
            }).ToArray(),
            ["animals"] = Utility.getPurchaseAnimalStock(farm).Select(s => new
            {
                animalType = s.Name, price = s.salePrice(), unlocked = s.Type is null,
                requiredBuilding = Game1.farmAnimalData[s.Name].RequiredBuilding
            }).ToArray()
        };
    }

    // A bounded read-only shortlist makes relocation a real option for the agent.
    // Never poke/clear tiles or invoke buildStructure while observing candidates.
    private static TileCoordinate[] FindClearBuildingSites(Farm farm, BuildingData data)
    {
        var layer = farm.Map?.Layers.FirstOrDefault();
        if (layer is null || data.Size.X <= 0 || data.Size.Y <= 0
            || data.Size.X > layer.LayerWidth || data.Size.Y > layer.LayerHeight) return Array.Empty<TileCoordinate>();
        bool Clear(int x, int y, bool passage = false)
        {
            var point = new Vector2(x, y);
            if (x < 0 || y < 0 || x >= layer.LayerWidth || y >= layer.LayerHeight
                || farm.terrainFeatures.ContainsKey(point) || farm.objects.ContainsKey(point)
                || farm.GetFurnitureAt(point) is not null
                || farm.warps.Any(w => w.X == x && w.Y == y)) return false;
            return farm.isBuildable(point, passage);
        }
        bool FootprintClear(TileCoordinate tile)
        {
            for (int y = 0; y < data.Size.Y; y++)
            for (int x = 0; x < data.Size.X; x++)
                if (!Clear(tile.X + x, tile.Y + y)) return false;
            if (data.AdditionalPlacementTiles is not null)
                foreach (var placement in data.AdditionalPlacementTiles)
                for (int y = placement.TileArea.Top; y < placement.TileArea.Bottom; y++)
                for (int x = placement.TileArea.Left; x < placement.TileArea.Right; x++)
                    if (!Clear(tile.X + x, tile.Y + y)) return false;
            return data.HumanDoor == new Point(-1, -1)
                || Clear(tile.X + data.HumanDoor.X, tile.Y + data.HumanDoor.Y + 1, passage: true);
        }
        var candidates = new List<TileCoordinate>();
        // Prefer central land, giving separated alternatives instead of dozens
        // of almost identical origins in one fragile patch.
        var origins = Enumerable.Range(0, layer.LayerHeight - data.Size.Y + 1)
            .SelectMany(y => Enumerable.Range(0, layer.LayerWidth - data.Size.X + 1).Select(x => new TileCoordinate(x, y)))
            .OrderBy(t => Math.Abs(t.X - layer.LayerWidth / 2) + Math.Abs(t.Y - layer.LayerHeight / 2));
        foreach (var tile in origins)
        {
            if (candidates.Any(c => Math.Abs(c.X - tile.X) < data.Size.X + 2 && Math.Abs(c.Y - tile.Y) < data.Size.Y + 2)
                || !FootprintClear(tile)) continue;
            candidates.Add(tile);
            if (candidates.Count == 3) break;
        }
        return candidates.ToArray();
    }

    // Read the same map action used by WorldMapGraph instead of assuming vanilla hours.
    private static (int Open, int Close)? ServiceDoorWindow(string targetLocation)
    {
        foreach (var source in Game1.locations)
        {
            var layer = source.Map?.GetLayer("Buildings");
            if (layer is null) continue;
            for (int y = 0; y < layer.LayerHeight; y++)
            for (int x = 0; x < layer.LayerWidth; x++)
            {
                var action = source.doesTileHaveProperty(x, y, "Action", "Buildings")?.Split(' ', StringSplitOptions.RemoveEmptyEntries);
                if (action is not { Length: >= 6 } || action[0] != "LockedDoorWarp"
                    || !string.Equals(action[3], targetLocation, StringComparison.OrdinalIgnoreCase)
                    || !int.TryParse(action[4], out int open) || !int.TryParse(action[5], out int close)) continue;
                return (open, close);
            }
        }
        return null;
    }
}
