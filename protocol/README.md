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
