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
