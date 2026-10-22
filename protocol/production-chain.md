# Native production chain

All mutations run on the game thread through the companion's real inventory and native item/building APIs. No instant crop/animal/processing maturation or injected supplies.

## Resource recovery

- `eat-food`: `{locationId,itemId}` consumes exactly one ordinary positive-energy food carried by the companion. Native `Farmer.doneEating` applies food values, quality, health and buffs. The detached farmer ownership gate is scoped to this one native completion call. Stardrops and negative-energy items are excluded. Fully recovered actors do not consume food.
- `inspect-production`: `{locationId}` returns `details.production` with `foods` (`itemId,name,stack,staminaRecovered,healthRecovered`) from the companion inventory; `groundItems` on the requested map, including indoor eggs; and `waterRefillTiles`. Ground facts cap at 256, water at 32, with separate truncation flags.
- Only an actual game-date advance restores overnight resources. A reverse patch copies the game's stamina/health/exhaustion/late-bedtime block from `Farmer.dayupdate`; it does not run quests, mail, farmhouse upgrades or world updates. A persisted resource-day marker prevents reload recovery. Bedtime comes from the real DayEnding clock. Existing saves without this marker start tracking on first load. Water is not replenished overnight; use `refill-watering-can`.

## Grass and husbandry

- `place-items` accepts the native Grass Starter `(O)297` and Blue Grass Starter `(O)BlueGrassStarter`, consuming real inventory on verified placement.
- `cut-grass`: `{locationId,tiles}` (1–100) uses the companion's actual scythe and native grass tool callback. Native probability and silo space decide hay yield. A successful cut does not promise any hay; inspect silo hay afterward. Scythe supply is never fabricated.
- Animals expose string `animalId` and `homeBuildingId`; pet/animal-harvest requests accept `animalId` instead of `animalName`. Animal buildings expose `buildingId,residentCount,residentAnimalIds`. Use resident count for housing capacity: `animalCount` counts animals currently indoors.
- Eggs already laid on the ground use `pickup-items` in the actual indoor location, not the animal's `currentProduce` field.

## Processing and storage

- `inspect-route`: `{locationId,tile}` returns a read-only reachable entrance route, walking tiles and an estimated normal-clock travel time. Execution uses the same planner; model deliberation and service time are excluded.
- `inspect-livestock`: `{locationId}` observes the requested map, with `roamingScopeLocationId` and `unobservedResidentIds`. An empty companion-map snapshot cannot prove animals absent from another map.

- `inspect-machines`: `{locationId}` returns `details.machines={locationId,items,truncated,capturedRevision}`; exact requested map, at most 256 machines.
- `world.snapshot.productionSignals` contains up to 256 ready machine coordinates across loaded maps and building interiors (`locationId,tile,isReady:true`), with `productionSignalsTruncated`. Refresh follows game-time changes and explicit production inspection/loading/collection. No countdowns or inventories are pushed into this signal. It is reconstructed after reload.
- Existing `insert-machine`, `collect-machine`, `withdraw-chest`, and `deposit-chest` use explicit locations. Machine loading obeys native input and additional-material consumption; collecting moves the actual native output into the companion backpack. Specified chest transfer remains physical.

## Building services

- `inspect-building-services`: `{locationId:'Farm'}` returns `details.buildingServices` with actual blueprint/animal catalogs, resource costs, service counter approach tiles, and construction conditions.
- `build-building`: `{locationId:'Farm',buildingType,tile,budget_limit}`.
- `upgrade-building`: `{locationId:'Farm',buildingName:observedGuid,buildingType,budget_limit}`.
- `purchase-animal`: `{locationId:'Farm',buildingName:observedGuid,animalType,animalName,budget_limit}`, one animal per request.
- The companion visits the real service counter. Target farm selection follows native service semantics; neither farmer is teleported. Construction/upgrades take normal game days. Materials come from the companion; authorized payment uses the native team wallet. `details.totalCost` records actual spend, including committed transactions with later verification errors.

Planting, hoeing, watering, harvest and grass-cut batches are bounded to 100 tiles; placement batches to 64. Long work must split batches and respond to real stamina, water, supplies, backpack capacity and elapsed-time results.

Model-selected short jobs remain bounded to 64 native targets. A declared `plant_seeds` batch may be followed by `water_zone` areas covering those same Farm tiles; count both planting and watering targets, plant before watering, and stop for a new model decision if either step is partial. Other business combinations and future waits remain separate decisions. Crop and chest actions verify the companion's actual loaded location independently of the human player's current map.
