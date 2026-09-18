using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Buildings;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;
using StardewAI.Companion.Mod.Domain;
using StardewAI.Companion.Mod.Execution;
using StardewAI.Companion.Mod.Observation;

namespace StardewAI.Companion.Mod.Adapters;

/// <summary>
/// Production adapter for the explicit native agricultural/husbandry actions.
///
/// Invariants (identical to the existing tool adapters):
/// 1. Main-thread and current-map execution only.
/// 2. The human player's resources and the <c>Game1.player</c> reference are
///    verified unchanged around every native call.
/// 3. Only native game APIs are used; nothing is fabricated and unsupported
///    requests return an actionable error instead of a simulated success.
/// 4. Actions happen at the companion's own position using the companion's own
///    inventory; no global state is edited from a distance.
/// </summary>
public sealed class NormalNativeActionAdapter : INativeActionAdapter
{
    private readonly IWorldObserver _observer;
    private readonly IMonitor _monitor;

    public NormalNativeActionAdapter(IWorldObserver observer, IMonitor monitor)
    {
        _observer = observer ?? throw new ArgumentNullException(nameof(observer));
        _monitor = monitor ?? throw new ArgumentNullException(nameof(monitor));
    }

    public NativeActionStepResult Execute(
        IFarmerActor actor,
        NativeActionRequest request,
        NativeActionTarget target)
    {
        ArgumentNullException.ThrowIfNull(actor);
        ArgumentNullException.ThrowIfNull(request);
        ArgumentNullException.ThrowIfNull(target);

        if (!_observer.IsMainThread)
            return NativeActionStepResult.Failed("Native action rejected: must execute on the game main thread.");

        // Feeding happens on the building's interior map, so the location check is
        // applied per-action against the actual target location instead of the
        // request's outer location id.
        try
        {
            return request.Kind switch
            {
                NativeActionKind.RefillWateringCan => RefillWateringCan(actor, request, target),
                NativeActionKind.ApplyFertilizer => ApplyFertilizer(actor, request, target),
                NativeActionKind.ClearDebris => ClearDebris(actor, request, target),
                NativeActionKind.PickupItems => PickupItems(actor, request, target),
                NativeActionKind.InsertMachine => InsertMachine(actor, request, target),
                NativeActionKind.CollectMachine => CollectMachine(actor, request, target),
                NativeActionKind.PetAnimal => PetAnimal(actor, request, target),
                NativeActionKind.FeedAnimals => FeedAnimals(actor, request, target),
                NativeActionKind.ToggleAnimalDoor => ToggleAnimalDoor(actor, request, target),
                NativeActionKind.CollectAnimalProduce => CollectAnimalProduce(actor, request, target),
                _ => NativeActionStepResult.Precondition($"unsupported native action '{request.Kind}'", "unsupported")
            };
        }
        catch (Exception ex)
        {
            _monitor.Log($"Native action {request.Kind} failed unexpectedly: {ex}", LogLevel.Error);
            return NativeActionStepResult.Failed($"{request.Kind} failed: {ex.Message}");
        }
    }

    // ------------------------------------------------------------------
    // Watering-can refill
    // ------------------------------------------------------------------
    private NativeActionStepResult RefillWateringCan(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var loc = ResolveLocation(request.LocationId);
        if (loc is null)
            return NativeActionStepResult.Failed($"Game location '{request.LocationId}' not found.");

        if (!actor.Tile.IsAdjacentTo(target.Tile) && actor.Tile != target.Tile)
            return NativeActionStepResult.Precondition(
                $"Actor at {actor.Tile} is not adjacent to refill tile {target.Tile}.",
                "not-adjacent");

        var can = actor.WateringCan;
        if (can is null)
            return NativeActionStepResult.Precondition("Companion does not possess a WateringCan.", "missing-tool", playerActionRequired: true);

        if (actor.WaterLeft >= actor.MaxWater)
            return NativeActionStepResult.Precondition(
                $"Watering can is already full ({actor.WaterLeft}/{actor.MaxWater}).",
                "already-full");

        bool canRefill;
        try { canRefill = loc.CanRefillWateringCanOnTile(target.Tile.X, target.Tile.Y); }
        catch (Exception ex)
        {
            return NativeActionStepResult.Failed($"CanRefillWateringCanOnTile threw: {ex.Message}");
        }
        if (!canRefill)
            return NativeActionStepResult.Precondition(
                $"Tile {target.Tile} is not a native watering-can refill tile.",
                "not-refillable");

        if (actor.GameFarmer is null)
            return NativeActionStepResult.Failed("Companion actor has no GameFarmer instance.");

        return InvokeIsolated(actor, "refill-watering-can", () =>
        {
            int before = actor.WaterLeft;
            int pixelX = target.Tile.X * 64 + 32;
            int pixelY = target.Tile.Y * 64 + 32;
            can.DoFunction(loc, pixelX, pixelY, power: 1, who: actor.GameFarmer);
            int after = actor.WaterLeft;
            if (after <= before)
                return NativeActionStepResult.Failed(
                    $"WateringCan.DoFunction ran but water did not increase ({before} -> {after}).");

            return NativeActionStepResult.Succeeded("refilled", waterGained: after - before, playerActionRequired: false);
        });
    }

    // ------------------------------------------------------------------
    // Fertilizer
    // ------------------------------------------------------------------
    private NativeActionStepResult ApplyFertilizer(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var loc = ResolveLocation(request.LocationId);
        if (loc is null)
            return NativeActionStepResult.Failed($"Game location '{request.LocationId}' not found.");

        if (!actor.Tile.IsAdjacentTo(target.Tile) && actor.Tile != target.Tile)
            return NativeActionStepResult.Precondition($"Actor at {actor.Tile} is not adjacent to target tile {target.Tile}.", "not-adjacent");

        string? fertilizerId = request.ItemId;
        if (string.IsNullOrWhiteSpace(fertilizerId))
            return NativeActionStepResult.Failed("fertilizerItemId is required for apply-fertilizer.");

        var v = new Vector2(target.Tile.X, target.Tile.Y);
        if (!loc.terrainFeatures.TryGetValue(v, out var tf) || tf is not HoeDirt dirt)
            return NativeActionStepResult.Precondition($"Tile {target.Tile} has no tilled soil.", "not-tilled");

        if (dirt.HasFertilizer())
            return NativeActionStepResult.Precondition($"Tile {target.Tile} already has fertilizer.", "already-fertilized");

        HoeDirtFertilizerApplyStatus status;
        try { status = dirt.CheckApplyFertilizerRules(fertilizerId); }
        catch (Exception ex)
        {
            return NativeActionStepResult.Failed($"CheckApplyFertilizerRules threw: {ex.Message}");
        }
        if (status != HoeDirtFertilizerApplyStatus.Okay)
            return NativeActionStepResult.Precondition(
                $"Native fertilizer rules rejected '{fertilizerId}' on tile {target.Tile}: {status}.",
                $"fertilizer-rules-{status}");

        if (actor.GetItemCount(fertilizerId) <= 0)
            return NativeActionStepResult.Precondition(
                $"Companion inventory has no '{fertilizerId}'.",
                "missing-item",
                playerActionRequired: true);

        if (actor.GameFarmer is null)
            return NativeActionStepResult.Failed("Companion actor has no GameFarmer instance.");

        return InvokeIsolated(actor, "apply-fertilizer", () =>
        {
            // Extract the fertilizer from the companion's own inventory first, so a
            // successful native application consumes exactly one unit and a failed
            // one leaves the inventory untouched.
            if (!actor.TryExtractItem(fertilizerId, 1, out var extracted) || extracted.Count == 0)
                return NativeActionStepResult.Precondition($"Companion inventory has no '{fertilizerId}'.", "missing-item", playerActionRequired: true);

            bool applied;
            try
            {
                applied = dirt.plant(fertilizerId, actor.GameFarmer, isFertilizer: true);
            }
            catch (Exception ex)
            {
                foreach (var item in extracted)
                    actor.TryAddItemToInventory(item);
                return NativeActionStepResult.Failed($"HoeDirt.plant threw: {ex.Message}");
            }

            if (!applied || !dirt.HasFertilizer())
            {
                foreach (var item in extracted)
                    actor.TryAddItemToInventory(item);
                return NativeActionStepResult.Failed(
                    $"HoeDirt.plant did not apply fertilizer '{fertilizerId}' on tile {target.Tile}.");
            }

            return NativeActionStepResult.Succeeded("fertilized", itemId: fertilizerId, itemCount: 1);
        });
    }

    // ------------------------------------------------------------------
    // Debris clearance (weeds / stones / twigs, explicit tiles only)
    // ------------------------------------------------------------------
    private NativeActionStepResult ClearDebris(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var loc = ResolveLocation(request.LocationId);
        if (loc is null)
            return NativeActionStepResult.Failed($"Game location '{request.LocationId}' not found.");

        if (!actor.Tile.IsAdjacentTo(target.Tile) && actor.Tile != target.Tile)
            return NativeActionStepResult.Precondition($"Actor at {actor.Tile} is not adjacent to target tile {target.Tile}.", "not-adjacent");

        var obj = loc.getObjectAtTile(target.Tile.X, target.Tile.Y);
        if (obj is null)
            return NativeActionStepResult.Precondition($"Tile {target.Tile} has no object to clear.", "no-object");

        if (obj is StardewValley.Objects.Chest)
            return NativeActionStepResult.Precondition($"Tile {target.Tile} holds a chest; chests are never cleared.", "protected-object");

        StardewValley.GameData.Machines.MachineData? machineData;
        try { machineData = obj.GetMachineData(); }
        catch { machineData = null; }
        if (machineData is not null)
            return NativeActionStepResult.Precondition($"Tile {target.Tile} holds a machine; machines are never cleared.", "protected-object");

        // Native game identity is authoritative: IsWeeds()/IsBreakableStone()/IsTwig()
        // are the exact predicates Object.performToolAction itself branches on. No
        // Type/ContextTags string matching and no item-id whitelist.
        bool isWeed;
        try { isWeed = obj.IsWeeds(); }
        catch { isWeed = false; }

        bool isStone = false;
        bool isTwig = false;
        try
        {
            isStone = obj.IsBreakableStone();
            isTwig = !isStone && obj.IsTwig();
        }
        catch { isStone = false; isTwig = false; }

        if (!isWeed && !isStone && !isTwig)
        {
            string identity = $"{obj.QualifiedItemId ?? obj.ItemId} ({obj.DisplayName})";
            return NativeActionStepResult.Precondition(
                $"Tile {target.Tile} holds '{identity}', which is not a native weed, stone or twig. " +
                "Only those native obstacles are cleared by this action.",
                "unsupported-debris-type",
                playerActionRequired: false);
        }

        // Native tool choice matches the game's own branches (Tool.isHeavyHitter() covers
        // Hoe/Axe/Pickaxe; Object.performToolAction then switches on the concrete tool):
        // weeds with the Hoe (Axe fallback), stones with the Pickaxe, twigs with the Axe.
        Tool? tool;
        string requiredTool;
        if (isWeed)
        {
            tool = actor.Hoe ?? (Tool?)actor.FindTool<Axe>();
            requiredTool = "Hoe";
        }
        else if (isStone)
        {
            tool = actor.FindTool<Pickaxe>();
            requiredTool = "Pickaxe";
        }
        else
        {
            tool = actor.FindTool<Axe>() ?? (Tool?)actor.FindTool<Pickaxe>();
            requiredTool = "Axe";
        }

        if (tool is null)
            return NativeActionStepResult.Precondition(
                $"Companion does not possess the native {requiredTool} needed to clear this " +
                $"{(isStone ? "stone" : isTwig ? "twig" : "weed")} at {target.Tile}.",
                $"missing-tool:{requiredTool}",
                playerActionRequired: true);

        if (actor.GameFarmer is null)
            return NativeActionStepResult.Failed("Companion actor has no GameFarmer instance.");

        string toolName = tool.GetType().Name;
        return InvokeIsolated(actor, "clear-debris", () =>
        {
            // The genuine player action is the tool swing. Axe/Pickaxe/Hoe.DoFunction pays
            // the companion's own stamina, sets lastUser, runs GameLocation.performToolAction
            // and then removes the obstacle object / spawns its native drops (verified in
            // the game assembly). Object.performToolAction alone only reports destroyability
            // and never removes the object, so it must not be used as the effect.
            float staminaBefore = actor.Stamina;
            tool.DoFunction(loc, target.Tile.X * 64 + 32, target.Tile.Y * 64 + 32, power: 1, who: actor.GameFarmer);
            bool stillThere = loc.getObjectAtTile(target.Tile.X, target.Tile.Y) is not null;
            if (stillThere)
            {
                // Native tool action ran but removed nothing: report the honest failure
                // with the native tool actually used instead of a fabricated success.
                return NativeActionStepResult.Failed(
                    $"Native {toolName}.DoFunction ran but the obstacle at {target.Tile} was not removed " +
                    $"(item '{obj.QualifiedItemId ?? obj.ItemId}').");
            }

            return NativeActionStepResult.Succeeded(
                isWeed ? "cleared-weeds" : isStone ? "cleared-stone" : "cleared-twig",
                staminaCost: Math.Max(0f, staminaBefore - actor.Stamina),
                itemId: obj.QualifiedItemId ?? obj.ItemId,
                itemCount: 1);
        });
    }

    // ------------------------------------------------------------------
    // Dropped / spawned item pickup (explicit tiles only)
    // ------------------------------------------------------------------
    private NativeActionStepResult PickupItems(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var loc = ResolveLocation(request.LocationId);
        if (loc is null)
            return NativeActionStepResult.Failed($"Game location '{request.LocationId}' not found.");

        if (!actor.Tile.IsAdjacentTo(target.Tile) && actor.Tile != target.Tile)
            return NativeActionStepResult.Precondition($"Actor at {actor.Tile} is not adjacent to target tile {target.Tile}.", "not-adjacent");

        if (actor.GameFarmer is null)
            return NativeActionStepResult.Failed("Companion actor has no GameFarmer instance.");

        return InvokeIsolated(actor, "pickup-items", () =>
        {
            // Native updateChunks removes a chunk only after collect returns true.
            // Bind the detached receiver to this exact ordinary debris/chunk; never
            // route archaeological discoveries through the global player path.
            int collected = 0;
            string? firstItemId = null;
            foreach (var debris in loc.debris.ToList())
            {
                if (debris is null || !NativeAnimalHarvestInventoryScope.IsOrdinaryDebris(debris)) continue;
                for (int i = debris.Chunks.Count - 1; i >= 0; i--)
                {
                    var chunk = debris.Chunks[i];
                    var pos = chunk.position.Value;
                    if ((int)(pos.X / 64) != target.Tile.X || (int)(pos.Y / 64) != target.Tile.Y) continue;
                    string id = debris.item?.QualifiedItemId ?? debris.itemId.Value;
                    int expected = debris.item?.Stack ?? 1;
                    var before = SnapshotInventoryTotals(actor.GameFarmer);
                    bool accepted;
                    using (new NativeAnimalHarvestInventoryScope(actor.GameFarmer, loc, debris, chunk))
                        accepted = debris.collect(actor.GameFarmer, chunk);
                    int gained = MeasureGainedStack(before, SnapshotInventoryTotals(actor.GameFarmer), id);
                    if (!accepted)
                        return gained == 0 && collected == 0
                            ? NativeActionStepResult.Precondition("Native inventory refused the dropped item.", "inventory-full")
                            : NativeActionStepResult.Failed($"Native pickup refused after a partial transfer: gained={gained}, earlier={collected}.");
                    // This is the native caller's cleanup, not a replacement effect.
                    debris.Chunks.RemoveAt(i);
                    if (debris.Chunks.Count == 0) loc.debris.Remove(debris);
                    if (gained != expected)
                        return NativeActionStepResult.Failed($"Native debris pickup '{id}': inventoryGained={gained}, expected={expected}.");
                    firstItemId ??= id;
                    collected += gained;
                }
            }
            if (collected > 0)
                return NativeActionStepResult.Succeeded("picked-up", itemId: firstItemId, itemCount: collected);

            // 2. Spawned object on the ground (forage / animal produce dropped by the game).
            if (loc.getObjectAtTile(target.Tile.X, target.Tile.Y) is StardewValley.Object spawned && spawned.CanBeGrabbed)
            {
                string spawnedId = spawned.QualifiedItemId ?? spawned.ItemId ?? "";
                int stack = spawned.Stack;
                var tileLoc = new xTile.Dimensions.Location(target.Tile.X, target.Tile.Y);
                var inventoryBefore = SnapshotInventoryTotals(actor.GameFarmer);
                bool groundEgg = loc is AnimalHouse && NativeAnimalHarvestInventoryScope.IsGroundEgg(spawned);
                if (groundEgg && !ReferenceEquals(actor.GameFarmer.currentLocation, loc))
                    return NativeActionStepResult.Failed("Companion is not inside the egg's animal house.");
                if (!actor.GameFarmer.couldInventoryAcceptThisItem(spawned))
                    return NativeActionStepResult.Precondition("Companion inventory cannot accept the ground item.", "inventory-full");
                using (groundEgg ? new NativeAnimalHarvestInventoryScope(actor.GameFarmer, (AnimalHouse)loc,
                           new Vector2(target.Tile.X, target.Tile.Y), spawned) : null)
                    loc.checkAction(tileLoc, Game1.viewport, actor.GameFarmer);
                int gained = MeasureGainedStack(inventoryBefore, SnapshotInventoryTotals(actor.GameFarmer), spawnedId);
                bool gone = loc.getObjectAtTile(target.Tile.X, target.Tile.Y) is null;
                if (!gone || gained != stack)
                    return NativeActionStepResult.Failed(
                        $"Native ground pickup '{spawnedId}' at {target.Tile}: removed={gone}, inventoryGained={gained}, expected={stack}.");
                return NativeActionStepResult.Succeeded("picked-up", itemId: spawnedId, itemCount: gained);
            }

            return NativeActionStepResult.Precondition($"Tile {target.Tile} has no dropped or spawned item.", "no-item");
        });
    }

    // ------------------------------------------------------------------
    // Machines
    // ------------------------------------------------------------------
    private NativeActionStepResult InsertMachine(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var loc = ResolveLocation(request.LocationId);
        if (loc is null)
            return NativeActionStepResult.Failed($"Game location '{request.LocationId}' not found.");

        if (!actor.Tile.IsAdjacentTo(target.Tile) && actor.Tile != target.Tile)
            return NativeActionStepResult.Precondition($"Actor at {actor.Tile} is not adjacent to machine tile {target.Tile}.", "not-adjacent");

        string? itemId = request.ItemId;
        if (string.IsNullOrWhiteSpace(itemId))
            return NativeActionStepResult.Failed("itemId is required for insert-machine.");

        int count = Math.Max(1, request.ItemCount);

        if (loc.getObjectAtTile(target.Tile.X, target.Tile.Y) is not StardewValley.Object machine)
            return NativeActionStepResult.Precondition($"Tile {target.Tile} has no object.", "no-machine");

        StardewValley.GameData.Machines.MachineData? machineData;
        try { machineData = machine.GetMachineData(); }
        catch { machineData = null; }
        if (machineData is null)
            return NativeActionStepResult.Precondition($"Tile {target.Tile} is not a machine.", "unsupported-machine");

        // Native rule (verified in the assembly): performObjectDropInAction refuses a load
        // while the machine holds an output or is still processing.
        bool busy = machine.MinutesUntilReady > 0 || machine.heldObject?.Value is not null;
        if (busy)
            return NativeActionStepResult.Precondition(
                $"Machine '{machine.QualifiedItemId}' at {target.Tile} is already processing or full " +
                $"(minutesUntilReady={machine.MinutesUntilReady}).",
                "machine-busy");

        if (actor.GetItemCount(itemId) < count)
            return NativeActionStepResult.Precondition(
                $"Companion inventory has {actor.GetItemCount(itemId)} x '{itemId}', needs {count}.",
                "missing-item",
                playerActionRequired: true);

        if (actor.GameFarmer is null)
            return NativeActionStepResult.Failed("Companion actor has no GameFarmer instance.");

        return InvokeIsolated(actor, "insert-machine", () =>
        {
            // Native consumption contract (verified against Object.performObjectDropInAction
            // -> Object.PlaceInMachine -> Object.ConsumeInventoryItem in the real assembly):
            // with a non-null "who", the native code consumes the exact Item instance handed
            // to it (ConsumeStack + inventory.RemoveButKeepEmptySlot) and also reduces the
            // machine's AdditionalConsumedItems (e.g. Furnace coal) from who.Items. We
            // therefore pass the companion's real inventory stack and never pre-extract it:
            // extracting first would double-charge the primary input, and passing a single
            // extracted stack would silently drop the rest when count > 1.
            Item? input = FindInventoryItem(actor.GameFarmer, itemId);
            if (input is null || input.Stack < count)
                return NativeActionStepResult.Precondition(
                    $"Companion inventory lost '{itemId}' before the native load.",
                    "missing-item",
                    playerActionRequired: true);

            // Probe first: the native game decides applicability and does not mutate in probe mode.
            bool accepted;
            try
            {
                accepted = machine.performObjectDropInAction(input, probe: true, who: actor.GameFarmer, returnFalseIfItemConsumed: true);
            }
            catch (Exception ex)
            {
                return NativeActionStepResult.Failed($"Machine probe threw: {ex.Message}");
            }

            if (!accepted)
                return NativeActionStepResult.Precondition(
                    $"Machine '{machine.QualifiedItemId}' at {target.Tile} rejected input '{input.QualifiedItemId}'.",
                    "machine-rejected-input");

            int inputBefore = actor.GetItemCount(itemId);
            int heldBefore = machine.heldObject?.Value is null ? 0 : 1;
            int minutesBefore = machine.MinutesUntilReady;

            bool performed;
            try
            {
                performed = machine.performObjectDropInAction(input, probe: false, who: actor.GameFarmer, returnFalseIfItemConsumed: false);
            }
            catch (Exception ex)
            {
                // The native path consumes the input (and additional items) only after the
                // machine output rule has already succeeded, so a throw before that point
                // leaves the inventory untouched. Report the real state; never claim success.
                return NativeActionStepResult.Failed($"Machine drop-in threw: {ex.Message}");
            }

            int inputAfter = actor.GetItemCount(itemId);
            int consumed = inputBefore - inputAfter;
            Item? held = machine.heldObject?.Value;
            bool started = held is not null
                && (machine.MinutesUntilReady > 0 || machine.readyForHarvest?.Value == true
                    || heldBefore == 0 && machine.MinutesUntilReady != minutesBefore);

            // Success requires all three real native facts at once: the native call reported
            // the load, the machine state actually changed to processing/ready, and the input
            // was actually consumed from the inventory. performed=true with no state change,
            // or a state change with no consumption, is never reported as success.
            if (!performed || !started || consumed <= 0)
                return NativeActionStepResult.Failed(
                    $"Machine '{machine.QualifiedItemId}' did not complete a real native load " +
                    $"(performed={performed}, started={started}, consumed={consumed}, " +
                    $"heldObject={(held is null ? "null" : held.QualifiedItemId)}, " +
                    $"minutesUntilReady={machine.MinutesUntilReady}).");

            return NativeActionStepResult.Succeeded("inserted", itemId: input.QualifiedItemId, itemCount: consumed);
        });
    }

    private NativeActionStepResult CollectMachine(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var loc = ResolveLocation(request.LocationId);
        if (loc is null)
            return NativeActionStepResult.Failed($"Game location '{request.LocationId}' not found.");

        if (!actor.Tile.IsAdjacentTo(target.Tile) && actor.Tile != target.Tile)
            return NativeActionStepResult.Precondition($"Actor at {actor.Tile} is not adjacent to machine tile {target.Tile}.", "not-adjacent");

        if (loc.getObjectAtTile(target.Tile.X, target.Tile.Y) is not StardewValley.Object machine)
            return NativeActionStepResult.Precondition($"Tile {target.Tile} has no object.", "no-machine");

        StardewValley.GameData.Machines.MachineData? machineData;
        try { machineData = machine.GetMachineData(); }
        catch { machineData = null; }
        if (machineData is null)
            return NativeActionStepResult.Precondition($"Tile {target.Tile} is not a machine.", "unsupported-machine");

        if (machine.readyForHarvest?.Value != true || machine.heldObject?.Value is null)
            return NativeActionStepResult.Precondition(
                $"Machine at {target.Tile} has no ready output (minutesUntilReady={machine.MinutesUntilReady}).",
                "not-ready");

        if (actor.GameFarmer is null)
            return NativeActionStepResult.Failed("Companion actor has no GameFarmer instance.");

        Item? held = machine.heldObject?.Value;
        if (held is null)
            return NativeActionStepResult.Precondition($"Machine at {target.Tile} has no ready output.", "not-ready");

        bool canAccept = false;
        try
        {
            canAccept = actor.GameFarmer.couldInventoryAcceptThisItem(held);
        }
        catch (Exception ex)
        {
            _monitor.Log($"couldInventoryAcceptThisItem check failed: {ex.Message}", LogLevel.Warn);
            canAccept = actor.FreeInventorySlots > 0;
        }

        if (!canAccept)
        {
            return NativeActionStepResult.Precondition(
                $"Companion inventory is full and cannot accept '{held.QualifiedItemId ?? held.ItemId}'.",
                "inventory-full",
                playerActionRequired: true);
        }

        return InvokeIsolated(actor, "collect-machine", () =>
        {
            var companion = actor.GameFarmer;
            string? outputId = held.QualifiedItemId ?? held.ItemId;
            int expectedStack = held.Stack;

            var inventoryBefore = SnapshotInventoryTotals(companion);

            // Execute native checkAction with the companion as 'who' (non-local farmer branch).
            // This runs the full native machine lifecycle (clearing machine state, tapper updates,
            // auto-load attempts, sound) without altering Game1.player identity or reference.
            var tileLoc = new xTile.Dimensions.Location(target.Tile.X, target.Tile.Y);
            loc.checkAction(tileLoc, Game1.viewport, companion);

            bool cleared = machine.readyForHarvest?.Value != true || machine.heldObject?.Value is null;
            if (!cleared)
            {
                // Native action did not clear the machine: do NOT transfer item, report honest failure.
                return NativeActionStepResult.Failed(
                    $"Machine at {target.Tile} still holds its output after the native check action; no items transferred.");
            }

            // Check if native code already delivered the item (e.g. if who was evaluated as local).
            var inventoryAfterNative = SnapshotInventoryTotals(companion);
            int nativeGained = MeasureGainedStack(inventoryBefore, inventoryAfterNative, outputId);

            if (nativeGained <= 0)
            {
                // Non-local branch completed machine lifecycle and released heldObject without adding it to who.
                // Transfer the original real held item reference into companion inventory via native inventory interface.
                bool added = actor.TryAddItemToInventory(held);
                if (!added)
                {
                    // Attempt rollback to preserve machine state
                    bool restored = false;
                    try
                    {
                        if (held is StardewValley.Object heldObj && machine.heldObject != null)
                        {
                            machine.heldObject.Value = heldObj;
                            if (machine.readyForHarvest != null)
                                machine.readyForHarvest.Value = true;
                            restored = true;
                        }
                    }
                    catch { restored = false; }

                    if (restored)
                    {
                        return NativeActionStepResult.Failed(
                            $"Companion inventory could not accept '{outputId}'; machine output was restored.",
                            playerActionRequired: true);
                    }
                    return NativeActionStepResult.Failed(
                        $"Machine at {target.Tile} was cleared but companion inventory transfer failed (state: unknown/partial).",
                        playerActionRequired: true);
                }
            }

            var inventoryFinal = SnapshotInventoryTotals(companion);
            int gained = MeasureGainedStack(inventoryBefore, inventoryFinal, outputId);
            if (gained <= 0)
            {
                return NativeActionStepResult.Failed(
                    $"Machine at {target.Tile} was cleared but companion inventory did not gain '{outputId}' (expected {expectedStack}, gained 0).");
            }

            return NativeActionStepResult.Succeeded("collected", itemId: outputId, itemCount: gained);
        });
    }

    // ------------------------------------------------------------------
    // Livestock
    // ------------------------------------------------------------------
    private NativeActionStepResult PetAnimal(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var animal = FindAnimal(target.TargetId, request.LocationId, target.Tile);
        if (animal is null)
            return NativeActionStepResult.Precondition(
                $"Animal '{target.TargetId}' was not found on '{request.LocationId}' near {target.Tile}.",
                "animal-not-found");

        if (Distance(actor.Tile, new TileCoordinate((int)animal.Tile.X, (int)animal.Tile.Y)) > 2)
            return NativeActionStepResult.Precondition(
                $"Animal '{animal.Name}' moved to {animal.Tile}; the companion is not close enough.",
                "animal-moved");

        if (animal.wasPet?.Value == true)
            return NativeActionStepResult.Precondition($"Animal '{animal.Name}' was already petted today.", "already-petted");

        if (actor.GameFarmer is null)
            return NativeActionStepResult.Failed("Companion actor has no GameFarmer instance.");

        return InvokeIsolated(actor, "pet-animal", () =>
        {
            int happinessBefore = animal.happiness?.Value ?? 0;
            animal.pet(actor.GameFarmer, is_auto_pet: false);
            bool petted = animal.wasPet?.Value == true || (animal.happiness?.Value ?? 0) > happinessBefore;
            if (!petted)
                return NativeActionStepResult.Failed($"FarmAnimal.pet did not register for '{animal.Name}'.");
            return NativeActionStepResult.Succeeded("petted", itemId: animal.Name);
        });
    }

    /// <summary>
    /// Whole-building feeding performed as the real player-performable hay flow, in
    /// internal multi-step form:
    ///
    /// 1. withdraw one real hay object from any silo with the game's own native
    ///    <c>GameLocation.GetHayFromAnySilo</c> (it decrements the silo and returns a real
    ///    hay item — the same primitive <c>AnimalHouse.feedAllAnimals</c> uses);
    /// 2. place it on an empty native <c>Trough/Back</c> tile through the building's own
    ///    <c>AnimalHouse.checkAction</c>, holding hay exactly like the player does
    ///    (that native branch adds the hay object and calls reduceActiveItemByOne).
    ///
    /// Success is proven by real counters only: trough hay objects increased and the
    /// silo+inventory decrease exactly matches the trough increase. Animal fullness is
    /// never used as evidence and no day-end <c>feedAllAnimals</c> state is touched, so
    /// nothing is fabricated when the building or silo is empty.
    /// </summary>
    private NativeActionStepResult FeedAnimals(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        string indoorsName = target.TargetId ?? request.LocationId;
        var loc = ResolveLocation(indoorsName);
        if (loc is not AnimalHouse house)
            return NativeActionStepResult.Precondition(
                $"Location '{indoorsName}' is not an animal building interior.",
                "not-animal-house");

        string companionLocation = !string.IsNullOrWhiteSpace(actor.GameFarmer?.currentLocation?.NameOrUniqueName)
            ? actor.GameFarmer.currentLocation.NameOrUniqueName
            : (!string.IsNullOrWhiteSpace(actor.GameFarmer?.currentLocation?.Name)
                ? actor.GameFarmer.currentLocation.Name
                : actor.LocationName);

        if (!string.Equals(companionLocation, indoorsName, StringComparison.OrdinalIgnoreCase))
            return NativeActionStepResult.Precondition(
                $"Companion is on '{companionLocation}', not inside '{indoorsName}'.",
                "wrong-map");

        if (actor.GameFarmer is null)
            return NativeActionStepResult.Failed("Companion actor has no GameFarmer instance.");

        return InvokeIsolated(actor, "feed-animals", () =>
        {
            List<FarmAnimal> animals;
            try { animals = house.getAllFarmAnimals(); }
            catch { animals = new List<FarmAnimal>(); }
            if (animals.Count == 0)
                return NativeActionStepResult.Precondition($"'{indoorsName}' has no animals.", "no-animals");

            var troughTiles = FindTroughTiles(house);
            if (troughTiles.Count == 0)
                return NativeActionStepResult.Precondition(
                    $"'{indoorsName}' has no native Trough/Back tiles to fill.",
                    "no-trough");

            int troughBefore = CountTroughHay(house, troughTiles);
            int siloBefore = SiloHayTotal(house);
            int invBefore = actor.GetItemCount(HayItemId);

            int animalLimit = 0;
            try { animalLimit = house.animalLimit?.Value ?? 0; } catch { animalLimit = 0; }
            int fillLimit = animalLimit > 0 ? Math.Min(animalLimit, troughTiles.Count) : troughTiles.Count;

            int filled = 0;
            foreach (var tile in troughTiles)
            {
                if (filled >= fillLimit)
                    break;
                bool occupied;
                try { occupied = house.objects.ContainsKey(tile); }
                catch { occupied = true; }
                if (occupied)
                    continue;

                if (actor.GetItemCount(HayItemId) <= 0 && !WithdrawHayFromSilo(house, actor))
                    break;

                if (PlaceHayOnTrough(house, actor, tile))
                    filled++;
            }

            int troughAfter = CountTroughHay(house, troughTiles);
            int siloAfter = SiloHayTotal(house);
            int invAfter = actor.GetItemCount(HayItemId);

            int added = troughAfter - troughBefore;
            int siloSpent = siloBefore - siloAfter;
            int invSpent = invBefore - invAfter; // negative when withdrawn hay is still carried
            bool conserved = siloSpent + invSpent == added;

            if (added <= 0)
            {
                if (troughBefore >= fillLimit || (troughTiles.Count > 0 && troughBefore >= troughTiles.Count))
                    return NativeActionStepResult.Precondition(
                        $"'{indoorsName}' troughs are already full ({troughBefore}/{fillLimit}).",
                        "already-full");

                return NativeActionStepResult.Precondition(
                    $"'{indoorsName}' feeding placed no trough hay " +
                    $"(trough {troughBefore}; silo {siloBefore}->{siloAfter}; inventory {invBefore}->{invAfter}).",
                    "no-hay",
                    playerActionRequired: true);
            }

            if (siloSpent <= 0 || !conserved)
                return NativeActionStepResult.Failed(
                    $"'{indoorsName}' feeding did not conserve hay " +
                    $"(trough +{added}, silo {siloBefore}->{siloAfter}, inventory {invBefore}->{invAfter}).");

            return NativeActionStepResult.Succeeded("fed", itemId: indoorsName, itemCount: added);
        });
    }

    private const string HayItemId = "(O)178";

    /// <summary>Native trough tiles: the <c>Trough/Back</c> map property the game uses.</summary>
    private static List<Vector2> FindTroughTiles(AnimalHouse house)
    {
        var tiles = new List<Vector2>();
        try
        {
            var layers = house.Map?.Layers;
            if (layers is null || layers.Count == 0)
                return tiles;
            var layer = layers[0];
            for (int y = 0; y < layer.LayerHeight; y++)
            {
                for (int x = 0; x < layer.LayerWidth; x++)
                {
                    bool isTrough;
                    try { isTrough = house.doesTileHaveProperty(x, y, "Trough", "Back", false) is not null; }
                    catch { isTrough = false; }
                    if (isTrough)
                        tiles.Add(new Vector2(x, y));
                }
            }
        }
        catch (Exception)
        {
            // Fall through with whatever was collected; callers treat an empty list as no-trough.
        }
        return tiles;
    }

    private static bool IsHay(StardewValley.Object? obj) =>
        obj is not null && FarmerMechanicsActor.ItemIdMatches(obj.QualifiedItemId ?? "", HayItemId);

    private static int CountTroughHay(AnimalHouse house, List<Vector2> troughTiles)
    {
        int count = 0;
        foreach (var tile in troughTiles)
        {
            try
            {
                if (house.objects.TryGetValue(tile, out var obj) && IsHay(obj))
                    count++;
            }
            catch { }
        }
        return count;
    }

    /// <summary>
    /// Total hay stored in real silos (the locations <c>GetHayFromAnySilo</c> searches).
    /// AnimalHouse.piecesOfHay is deliberately excluded: in 1.6 the trough supply is the
    /// hay objects, not that legacy field, so counting it here would fabricate storage.
    /// </summary>
    private static int SiloHayTotal(AnimalHouse house)
    {
        int total = 0;
        var seen = new HashSet<GameLocation>();
        void Add(GameLocation? candidate)
        {
            if (candidate is null || candidate is AnimalHouse || !seen.Add(candidate))
                return;
            try { total += candidate.piecesOfHay?.Value ?? 0; }
            catch { }
        }

        try { Add(house.GetRootLocation()); } catch { }
        try { Add(Game1.getFarm()); } catch { }
        try
        {
            foreach (var location in Game1.locations)
                Add(location);
        }
        catch { }
        return total;
    }

    /// <summary>
    /// Withdraws one real hay object from a silo through the game's own native
    /// <c>GetHayFromAnySilo</c> (it decrements the silo before returning the item) and puts
    /// it in the companion's inventory. If the companion cannot hold it, the hay is put
    /// back into a silo so the world total is never silently drained.
    /// </summary>
    private bool WithdrawHayFromSilo(AnimalHouse house, IFarmerActor actor)
    {
        try
        {
            GameLocation root;
            try { root = house.GetRootLocation() ?? house; }
            catch { root = house; }

            var hay = GameLocation.GetHayFromAnySilo(root);
            if (hay is null)
                return false;

            if (actor.TryAddItemToInventory(hay))
                return true;

            try { GameLocation.StoreHayInAnySilo(1, root); }
            catch (Exception ex) { _monitor.Log($"Could not return unheld hay to a silo: {ex.Message}", LogLevel.Warn); }
            return false;
        }
        catch (Exception ex)
        {
            _monitor.Log($"Native silo hay withdrawal failed: {ex.Message}", LogLevel.Warn);
            return false;
        }
    }

    /// <summary>
    /// Places one hay from the companion's inventory on an empty trough tile using the
    /// building's own native <c>checkAction</c> — the exact branch a player triggers by
    /// holding hay and activating the trough. Returns true only when a real hay object
    /// appeared on the tile.
    /// </summary>
    private bool PlaceHayOnTrough(AnimalHouse house, IFarmerActor actor, Vector2 tile)
    {
        var farmer = actor.GameFarmer;
        if (farmer is null)
            return false;

        int haySlot = FindItemSlot(farmer, HayItemId);
        if (haySlot < 0)
            return false;

        int originalSlot = farmer.CurrentToolIndex;
        try
        {
            // Hold the hay exactly like the player's selected slot, then let the building's
            // native check action place it and reduce the held stack by one.
            farmer.CurrentToolIndex = haySlot;
            house.checkAction(new xTile.Dimensions.Location((int)tile.X, (int)tile.Y), Game1.viewport, farmer);
        }
        catch (Exception ex)
        {
            _monitor.Log($"Native trough fill failed at {tile}: {ex.Message}", LogLevel.Warn);
        }
        finally
        {
            try { farmer.CurrentToolIndex = originalSlot; } catch { }
        }

        try { return house.objects.TryGetValue(tile, out var placed) && IsHay(placed); }
        catch { return false; }
    }

    private static int FindItemSlot(Farmer farmer, string itemId)
    {
        for (int i = 0; i < farmer.Items.Count; i++)
        {
            var item = farmer.Items[i];
            if (item is null || item.Stack <= 0)
                continue;
            if (FarmerMechanicsActor.ItemIdMatches(item.ItemId ?? "", itemId)
                || FarmerMechanicsActor.ItemIdMatches(item.QualifiedItemId ?? "", itemId))
                return i;
        }
        return -1;
    }

    /// <summary>
    /// The companion's real inventory stack matching an item id (largest stack wins so the
    /// native ConsumeStack always has enough units). Returns null when absent.
    /// </summary>
    private static Item? FindInventoryItem(Farmer farmer, string itemId)
    {
        Item? best = null;
        foreach (var item in farmer.Items)
        {
            if (item is null || item.Stack <= 0)
                continue;
            if (!FarmerMechanicsActor.ItemIdMatches(item.ItemId ?? "", itemId)
                && !FarmerMechanicsActor.ItemIdMatches(item.QualifiedItemId ?? "", itemId))
                continue;
            if (best is null || item.Stack > best.Stack)
                best = item;
        }
        return best;
    }

    private static Dictionary<(string ItemId, int Quality), int> SnapshotInventoryTotals(Farmer farmer)
    {
        var totals = new Dictionary<(string, int), int>();
        foreach (var item in farmer.Items)
        {
            if (item is null) continue;
            var key = (item.QualifiedItemId ?? item.ItemId ?? item.Name, item.Quality);
            totals.TryGetValue(key, out int current);
            totals[key] = current + item.Stack;
        }
        return totals;
    }

    private static int MeasureGainedStack(
        Dictionary<(string ItemId, int Quality), int> before,
        Dictionary<(string ItemId, int Quality), int> after,
        string? expectedItemId)
    {
        int totalGained = 0;
        foreach (var (key, afterStack) in after)
        {
            before.TryGetValue(key, out int beforeStack);
            int delta = afterStack - beforeStack;
            if (delta <= 0)
                continue;

            if (expectedItemId is null ||
                FarmerMechanicsActor.ItemIdMatches(key.ItemId, expectedItemId))
            {
                totalGained += delta;
            }
        }
        return totalGained;
    }

    private NativeActionStepResult ToggleAnimalDoor(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var loc = ResolveLocation(request.LocationId);
        if (loc is null)
            return NativeActionStepResult.Failed($"Game location '{request.LocationId}' not found.");

        Building? building = null;
        try { building = loc.getBuildingAt(new Vector2(target.Tile.X, target.Tile.Y)); }
        catch { building = null; }

        if (building is null)
        {
            // The target may be the door tile rather than the building origin.
            foreach (var candidate in loc.buildings)
            {
                var door = DoorTile(candidate);
                if (door == target.Tile)
                {
                    building = candidate;
                    break;
                }
            }
        }

        if (building is null)
            return NativeActionStepResult.Precondition($"No building at {target.Tile} on '{request.LocationId}'.", "no-building");

        if (building.GetIndoors() is not AnimalHouse)
            return NativeActionStepResult.Precondition($"Building '{building.buildingType?.Value}' has no animal interior.", "no-animal-house");

        // Match the native Building.doAction animal-door branch without injecting
        // player mouse input or entering its player-only human-door warp branch.
        var doorTile = DoorTile(building);
        var data = building.GetData();
        if (data is null || !IsNativeAnimalDoorTile(building.getRectForAnimalDoor(data), doorTile))
            return NativeActionStepResult.Precondition("Building has no native animal door at this tile.", "no-animal-door");
        if (building.daysOfConstructionLeft.Value > 0)
            return NativeActionStepResult.Precondition("Building is still under construction.", "building-under-construction");
        if (Distance(actor.Tile, doorTile) > 1)
            return NativeActionStepResult.Precondition(
                $"Actor at {actor.Tile} is not adjacent to the animal door at {doorTile}.",
                "not-adjacent-to-door");

        if (actor.GameFarmer is null)
            return NativeActionStepResult.Failed("Companion actor has no GameFarmer instance.");

        if (!ReferenceEquals(actor.GameFarmer.currentLocation, loc))
            return NativeActionStepResult.Precondition("Companion is not on the building's map.", "wrong-map");
        if (actor.GameFarmer.isRidingHorse())
            return NativeActionStepResult.Precondition("Native building interaction is unavailable while mounted.", "mounted");

        return InvokeIsolated(actor, "toggle-animal-door", () =>
        {
            bool before = building.animalDoorOpen?.Value == true;
            building.ToggleAnimalDoor(actor.GameFarmer);
            bool after = building.animalDoorOpen?.Value == true;
            if (after == before)
                return NativeActionStepResult.Failed(
                    $"Building.ToggleAnimalDoor did not change the door state of '{building.buildingType?.Value}'.");
            return NativeActionStepResult.Succeeded(after ? "door-opened" : "door-closed", itemId: building.buildingType?.Value);
        });
    }

    private NativeActionStepResult CollectAnimalProduce(IFarmerActor actor, NativeActionRequest request, NativeActionTarget target)
    {
        var animal = FindAnimal(target.TargetId, request.LocationId, target.Tile);
        if (animal is null)
            return NativeActionStepResult.Precondition(
                $"Animal '{target.TargetId}' was not found on '{request.LocationId}' near {target.Tile}.",
                "animal-not-found");

        string produce = animal.currentProduce?.Value ?? "";
        if (string.IsNullOrWhiteSpace(produce))
            return NativeActionStepResult.Precondition($"Animal '{animal.Name}' has no produce ready.", "no-produce");

        if (Distance(actor.Tile, new TileCoordinate((int)animal.Tile.X, (int)animal.Tile.Y)) > 2)
            return NativeActionStepResult.Precondition(
                $"Animal '{animal.Name}' moved to {animal.Tile}; the companion is not close enough.",
                "animal-moved");

        string harvestType = "";
        try { harvestType = animal.GetHarvestType()?.ToString() ?? ""; }
        catch { harvestType = ""; }

        if (string.Equals(harvestType, "HarvestWithTool", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(harvestType, "DigUp", StringComparison.OrdinalIgnoreCase))
        {
            string? requiredTool = null;
            try { requiredTool = animal.GetAnimalData()?.HarvestTool; }
            catch { requiredTool = null; }

            // The player milks/shears with the real tool. Resolve the companion's own
            // tool by the native HarvestTool identity; when the tool type cannot be
            // mapped, report it as an actionable missing-tool instead of a fake success.
            Tool? tool = ResolveHarvestTool(actor, requiredTool, out string toolLabel);

            if (string.Equals(harvestType, "DigUp", StringComparison.OrdinalIgnoreCase) || tool is null)
            {
                return NativeActionStepResult.Precondition(
                    $"Produce '{produce}' from '{animal.Name}' needs the native tool " +
                    $"'{requiredTool ?? "unknown"}', which the companion does not carry.",
                    $"missing-tool:{requiredTool ?? toolLabel}",
                    playerActionRequired: true);
            }

            if (Distance(actor.Tile, new TileCoordinate((int)animal.Tile.X, (int)animal.Tile.Y)) > 2)
                return NativeActionStepResult.Precondition(
                    $"Animal '{animal.Name}' moved to {animal.Tile}; the companion is not close enough.",
                    "animal-moved");

            if (actor.GameFarmer is null)
                return NativeActionStepResult.Failed("Companion actor has no GameFarmer instance.");

            return InvokeIsolated(actor, "collect-animal-produce", () =>
            {
                bool canHarvest;
                try { canHarvest = animal.CanGetProduceWithTool(tool); }
                catch (Exception ex)
                {
                    return NativeActionStepResult.Failed($"CanGetProduceWithTool threw: {ex.Message}");
                }
                if (!canHarvest)
                    return NativeActionStepResult.Precondition(
                        $"Native rules refuse harvesting '{produce}' from '{animal.Name}' with {toolLabel}.",
                        "produce-not-ready-for-tool");

                var animalLocation = ResolveLocation(request.LocationId);
                if (animalLocation is null)
                    return NativeActionStepResult.Failed($"Game location '{request.LocationId}' not found.");

                var inventoryBefore = SnapshotInventoryTotals(actor.GameFarmer);
                var farmer = actor.GameFarmer;
                int previousToolIndex = farmer.CurrentToolIndex;

                try
                {
                    if (!ReferenceEquals(farmer.currentLocation, animalLocation)
                        || !ReferenceEquals(animal.currentLocation, animalLocation))
                        return NativeActionStepResult.Failed("Native harvest actor/animal location context does not match.");
                    int toolIndex = farmer.Items.IndexOf(tool);
                    if (toolIndex < 0)
                        return NativeActionStepResult.Failed("Native harvest tool is no longer in companion inventory.");
                    farmer.CurrentToolIndex = toolIndex;
                    // Drain an old finish event before binding, then verify the exact
                    // animal field used by DoFunction. Never silently use a null target.
                    tool.tickUpdate(new GameTime(), farmer);
                    BindToolToAnimal(tool, animal);
                    using (var inventoryScope = new NativeAnimalHarvestInventoryScope(farmer, tool))
                    {
                        tool.DoFunction(animalLocation, (int)animal.Tile.X * 64 + 32, (int)animal.Tile.Y * 64 + 32, 1, farmer);
                    }
                }
                catch (Exception ex)
                {
                    return NativeActionStepResult.Failed($"{toolLabel} native harvest context failed: {ex.Message}");
                }
                finally
                {
                    try { tool.tickUpdate(new GameTime(), farmer); }
                    finally { farmer.CurrentToolIndex = previousToolIndex; }
                }

                bool produceCleared;
                try { produceCleared = string.IsNullOrWhiteSpace(animal.currentProduce?.Value); }
                catch { produceCleared = false; }

                int gained = MeasureGainedStack(inventoryBefore, SnapshotInventoryTotals(actor.GameFarmer), produce);
                if (!produceCleared || gained <= 0)
                    return NativeActionStepResult.Failed(
                        $"{toolLabel} harvest was not verified for '{animal.Name}': produceCleared={produceCleared}, inventoryGained={gained}.");

                return NativeActionStepResult.Succeeded("collected", itemId: produce, itemCount: gained);
            });
        }

        // DropOvernight produce is a spawned object the game places in the building;
        // it is collected through the same native check action the player uses.
        var animalLoc = ResolveLocation(request.LocationId);
        if (animalLoc is null)
            return NativeActionStepResult.Failed($"Game location '{request.LocationId}' not found.");

        StardewValley.Object? produceObject = null;
        TileCoordinate produceTile = default;
        try
        {
            foreach (var pair in animalLoc.objects.Pairs)
            {
                if (pair.Value is StardewValley.Object candidate && candidate.CanBeGrabbed)
                {
                    produceObject = candidate;
                    produceTile = new TileCoordinate((int)pair.Key.X, (int)pair.Key.Y);
                    break;
                }
            }
        }
        catch { produceObject = null; }

        if (produceObject is null)
            return NativeActionStepResult.Precondition(
                $"No collectable produce object was found on '{request.LocationId}'.",
                "produce-not-on-ground");

        if (Distance(actor.Tile, produceTile) > 1)
            return NativeActionStepResult.Precondition(
                $"Produce at {produceTile} is not adjacent to the companion at {actor.Tile}.",
                "produce-out-of-reach");

        if (actor.GameFarmer is null)
            return NativeActionStepResult.Failed("Companion actor has no GameFarmer instance.");

        return InvokeIsolated(actor, "collect-animal-produce", () =>
        {
            string id = produceObject.QualifiedItemId ?? produceObject.ItemId ?? produce;
            int stack = produceObject.Stack;
            var tileLoc = new xTile.Dimensions.Location(produceTile.X, produceTile.Y);
            animalLoc.checkAction(tileLoc, Game1.viewport, actor.GameFarmer);
            bool gone = animalLoc.getObjectAtTile(produceTile.X, produceTile.Y) is null;
            if (!gone)
                return NativeActionStepResult.Failed($"Produce at {produceTile} was not collected by the native check action.");
            return NativeActionStepResult.Succeeded("collected", itemId: id, itemCount: Math.Max(1, stack));
        });
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    /// <summary>
    /// Runs a native call with the human-player isolation invariants the existing
    /// tool adapters enforce: <c>Game1.player</c> identity and player resources
    /// must be unchanged.
    /// </summary>
    private NativeActionStepResult InvokeIsolated(IFarmerActor actor, string action, Func<NativeActionStepResult> body)
    {
        var playerBefore = _observer.GetPlayerSnapshot();
        var originalPlayerRef = Game1.player;
        try
        {
            return body();
        }
        finally
        {
            if (!ReferenceEquals(originalPlayerRef, Game1.player))
                throw new InvalidOperationException($"CRITICAL SAFETY VIOLATION: Game1.player reference was modified during {action}!");

            var playerAfter = _observer.GetPlayerSnapshot();
            if (!playerBefore.EqualsPlayerResources(playerAfter))
                throw new InvalidOperationException(
                    $"CRITICAL SAFETY VIOLATION: Human player resources changed during {action}! Before: {playerBefore}, After: {playerAfter}");
        }
    }

    private static GameLocation? ResolveLocation(string locationName)
    {
        if (string.IsNullOrWhiteSpace(locationName)) return Game1.currentLocation;
        try
        {
            var loc = Game1.getLocationFromName(locationName);
            if (loc != null) return loc;
        }
        catch { }

        try
        {
            if (Game1.currentLocation != null)
            {
                string? currName = !string.IsNullOrWhiteSpace(Game1.currentLocation.NameOrUniqueName)
                    ? Game1.currentLocation.NameOrUniqueName
                    : Game1.currentLocation.Name;
                if (string.Equals(currName, locationName, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(Game1.currentLocation.Name, locationName, StringComparison.OrdinalIgnoreCase))
                {
                    return Game1.currentLocation;
                }
            }
        }
        catch { }

        try
        {
            foreach (var l in Game1.locations)
            {
                if (l == null) continue;
                if (l.buildings != null)
                {
                    foreach (var b in l.buildings)
                    {
                        if (b == null) continue;
                        var indoors = b.GetIndoors();
                        if (indoors != null)
                        {
                            string? inName = b.GetIndoorsName() ?? (!string.IsNullOrWhiteSpace(indoors.NameOrUniqueName) ? indoors.NameOrUniqueName : indoors.Name);
                            if (string.Equals(inName, locationName, StringComparison.OrdinalIgnoreCase) ||
                                string.Equals(indoors.Name, locationName, StringComparison.OrdinalIgnoreCase))
                            {
                                return indoors;
                            }
                        }
                    }
                }
            }
        }
        catch { }

        return Game1.currentLocation;
    }

    /// <summary>
    /// Resolves the companion's own tool for a native <c>HarvestTool</c> identity
    /// (native "Milk Pail" name, legacy "MilkPail", or "Shears").
    /// Returns null when the companion carries no such tool.
    /// </summary>
    private static Tool? ResolveHarvestTool(IFarmerActor actor, string? harvestTool, out string toolLabel)
    {
        toolLabel = string.IsNullOrWhiteSpace(harvestTool) ? "unknown" : harvestTool!;
        if (string.IsNullOrWhiteSpace(harvestTool))
            return null;

        string wanted = harvestTool!.Trim();
        if (wanted.Equals("Milk Pail", StringComparison.OrdinalIgnoreCase) ||
            wanted.Equals("MilkPail", StringComparison.OrdinalIgnoreCase))
            return actor.FindTool<StardewValley.Tools.MilkPail>();
        if (wanted.Equals("Shears", StringComparison.OrdinalIgnoreCase))
            return actor.FindTool<StardewValley.Tools.Shears>();

        // Unknown/other tool identity: never guess. The caller reports missing-tool.
        return null;
    }

    /// <summary>
    /// Binds a harvest tool to the animal it is used on, mirroring the native
    /// <c>MilkPail.animal</c> / <c>Shears.animal</c> field the game sets before
    /// <c>DoFunction</c>. Reflection keeps this independent of tool-class internals
    /// while still driving the genuine native code path.
    /// </summary>
    private static void BindToolToAnimal(Tool tool, FarmAnimal animal)
    {
        var nativeType = tool is MilkPail ? typeof(MilkPail) : tool is Shears ? typeof(Shears) : null;
        var field = nativeType?.GetField("animal", System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
            ?? throw new InvalidOperationException("Native harvest animal field is unavailable.");
        field.SetValue(tool, animal);
        if (!ReferenceEquals(field.GetValue(tool), animal))
            throw new InvalidOperationException("Native harvest animal binding failed verification.");
    }

    internal static bool IsNativeAnimalDoorTile(Rectangle pixelBounds, TileCoordinate tile)
    {
        // Use the game's integer conversion, including multi-tile barn doors.
        var tiles = new Rectangle(pixelBounds.X / 64, pixelBounds.Y / 64,
            pixelBounds.Width / 64, pixelBounds.Height / 64);
        return tiles != Rectangle.Empty && tiles.Contains(tile.X, tile.Y);
    }

    private static TileCoordinate DoorTile(Building building)
    {
        int originX = building.tileX?.Value ?? 0;
        int originY = building.tileY?.Value ?? 0;
        var door = building.animalDoor;
        return new TileCoordinate(originX + (door?.X ?? 0), originY + (door?.Y ?? 0));
    }

    private static int Distance(TileCoordinate a, TileCoordinate b) =>
        Math.Max(Math.Abs(a.X - b.X), Math.Abs(a.Y - b.Y));

    private FarmAnimal? FindAnimal(string? name, string locationName, TileCoordinate near)
    {
        if (string.IsNullOrWhiteSpace(name))
            return null;

        try
        {
            var targetLoc = ResolveLocation(locationName);
            if (targetLoc != null)
            {
                var locAnimals = targetLoc.getAllFarmAnimals()
                    .Where(a => string.Equals(a.Name, name, StringComparison.OrdinalIgnoreCase))
                    .OrderBy(a => Distance(new TileCoordinate((int)a.Tile.X, (int)a.Tile.Y), near))
                    .ToList();
                if (locAnimals.Count > 0)
                    return locAnimals[0];
            }

            var animals = Game1.locations
                .SelectMany(loc => loc.getAllFarmAnimals())
                .Where(a => string.Equals(a.Name, name, StringComparison.OrdinalIgnoreCase))
                .OrderBy(a => Distance(new TileCoordinate((int)a.Tile.X, (int)a.Tile.Y), near))
                .ToList();
            if (animals.Count > 0)
                return animals[0];
        }
        catch (Exception ex)
        {
            _monitor.Log($"Error resolving animal '{name}' on '{locationName}': {ex.Message}", LogLevel.Warn);
        }

        return null;
    }
}
