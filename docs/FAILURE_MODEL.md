# Failure Model

Compaction Sentinel is built around the failure modes that make long Codex Desktop runs painful.

## Covered

- Resume from stale last-message context after compaction.
- Repeated command/fix/test loops.
- Goal UI remains active but practical work stops.
- User acceptance criteria disappear from the live context.
- A new chat in the same project lacks the recent work trail.
- Installer accidentally overwrites existing hook configuration.
- Broken hook runtime interrupts normal Codex work.

## Partially Covered

- Tool-loop detection: hooks currently cover supported tool paths such as Bash, `apply_patch`, and MCP calls. They do not cover every possible internal or future tool path.
- Plugin-only installs: plugin hooks are currently off by default in Codex, so the user-level installer is the recommended Mac path.
- Cross-machine continuity: the SQLite ledger is local. Sync is out of scope for now.

## Not Covered

- Server-side model compaction internals.
- Guaranteeing that the model obeys injected context.
- Reconstructing a task that never had a useful prompt, checkpoint, note, or hook trail.
- Acting as a security sandbox. This is continuity tooling, not a policy-enforcement product.
