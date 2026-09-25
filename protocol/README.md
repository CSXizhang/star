# Protocol v0.1 baseline

`schemas/protocol-v0.1.schema.json` is the source of truth for the stage 0 transport envelope.
The baseline defines handshake, snapshot, execute, cancel, and result message shapes. It does
not yet claim reconnect, persistence, or exactly-once delivery.

During `runtime.hello`, `saveId` and `gameSessionId` are absent because the Runtime cannot know
them before `mod.welcome`. After the handshake they are required by lifecycle policy even though
the shared JSON Schema keeps them optional for bootstrap validation.

Wire compatibility rules:

- JSON property names use camelCase.
- Protocol version `0.1` is a design baseline, not a stable public contract.
- Every side-effecting `skill.execute` requires an `idempotencyKey`.
- All timestamps use RFC 3339 UTC strings.
- `worldRevision` starts at zero during handshake.

## F8 command and control correlation

The chat channel distinguishes a model turn from the whole player instruction.
`chat.reply.requestId` identifies the turn; optional `commandId` identifies its
original player request and stays the same through subsequent turns and native
jobs. Replies remain partitioned by `saveId`.

`commandComplete: false` keeps that instruction active, including after an
individual `job-completed` reply. Only `commandComplete: true` closes the whole
instruction. An omitted value retains the older per-request completion behavior;
it must not be serialized as `false` by a reader that received no value. A late
reply from another command or save must not replace the current display.

F8 pause, resume and cancel use `autonomy.control`, correlated with the control's
own `requestId` and `saveId`. The optional `parameters.commandId` targets the
displayed instruction. The UI shows a pending control until a matching
`autonomy.state` confirmation or rejection arrives. Local safety controls may
already have paused or requested cancellation of a native action; a failed
bridge confirmation must not be shown as global success or automatically undo
that local control. Pausing retains the instruction; cancelling ends it.

## Life-channel messages (`life.*`, contract §1)

The seven `life.*` message types below ride the same `/chat` WebSocket channel
and Envelope as the F8 chat family: `protocolVersion` stays `"0.1"`,
`messageType` is the literal shown, and payloads stay partitioned by `saveId`.
Like the rest of the chat family they are deliberately **not** part of
`schemas/protocol-v0.1.schema.json` (the JSON Schema `oneOf` is unchanged);
they are locked by this documentation plus the cross-language wire contract
tests (`tools/export-life-wire-test.py` ↔ `LifeWireContractTests`).

Direction: **C#→Py** means the Companion Mod sends the message to the Runtime.

### `life.chat.submit` (C#→Py, §1.1)

| field | type | notes |
| --- | --- | --- |
| `requestId` | str | correlates the `life.chat.reply` responses |
| `saveId` | str | active save partition |
| `mode` | str | `"chat"` (casual talk) or `"plan"` (read-only plan discussion; never dispatches) |
| `text` | str | 1..500 chars |
| `source` | str | always `"life-menu"` |

### `life.chat.reply` (Py→C#, §1.2)

| field | type | notes |
| --- | --- | --- |
| `requestId` | str | matches the submit |
| `saveId` | str | |
| `status` | str | `"processing"` \| `"queued"` \| `"completed"` \| `"failed"`; `queued` carries `queuePosition` and the same `requestId` always reaches a terminal state |
| `replyText` | str? | present on completed/failed |
| `queuePosition` | int? | present on `queued` |
| `error` | str? | present on `failed` |
| `profileRevision` | int | for optimistic-lock tracking |
| `memoryRevision` | int | for optimistic-lock tracking |

Never carries token/usage/session fields.

### `life.profile.get` (C#→Py, §1.3)

| field | type |
| --- | --- |
| `requestId` | str |
| `saveId` | str |

### `life.profile.state` (Py→C#, §1.3/§1.4 reply)

| field | type | notes |
| --- | --- | --- |
| `requestId` | str | |
| `saveId` | str | |
| `status` | str | `"confirmed"` \| `"rejected"` (also `"failed"` on handler error) |
| `reason` | str? | e.g. `"STALE_REVISION"` when rejected |
| `profile` | object? | `null` when not onboarded; else see below |
| `profileRevision` | int | |
| `work` | object | read-only autonomy + WorkStore projection, see below |

`profile`:

| field | type | notes |
| --- | --- | --- |
| `onboarded` | bool | |
| `skipped` | bool | |
| `playStyle` | str | `"earn"` \| `"workhorse"` \| `"community"` \| `"decor"` (latter two are planning placeholders) |
| `personality` | str | `"gentle"` \| `"lively"` \| `"calm"` \| `"tsundere"` |
| `careFrequency` | str | `"quiet"` \| `"moderate"` \| `"chatty"` |
| `companionName` | str | 1..12 chars |

`work` (read-only projection; lists capped at 5):

| field | type | notes |
| --- | --- | --- |
| `mode` | str | `"free"` \| `"command"` |
| `paused` | bool | |
| `goal` | str? | |
| `dailySpendLimit` | int? | real semantics: per-day purchase cap (not "reserved funds") |
| `boxPreference` | str? | |
| `dailySpend` | int? | |
| `hasExecutableWork` | bool | |
| `lastPlanAction` | str? | |
| `planWaitReason` | str? | |
| `lastSettledDay` | str? | `"y:season:d"` |
| `activeGoals` | `[{id, text, status}]` | ≤5 |
| `recentTodos` | `[{id, intent, status}]` | ≤5 |
| `waitingConditions` | `[str]` | ≤5, display text only |

### `life.profile.set` (C#→Py, §1.4)

| field | type | notes |
| --- | --- | --- |
| `requestId` | str | |
| `saveId` | str | |
| `expectedRevision` | int | optimistic lock; mismatch → `rejected`/`"STALE_REVISION"` |
| `patch` | object | optional subset of `onboarded`, `skipped`, `playStyle`, `personality`, `careFrequency`, `companionName`; no budget fields |

### `life.memory.list` (C#→Py, §1.5)

| field | type |
| --- | --- |
| `requestId` | str |
| `saveId` | str |

### `life.memory.state` (Py→C#, §1.5/§1.6 reply)

| field | type | notes |
| --- | --- | --- |
| `requestId` | str | |
| `saveId` | str | |
| `status` | str | `"confirmed"` \| `"rejected"` \| `"failed"` |
| `reason` | str? | on rejected/failed |
| `memoryRevision` | int | |
| `entries` | array | each entry: `{id, kind, text, source, gameDate, createdAt}` |

entry fields:

| field | type | notes |
| --- | --- | --- |
| `id` | str | |
| `kind` | str | `"preference"` \| `"agreement"` \| `"event"` |
| `text` | str | ≤200 chars |
| `source` | str | `"player"` \| `"companion"` \| `"system"` |
| `gameDate` | str | `"y:season:d"` |
| `createdAt` | str | RFC 3339 UTC |

### `life.memory.edit` (C#→Py, §1.6)

| field | type | notes |
| --- | --- | --- |
| `requestId` | str | |
| `saveId` | str | |
| `expectedRevision` | int | optimistic lock |
| `op` | str | `"add"` \| `"correct"` \| `"delete"` |
| `id` | str? | target entry for correct/delete |
| `kind` | str? | required for `add`; `event` rejected (`add` is player-sourced only) |
| `text` | str? | 1..200 chars; required for add/correct |

### `life.care` (Py→C#, §1.7, one-way push)

| field | type | notes |
| --- | --- | --- |
| `saveId` | str | |
| `eventKey` | str | dedup key; `{kind}:{gameDate}:{ref}` |
| `gameDate` | str | `"y:season:d"` |
| `kind` | str | `"morning"` \| `"work-done"` \| `"evening"` |
| `text` | str | 1..300 chars, model-generated natural language |

The Mod deduplicates by `eventKey`, shows low-distraction HUD hints only when no
menu/event is active (otherwise defers and stores the body in the life menu's
unread list, capacity 5), and discards unread hints on day change without
back-filling.
