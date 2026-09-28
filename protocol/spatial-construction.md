# Spatial observation and construction

These operations use the existing `skill.execute` envelope and `skill.result` reply.
They do not introduce a layout generator: the caller chooses explicit placements and ordering.

| skillId | parameters | result |
| --- | --- | --- |
| `inspect-location` | `locationId`, optional `region:{x,y,width,height}` | `details.farmSpace` |
| `inspect-map-image` | `locationId` matching the player's rendered map | `details.mapImage` |
| `inspect-crafting` | `locationId` | `details.crafting` |
| `place-items` | `locationId`, `itemId`, 1–64 ordered `tiles` | Per-tile `placed` effects |
| `remove-items` | `locationId`, expected `itemId`, 1–64 ordered `tiles` | Per-tile `removed-and-recovered` effects |
| `craft-items` | `locationId`, `recipeName`, `itemCount` (1–64 craft iterations) | One `crafted` effect per completed iteration |
| `move-building` | `locationId`, `buildingName` (observed building GUID), `tile` (new top-left origin) | One verified `building-moved` effect |

Observation commands return zero completed actions and no resource use, and do not bump the world revision.
Spatial data is on demand, never attached to routine snapshots. `farmSpace` contains:

- `width`, `height`: returned region dimensions; `mapWidth`, `mapHeight`: complete map dimensions.
- `offset:{x,y}`: row-grid origin. A row character at `(column,row)` describes absolute tile `(offset.x+column,offset.y+row)`.
- `rows`, `legend`: complete grid within the requested rectangle. `.` is passable ground, `o` is passable occupied terrain, `#` is blocked, `E` is an entrance/warp. Passability is not a promise that every item can be placed there.
- `occupants`: objects, terrain, resource clumps, furniture and building footprints intersecting the rectangle. All coordinates remain absolute. Buildings include stable `id`, `entrance` and `indoors` when available.
- `warps`, `capturedRevision`, `placementGuaranteed:false`.

Out-of-bounds regions and regions over 65536 tiles are explicitly rejected; data is never silently truncated. Full-map inspection omits `region`. Follow-up reviews should request the changed region.

Only `inspect-*` result envelopes may exceed the ordinary 64 KB outbound limit, up to a bounded 2 MB. An oversized result becomes a terminal `RESPONSE_TOO_LARGE` error before idempotency caching; it is not silently dropped or left running. Incoming command limits stay unchanged.

`mapImage` supplies `locationId`, `path`, `mimeType`, `scale`, `capturedRevision` and `region` description. The native renderer writes a unique PNG under the controlled temporary `StardewAI.Companion/map-images` directory. The caller cannot choose a path. Screenshots never move the player and cannot render an off-screen map; custom maps may define a native `ScreenshotRegion` crop. Files are image artifacts, not part of save data or periodic context.

Construction walks to each target and invokes native rules. Placement consumes the companion's real existing item only after the native placement action; observable placement is verified. Native floor/path, fence/gate, furniture, big craftable, sprinkler and supported sapling predicates determine eligible items. Occupied tiles and entrances are protected; construction does not implicitly clear crops or replace existing objects.

Removal requires an exact expected item identity. Native tool-generated drops are collected by the companion during the same tick, and the received inventory delta is verified. Chests, filled machines, tappers and non-construction objects are protected. Ordinary empty furniture can be picked up in single-player by transferring its existing native instance; storage furniture, subclasses and multiplayer pickup are explicitly unsupported.

Crafting uses player-unlocked recipes as shared farm knowledge and consumes only companion inventory. Native ingredient matching and recipe output creation are retained; no recipe is unlocked and no human inventory is consumed. Reported crafting counts use companion materials; execution revalidates overlapping modded ingredient categories. Building relocation is single-player only, rejects construction-in-progress and occupied destination footprints, and calls the native building move path with safety checks enabled. The companion automatically stands north or west of the new origin. New building construction, upgrades and demolition are not exposed.

Mutating actions retain existing pause/resume/cancel and budget handling. A cancellation is not rollback: completed tiles remain real and are returned in `effects`; callers must re-observe and submit only remaining work. Native failure or recovery failure is never converted into fabricated success. Reusing a command's idempotency key with changed identity, recipe, destination, region or construction order is a conflict.
