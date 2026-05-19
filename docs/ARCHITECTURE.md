# Architecture

Compaction Sentinel is intentionally small: record the live state before the model can lose it, then expose that state at resume time in a compact operating brief.

## Layers

1. Skill

The skill teaches Codex how to behave after compaction. It is not sufficient by itself because it depends on Codex deciding to load it.

2. Hooks

Hooks run at lifecycle boundaries:

- `SessionStart`: inject latest packet.
- `UserPromptSubmit`: record prompt, infer fresh goals, inject packet.
- `PreToolUse`: record planned tool actions and warn on loops.
- `PermissionRequest`: record approval context and repeated approval requests without deciding for the user.
- `PostToolUse`: record outcomes and warn when a failed pattern repeats.
- `Stop`: record turn end and optionally continue active work.

The hook CLI is fail-open: unexpected exceptions are logged and return `{}` so Codex work is not blocked by Sentinel.

3. SQLite Ledger

The ledger is local, durable, and compact. It stores events, checkpoints, notes, and small state keys per project root.

New active checkpoints supersede older active checkpoints. Complete checkpoints close active work for that project.

4. Packet Builder

Packet v2 turns ledger state into a ranked brief:

- Authority.
- Project.
- Active objective.
- Acceptance criteria.
- Current state.
- Next action.
- Blockers.
- Evidence.
- Do-not-repeat.
- Continuity notes.
- Recent event trail.
- Resume contract.

5. MCP Server

MCP tools let Codex explicitly write and query continuity state when available. Hooks still work if MCP is unavailable. Every MCP tool requires an explicit `cwd` so project resolution is deterministic.

6. CLI

The CLI is both user-facing and hook-facing. User commands manage checkpoints, evidence, avoid items, scrub/export, retention, config, backups, install repair, and uninstall.

## Design Boundaries

- Sentinel does not control server-side compaction.
- Sentinel does not advertise itself as a security boundary.
- Sentinel does not store full transcripts by default.
- Sentinel redacts secrets before storing hook text by default.
- Stop continuation is opt-in and capped.
- Stop continuation state is keyed by hashed project root, session/turn, and optional checkpoint id; it never needs raw project paths in state keys.
- Installer writes are backed up and atomic where practical.
- Plugin packaging exists, but user-level hooks are the reliable install path today.

## Failure Modes Covered

- Agent resumes from the last user message and forgets work from minutes ago.
- Agent repeats the same command, failure, or investigation loop.
- Agent asks for the same risky permission repeatedly.
- Agent loses acceptance criteria after compaction.
- Agent claims completion after a failing tool result.
- Goal UI survives while work effectively stops.
- A new thread starts in the same project without the old investigation packet.
- Two projects write checkpoints without bleeding state into each other.

## Failure Modes Not Fully Solved

- If hooks are disabled or not trusted, only the skill/plugin layer remains.
- If Codex changes hook schemas, Sentinel falls back to best-effort extraction.
- If the agent ignores injected context, the user may still need to ask it to use the skill or inspect `~/.codex/bin/cs packet --cwd "$PWD"`.
- If a task has no checkpoint and no useful hook history, the packet can only preserve recent events.
- Hook coverage is not complete for every possible tool path, so warnings are a continuity guardrail rather than complete enforcement.
