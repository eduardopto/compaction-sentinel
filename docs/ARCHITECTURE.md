# Architecture

Compaction Sentinel is intentionally simple: write down the live state before the model forgets it, then make that state visible at resume time.

## Layers

1. Skill

The skill teaches Codex how to use checkpoints and how to behave after compaction. It is not enough by itself because it only works when the agent remembers to load it.

2. Hooks

Hooks run at the hard boundaries:

- `SessionStart`: inject the latest resume packet.
- `UserPromptSubmit`: record the new prompt, infer fresh goals, inject the packet.
- `PreToolUse`: record planned tool actions and warn on repeated loops.
- `PostToolUse`: record outcomes and warn when a failed pattern repeats.
- `Stop`: record turn end and optionally continue active work.

The hook CLI is fail-open: unexpected exceptions are logged and return `{}` so Codex work is not blocked by Sentinel.

3. SQLite Ledger

The ledger is local, durable, and compact. It stores events, checkpoints, and notes per project root. New active checkpoints supersede older active checkpoints, and complete checkpoints close active work for that project.

4. MCP Server

MCP tools let Codex explicitly write and query continuity state when available. The hooks still work if MCP is unavailable.

## Design Boundaries

- The package does not claim to control server-side compaction.
- The package does not trust stale packets over current files.
- The package does not store full transcripts.
- The package redacts obvious secrets before storing text.
- Forced continuation is opt-in.
- Installer writes are backed up and atomic where possible.

## Failure Modes Covered

- Agent resumes from the last user message and forgets work from minutes ago.
- Agent repeats the same command/fix/test loop.
- Agent loses acceptance criteria after compaction.
- Goal UI survives while work effectively stops.
- A new thread starts in the same project without the old investigation packet.

## Failure Modes Not Fully Solved

- If hooks are disabled or not trusted, only the skill/plugin layer remains.
- If Codex changes hook schemas, the handler falls back to best-effort extraction.
- If the agent ignores injected context, the user may still need to ask it to use the skill.
- If a task has no checkpoint and no useful prompt history, the packet can only preserve recent hook events.
