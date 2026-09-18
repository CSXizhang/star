# Stage 0 Protocol & Transport Contract

This document defines the authoritative contract between the **Out-of-Process Runtime (Python)** and the **Stardew AI Companion Mod (C# .NET 6.0)** for Stage 0 ("water-zone" bounded execution).

---

## 1. Architectural Principles & Thread Boundaries

1. **Strict Thread Isolation**:
   - WebSocket socket I/O executes asynchronously on background worker threads.
   - **Network threads MUST NOT read or write game state** (`Game1`, `Farmer`, `Tool`, locations, etc.).
   - Network threads deserialize incoming wire payloads into strongly-typed DTOs, perform structural/envelope validation, and enqueue work onto a **Bounded Main-Thread Queue**.
   - The Stardew Valley main thread drains and processes queued commands during SMAPI `UpdateTicked` ticks.
   - Outgoing messages (e.g. snapshots, skill results, acks, errors) are enqueued from the main thread and sent asynchronously by the transport.

2. **Loopback & Session Security**:
   - WebSocket server binds **exclusively** to the IPv4 loopback interface (`127.0.0.1`).
   - A single active companion client is allowed.
   - A cryptographically random `sessionToken` is generated upon server initialization.
   - The Runtime client must supply this token either as HTTP header `Authorization: Bearer <sessionToken>` or query string parameter `?token=<sessionToken>`.
   - Connections with invalid or missing tokens are rejected immediately with HTTP 401 Unauthorized before WebSocket upgrade.

3. **Wire Hardening Limits**:
   - Maximum incoming frame/message size: **65,536 bytes (64 KB)**.
   - Maximum JSON recursion depth: **32**.
   - Violations immediately terminate the connection with WebSocket close code `1009` (Message Too Big) or `1008` (Policy Violation).

4. **Lifecycle & Readiness**:
   - Protocol lifecycle states:
     `Disconnected` -> `Handshaking` -> `Syncing` -> `Ready` -> `Offline` / `Degraded`.
   - **Ready-Before-Execute**: The server rejects `skill.execute` unless the session is in `Ready` state (meaning `runtime.hello` -> `mod.welcome` and initial `world.snapshot` reconciliation are complete).
   - **One Active Task**: At most one skill task may be active at any given moment. A concurrent `skill.execute` with a new task/command ID is rejected with a `BUSY` / `CONFLICT` error result.

5. **Safe Disconnect**:
   - If the WebSocket closes or times out, the transport transitions to `Offline`.
   - If an active skill task is executing, the transport signals cancellation to the Mechanics Actor with reason `"client_disconnected"` and `cancelPolicy: "safe-point"`.

---

## 2. Shared Envelope Definition

All messages on the wire adhere to the shared JSON envelope:

```json
{
  "protocolVersion": "0.1",
  "messageType": "skill.execute",
  "messageId": "uuid-or-msg-seq",
  "correlationId": "optional-correlating-message-id",
  "saveId": "anon-save-id",
  "gameSessionId": "session-guid",
  "senderInstanceId": "runtime-or-mod-instance-id",
  "sequenceNumber": 0,
  "worldRevision": 12,
  "sentAt": "2026-09-10T12:00:00.000Z",
  "expiresAt": "2026-09-10T12:00:15.000Z",
  "idempotencyKey": "save:task-01:attempt-01",
  "payload": {}
}
```

### Type Constraints
- `protocolVersion`: Exact string `"0.1"`.
- `messageType`: Dot-separated ASCII string (e.g. `runtime.hello`, `mod.welcome`, `world.snapshot`, `skill.execute`, `skill.cancel`, `skill.pause`, `skill.resume`, `skill.result`, `protocol.error`).
- `sequenceNumber`: Non-negative 64-bit integer (`>= 0`). **Booleans are strictly rejected** (`True` / `False` are not integers).
- `worldRevision`: Non-negative 64-bit integer (`>= 0`). **Booleans are strictly rejected**.
- `sentAt`: RFC 3339 string with mandatory timezone offset (`Z` or `+HH:MM`). **Naive datetimes are strictly rejected**.
- `expiresAt`: Optional RFC 3339 string with mandatory timezone offset if present.
- `payload`: Non-null JSON Object.

---

## 3. Message Catalog & Payloads

### 3.1 Handshake: `runtime.hello` (Runtime -> Mod)
Sent by Runtime immediately upon WebSocket connection.
```json
{
  "protocolVersion": "0.1",
  "messageType": "runtime.hello",
  "messageId": "msg-hello-001",
  "senderInstanceId": "runtime-abc",
  "sequenceNumber": 0,
  "worldRevision": 0,
  "sentAt": "2026-09-10T12:00:00Z",
  "payload": {
    "supportedProtocolVersions": ["0.1"],
    "runtimeVersion": "0.1.0",
    "sessionToken": "secret-token",
    "features": {
      "planner": false,
      "memory": false,
      "voice": false,
      "mcp": false
    }
  }
}
```

### 3.2 Handshake Response: `mod.welcome` (Mod -> Runtime)
Sent by Mod in response to valid `runtime.hello`.
```json
{
  "protocolVersion": "0.1",
  "messageType": "mod.welcome",
  "messageId": "msg-welcome-001",
  "correlationId": "msg-hello-001",
  "saveId": "farm-save-hash",
  "gameSessionId": "session-1234",
  "senderInstanceId": "mod-instance-001",
  "sequenceNumber": 0,
  "worldRevision": 1,
  "sentAt": "2026-09-10T12:00:01Z",
  "payload": {
    "selectedProtocolVersion": "0.1",
    "modVersion": "0.1.0",
    "gameVersion": "1.6.15",
    "smapiVersion": "4.2.1",
    "lifecycleState": "syncing",
    "skills": ["water-zone"]
  }
}
```

### 3.3 State Snapshot: `world.snapshot` (Mod -> Runtime)
Sent upon completing handshake, on demand, or on map/revision change.
```json
{
  "protocolVersion": "0.1",
  "messageType": "world.snapshot",
  "messageId": "msg-snapshot-001",
  "correlationId": "msg-hello-001",
  "saveId": "farm-save-hash",
  "gameSessionId": "session-1234",
  "senderInstanceId": "mod-instance-001",
  "sequenceNumber": 1,
  "worldRevision": 1,
  "sentAt": "2026-09-10T12:00:01Z",
  "payload": {
    "capturedRevision": 1,
    "companion": {
      "locationId": "Farm",
      "tileX": 64,
      "tileY": 15,
      "facingDirection": 2,
      "stamina": 270.0,
      "maxStamina": 270,
      "waterCanLevel": 40,
      "maxWaterCanLevel": 40,
      "hasWateringCan": true,
      "activity": "idle"
    },
    "world": {
      "currentLocation": "Farm",
      "timeOfDay": 600,
      "season": "spring",
      "dayOfMonth": 1,
      "isRaining": false
    },
    "farmWork": {
      "tilledUnwateredTiles": [
        { "x": 64, "y": 15 },
        { "x": 64, "y": 16 }
      ],
      "tilledUnwateredCount": 2,
      "isTruncated": false,
      "matureCropCount": 0
    }
  }
}
```

### 3.4 Command: `skill.execute` (Runtime -> Mod)
Dispatches execution of a registered skill. Stage 0 focus: `skillId = "water-zone"`.
```json
{
  "protocolVersion": "0.1",
  "messageType": "skill.execute",
  "messageId": "msg-exec-001",
  "saveId": "farm-save-hash",
  "gameSessionId": "session-1234",
  "senderInstanceId": "runtime-abc",
  "sequenceNumber": 1,
  "worldRevision": 1,
  "sentAt": "2026-09-10T12:00:02Z",
  "expiresAt": "2026-09-10T12:00:32Z",
  "idempotencyKey": "farm-save-hash:task-water-01:1",
  "payload": {
    "commandId": "cmd-water-001",
    "taskId": "task-water-01",
    "skillId": "water-zone",
    "skillVersion": "0.1",
    "expectedWorldRevision": 1,
    "parameters": {
      "locationId": "Farm",
      "tiles": [
        { "x": 64, "y": 15 },
        { "x": 64, "y": 16 },
        { "x": 65, "y": 15 }
      ]
    },
    "budgets": {
      "maxGameMinutes": 60,
      "maxStamina": 50.0,
      "maxWater": 20
    },
    "cancelPolicy": "safe-point",
    "policyDecisionId": "policy-allow-01"
  }
}
```

#### Parameter Bounds for `water-zone`:
- `locationId`: Non-empty string matching the Companion's current map (cross-map execution rejected).
- `tiles`: Array of 1 to 100 coordinates `{"x": int, "y": int}`.
- `budgets.maxGameMinutes`: Integer in `[1, 1440]`.
- `budgets.maxStamina`: Float in `[0.0, 500.0]`.
- `budgets.maxWater`: Integer in `[0, 100]`.

### 3.5 Command: `skill.cancel` (Runtime -> Mod)
Cancels the currently running or queued skill.
```json
{
  "protocolVersion": "0.1",
  "messageType": "skill.cancel",
  "messageId": "msg-cancel-001",
  "correlationId": "msg-exec-001",
  "saveId": "farm-save-hash",
  "gameSessionId": "session-1234",
  "senderInstanceId": "runtime-abc",
  "sequenceNumber": 2,
  "worldRevision": 1,
  "sentAt": "2026-09-10T12:00:10Z",
  "payload": {
    "commandId": "cmd-water-001",
    "reason": "Player commanded stop",
    "requestedByPlayer": true,
    "cancelPolicy": "safe-point"
  }
}
```

### 3.6 Command: `skill.pause` (Runtime -> Mod)
Pauses the active skill at the nearest safe point.
```json
{
  "protocolVersion": "0.1",
  "messageType": "skill.pause",
  "messageId": "msg-pause-001",
  "correlationId": "msg-exec-001",
  "saveId": "farm-save-hash",
  "gameSessionId": "session-1234",
  "senderInstanceId": "runtime-abc",
  "sequenceNumber": 3,
  "worldRevision": 1,
  "sentAt": "2026-09-10T12:00:12Z",
  "payload": {
    "commandId": "cmd-water-001",
    "reason": "Dialogue opened"
  }
}
```

### 3.7 Command: `skill.resume` (Runtime -> Mod)
Resumes execution of the paused skill.
```json
{
  "protocolVersion": "0.1",
  "messageType": "skill.resume",
  "messageId": "msg-resume-001",
  "correlationId": "msg-exec-001",
  "saveId": "farm-save-hash",
  "gameSessionId": "session-1234",
  "senderInstanceId": "runtime-abc",
  "sequenceNumber": 4,
  "worldRevision": 1,
  "sentAt": "2026-09-10T12:00:15Z",
  "payload": {
    "commandId": "cmd-water-001"
  }
}
```

### 3.8 Terminal Result: `skill.result` (Mod -> Runtime)
Published by Mod when a skill execution reaches a terminal state.
```json
{
  "protocolVersion": "0.1",
  "messageType": "skill.result",
  "messageId": "msg-result-001",
  "correlationId": "msg-exec-001",
  "saveId": "farm-save-hash",
  "gameSessionId": "session-1234",
  "senderInstanceId": "mod-instance-001",
  "sequenceNumber": 2,
  "worldRevision": 4,
  "sentAt": "2026-09-10T12:00:25Z",
  "payload": {
    "commandId": "cmd-water-001",
    "taskId": "task-water-01",
    "terminalState": "succeeded",
    "completedCount": 3,
    "skippedCount": 0,
    "failedCount": 0,
    "finalWorldRevision": 4,
    "effects": [
      {
        "tile": { "x": 64, "y": 15 },
        "state": "watered"
      },
      {
        "tile": { "x": 64, "y": 16 },
        "state": "watered"
      },
      {
        "tile": { "x": 65, "y": 15 },
        "state": "watered"
      }
    ],
    "resources": {
      "staminaUsed": 6.0,
      "waterUsed": 3,
      "gameMinutesElapsed": 12
    },
    "error": null,
    "retryRecommended": false,
    "playerActionRequired": false
  }
}
```

#### `terminalState` Enum:
- `"succeeded"`: All requested targets were successfully watered/completed.
- `"partially-succeeded"`: Some targets completed, but stopped early due to budget exhaustion or obstacle.
- `"failed"`: Preconditions failed or execution aborted due to unrecoverable error.
- `"cancelled"`: Execution halted following a `skill.cancel` command or disconnect.
- `"rejected"`: Command was rejected during preflight validation (e.g. invalid bounds, wrong map, busy, expired).

#### Error Shape (`error`):
```json
{
  "code": "CAN_EMPTY",
  "message": "Watering can has no water remaining",
  "details": "Tile (65, 15) could not be watered",
  "retryable": true
}
```

### 3.9 Protocol Error: `protocol.error` (Bidirectional)
Returned when an incoming message is unparseable or violates wire invariants.
```json
{
  "protocolVersion": "0.1",
  "messageType": "protocol.error",
  "messageId": "msg-err-001",
  "correlationId": "msg-bad-001",
  "senderInstanceId": "mod-instance-001",
  "sequenceNumber": 3,
  "worldRevision": 1,
  "sentAt": "2026-09-10T12:00:03Z",
  "payload": {
    "code": "IDEMPOTENCY_CONFLICT",
    "message": "Idempotency key reused with mismatched parameters"
  }
}
```

---

## 4. Idempotency & Replay Semantics

The Mod maintains an in-memory sliding window of recent `idempotencyKey` records within the current `gameSessionId`.
Each entry records:
- `idempotencyKey`
- `commandId`
- `parametersHash`
- `status` (`InFlight` or `Completed`)
- `cachedResult` (`skill.result` envelope)

### Handling Rules:
1. **In-Flight Duplicate**:
   - If an execute command is received with a key that is currently executing with matching parameters:
   - Mod does not restart execution; it acknowledges the in-flight operation.
2. **Completed Replay**:
   - If an execute command is received with a key that has already completed with matching parameters:
   - Mod immediately replays the cached `skill.result` with updated timestamp.
3. **Idempotency Conflict**:
   - If an execute command arrives with an identical `idempotencyKey` but differing `commandId`, `skillId`, or `parameters`:
   - Mod immediately rejects the command with `terminalState: "rejected"` and error code `IDEMPOTENCY_CONFLICT`.

---

## 5. Main-Thread Queue with Cancel Priority

Incoming WebSocket messages pass through structural and token validation on the network thread, then enter the `BoundedMessageQueue`:
- Maximum capacity: **64 messages**.
- If full and the incoming message is a normal command, it is dropped or rejected with `QUEUE_FULL`.
- **Cancel Priority**: `skill.cancel` messages jump ahead of pending execute/sync messages to ensure fast halting of actions even under backpressure.
- Game loop tick drains pending messages and dispatches them sequentially to the `ITransportHandler`.

---

## 6. Exact C# net6.0 DTO & Interface Signatures

These types are implemented in `src/StardewAI.Companion.Mod/Transport/`.

### 6.1 DTO Signatures (`TransportDtos.cs`)

```csharp
namespace StardewAI.Companion.Mod.Transport;

public sealed record TileCoord(int X, int Y);

public sealed record EnvelopeDto(
    string ProtocolVersion,
    string MessageType,
    string MessageId,
    string SenderInstanceId,
    long SequenceNumber,
    long WorldRevision,
    DateTimeOffset SentAt,
    System.Text.Json.Nodes.JsonObject Payload,
    string? CorrelationId = null,
    string? SaveId = null,
    string? GameSessionId = null,
    DateTimeOffset? ExpiresAt = null,
    string? IdempotencyKey = null
);

public sealed record RuntimeHelloPayload(
    string[] SupportedProtocolVersions,
    string RuntimeVersion,
    string? SessionToken,
    Dictionary<string, bool> Features
);

public sealed record ModWelcomePayload(
    string SelectedProtocolVersion,
    string ModVersion,
    string GameVersion,
    string SmapiVersion,
    string LifecycleState,
    string[] Skills
);

public sealed record CompanionSnapshot(
    string LocationId,
    int TileX,
    int TileY,
    int FacingDirection,
    float Stamina,
    int MaxStamina,
    int WaterCanLevel,
    int MaxWaterCanLevel,
    bool HasWateringCan,
    string Activity
);

public sealed record WorldStateSnapshot(
    string CurrentLocation,
    int TimeOfDay,
    string Season,
    int DayOfMonth,
    bool IsRaining
);

public sealed record WorldSnapshotPayload(
    long CapturedRevision,
    CompanionSnapshot Companion,
    WorldStateSnapshot World
);

public sealed record WaterZoneParameters(
    string LocationId,
    List<TileCoord> Tiles
);

public sealed record ExecutionBudgets(
    int MaxGameMinutes,
    float MaxStamina,
    int MaxWater
);

public sealed record SkillExecutePayload(
    string CommandId,
    string TaskId,
    string SkillId,
    string SkillVersion,
    long ExpectedWorldRevision,
    WaterZoneParameters Parameters,
    ExecutionBudgets Budgets,
    string CancelPolicy,
    string PolicyDecisionId
);

public sealed record SkillCancelPayload(
    string CommandId,
    string Reason,
    bool RequestedByPlayer,
    string CancelPolicy
);

public sealed record SkillPausePayload(
    string CommandId,
    string Reason
);

public sealed record SkillResumePayload(
    string CommandId
);

public sealed record SkillResultResources(
    float StaminaUsed,
    int WaterUsed,
    int GameMinutesElapsed
);

public sealed record SkillResultError(
    string Code,
    string Message,
    string? Details,
    bool Retryable
);

public sealed record SkillResultPayload(
    string CommandId,
    string TaskId,
    string TerminalState,
    int CompletedCount,
    int SkippedCount,
    int FailedCount,
    long FinalWorldRevision,
    List<Dictionary<string, object>> Effects,
    SkillResultResources? Resources = null,
    SkillResultError? Error = null,
    bool RetryRecommended = false,
    bool PlayerActionRequired = false
);

public sealed record ProtocolErrorPayload(
    string Code,
    string Message,
    string? Details = null
);
```

### 6.2 Transport Callback Interface (`ITransportHandler.cs`)

Implemented by the Mechanics Actor / Coordination subsystem:

```csharp
namespace StardewAI.Companion.Mod.Transport;

public interface ITransportHandler
{
    /// <summary>
    /// Invoked on the game main thread when a skill.execute command is dequeued.
    /// Returns true if accepted; false if rejected by preflight validation.
    /// </summary>
    bool TryAcceptSkillExecute(
        SkillExecutePayload payload,
        EnvelopeDto envelope,
        out SkillResultPayload? rejectResult
    );

    /// <summary>
    /// Invoked on the game main thread when a skill.cancel command is dequeued.
    /// </summary>
    void OnCancelSkill(SkillCancelPayload payload, EnvelopeDto envelope);

    /// <summary>
    /// Invoked on the game main thread when a skill.pause command is dequeued.
    /// </summary>
    void OnPauseSkill(SkillPausePayload payload, EnvelopeDto envelope);

    /// <summary>
    /// Invoked on the game main thread when a skill.resume command is dequeued.
    /// </summary>
    void OnResumeSkill(SkillResumePayload payload, EnvelopeDto envelope);

    /// <summary>
    /// Invoked on the game main thread to obtain current world & companion snapshot.
    /// </summary>
    WorldSnapshotPayload CaptureCurrentSnapshot(long worldRevision);

    /// <summary>
    /// Invoked on the game main thread when client disconnects or session is lost.
    /// </summary>
    void OnTransportDisconnected(string reason);
}
```

### 6.3 Transport Server Interface (`ITransportServer.cs`)

```csharp
namespace StardewAI.Companion.Mod.Transport;

public interface ITransportServer : IDisposable
{
    int Port { get; }
    string SessionToken { get; }
    TransportState State { get; }
    bool IsClientConnected { get; }

    void Start();
    void Stop();

    /// <summary>
    /// Must be called every tick on SMAPI's GameLoop.UpdateTicked.
    /// Drains incoming messages from the bounded queue and invokes ITransportHandler.
    /// </summary>
    void Update();

    /// <summary>
    /// Sends a world snapshot to the connected client.
    /// </summary>
    Task SendSnapshotAsync(WorldSnapshotPayload snapshot, long worldRevision);

    /// <summary>
    /// Sends a terminal skill result to the connected client.
    /// </summary>
    Task SendSkillResultAsync(SkillResultPayload result, string correlationId, string? idempotencyKey = null);

    /// <summary>
    /// Sends a protocol error to the connected client.
    /// </summary>
    Task SendProtocolErrorAsync(string code, string message, string? correlationId = null);
}

public enum TransportState
{
    Disconnected,
    Listening,
    Handshaking,
    Syncing,
    Ready,
    Offline
}
```

---

## 7. Integration Instructions for Mechanics Worker

To integrate the Transport into the Companion Mod without editing `ModEntry.cs` before baseline gates are approved:

1. **Instantiation**:
   Create an instance of `WebSocketTransportServer`:
   ```csharp
   var server = new WebSocketTransportServer(
       port: 0, // 0 selects random available loopback port
       sessionToken: Guid.NewGuid().ToString("N"),
       handler: mechanicsActor,
       saveId: currentSaveHash,
       gameSessionId: currentGameSessionId,
       logger: Monitor.Log
   );
   server.Start();
   ```

2. **Tick Hook**:
   Inside SMAPI `UpdateTicked` handler:
   ```csharp
   private void OnUpdateTicked(object? sender, UpdateTickedEventArgs e)
   {
       server.Update();
       // mechanicsActor.Update(e);
   }
   ```

3. **Sending Results**:
   When the state machine completes or halts the `water-zone` execution:
   ```csharp
   await server.SendSkillResultAsync(resultPayload, correlationId, idempotencyKey);
   ```

4. **Shutdown**:
   Inside SMAPI `ReturnedToTitle` or `GameLaunched` teardown:
   ```csharp
   server.Stop();
   server.Dispose();
   ```

---

## 8. Farm Autonomy Skills Extension (`harvest-zone` / `deposit-chest` / `organize-chest`)

This section extends protocol v0.1 with three farm-autonomy skills. All Stage 0 envelope,
lifecycle, idempotency, pause/resume/cancel, and one-active-task rules apply unchanged.
`skillVersion` is `"0.1"` for all three; `budgets` reuse the `{maxGameMinutes, maxStamina,
maxWater}` defaults (hand harvesting costs 0 stamina and is reported as such).

### 8.1 Skill Parameters (`skill.execute` payload)

| skillId | parameters |
|---|---|
| `harvest-zone` | `locationId`, `tiles: [{x, y}...]` (1..100) — hand-harvest mature crops into the companion backpack |
| `deposit-chest` | `locationId`, `chestTile: {x, y}`, `itemIds?: string[]` (<= 36, non-empty strings; omitted = all non-tool items; tools are always excluded) |
| `organize-chest` | `locationId`, `chestTile: {x, y}` — merge same-item stacks inside the chest (no reordering) |
| `hoe-tiles` | `locationId`, `tiles: [{x, y}...]` (1..64) — till dirt tiles with Hoe; preserves existing crops/objects |
| `plant-seeds` | `locationId`, `tiles: [{x, y}...]` (1..64), `seedItemId: string` (qualified e.g. `(O)472` or unqualified) — plant seeds into hoed empty soil |

All other `skill.execute` fields (`commandId`, `taskId`, `expectedWorldRevision`,
`cancelPolicy`, `policyDecisionId`, envelope `idempotencyKey`, envelope `expiresAt`)
are identical to `water-zone`.

### 8.2 `skill.result` Extensions

The optional `details` object may appear per skill (absent otherwise):

- `harvest-zone`: `details.inventoryFull?: true` — backpack full, execution stopped early.
- `deposit-chest`: `details.chestFull?: true` — chest full, execution stopped early.
- `organize-chest`: none.
- `hoe-tiles`: `details.hoedTiles?: [{x, y}...]`, `details.skippedTiles?: [{x, y, reason}...]`.
- `plant-seeds`: `details.plantedTiles?: [{x, y}...]`, `details.remainingSeedStack?: int`, `details.seedItemId?: string`, `details.outOfSeeds?: bool`, `details.skippedTiles?: [{x, y, reason}...]`.

`effects[]` item optional fields per skill:

- `harvest-zone`: `{tile, state: "harvested", cropId, itemId, itemName, stack, quality}`;
  skipped: `{tile, state: "skipped", reason: "not-ready" | "scythe-required" | "inventory-full"}`.
- `deposit-chest`: `{state: "deposited", itemId, itemName, stack, quality, chestTile}`;
  skipped: `{state: "skipped", itemId, reason: "chest-full" | "tool-excluded"}`.
- `organize-chest`: `{state: "merged", itemId, itemName, quality, mergedStack, chestTile}`.
- `hoe-tiles`: `{tile, state: "hoed"}`;
  skipped: `{tile, state: "skipped", reason}`.
- `plant-seeds`: `{tile, state: "planted", itemId, stack}`;
  skipped: `{tile, state: "skipped", reason}`.

`terminalState` conventions follow `water-zone`: all targets completed -> `succeeded`;
some completed but stopped early (including inventory-full / chest-full early stops) ->
`partially-succeeded`; zero completed -> `failed` (budget/timeout) or `cancelled`
(cancellation with completions is `partially-succeeded`). Skips are pre-execution
filters and never alone cause `failed`.

### 8.3 `world.snapshot` Extensions

The snapshot payload gains/extends these sections (all optional on the wire for backward
compatibility; current builds always publish them):

- `farmWork.matureCrops`: `[{x, y, cropId}]`, <= 64 entries, sorted Y-X.
- `farmWork.matureCropsTruncated`: `boolean` — true when the mature crop list was capped.
- `inventory`: `{capacity, freeSlots, slots: [{index, itemId, name, stack, quality}]}` —
  companion backpack, non-empty slots only; `itemId` prefers QualifiedItemId.
- `chests`: `{truncated, items: [{tile, capacity, freeSlots, contents: [{slot, itemId,
  name, stack, quality}]}]}` — normal chests on the Farm only (fridge/special chests
  excluded), sorted Y-X, capped at 16 (`truncated: true` when capped).
- `planting`: `{season, dayOfMonth, companionHasHoe, seeds: [{itemId, name, stack, canPlantCurrentSeason, seasons, growthDays?, regrows?, isRaised?}], candidateTiles: {tilledEmptyCount, tilledEmptyTiles, tilledEmptyTruncated, tillableCount, tillableTiles, tillableTruncated}, searchBounds: {center, radius}}`.

Snapshot push timing is unchanged (after handshake and after every completed task).

### 8.4 `mod.welcome` Skills List

Current builds advertise:

```json
"skills": ["water-zone", "harvest-zone", "deposit-chest", "organize-chest", "hoe-tiles", "plant-seeds"]
```

### 8.5 Idempotency Fingerprint

The semantic fingerprint recorded per `idempotencyKey` covers the existing fields
(`commandId`, `taskId`, `skillId`, `skillVersion`, `expectedWorldRevision`,
`cancelPolicy`, `policyDecisionId`, `locationId`, sorted `tiles`, `budgets`) plus, for
chest skills, normalized `chestTile` (x, y) and sorted `itemIds`; for plant skills,
`seedItemId`; `tiles` may be absent for chest skills.
