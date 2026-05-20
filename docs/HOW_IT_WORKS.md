# How It Works

Compaction Sentinel is not a second agent and not a replacement for OpenAI server-side compaction. It is a local continuity recorder for Codex Desktop on macOS.

## Short Version

1. Hooks run when Codex starts/resumes, receives a prompt, prepares a tool call, asks for permission, receives tool output, and stops a turn.
2. The hook runtime records compact facts in `~/.codex/compaction-sentinel/sentinel.sqlite`.
3. On the next session or prompt, Sentinel injects a packet-v2 operating brief as extra context.
4. Codex sees the active objective, acceptance criteria, current state, next action, do-not-repeat list, blockers, evidence, and loop warnings.
5. Codex can also use the local CLI or MCP tools to save explicit checkpoints, evidence, notes, avoid-list entries, and status.

You do not need to say "use Compaction Sentinel" in every chat after installation and restart.

The skill prefers the local `~/.codex/bin/cs` CLI for normal Codex Desktop work. Codex Full Access applies to shell/filesystem operations, while MCP/plugin tools may still trigger separate approval prompts in some app configurations. The CLI writes the same local ledger and avoids that extra prompt path.

All MCP tools still require an explicit `cwd`. This avoids a subtle but serious reliability bug: a model or MCP server process should never guess which project should receive continuity state.

## Runtime Files

- Runtime: `~/.codex/compaction-sentinel/`
- Database: `~/.codex/compaction-sentinel/sentinel.sqlite`
- Config: `~/.codex/compaction-sentinel/config.json`
- User hooks: `~/.codex/hooks.json`
- MCP config: `~/.codex/config.toml`
- CLI: `~/.codex/bin/cs` or `~/.codex/compaction-sentinel/bin/cs`
- Skill copy: `~/.codex/skills/compaction-sentinel`

Plain `cs` is a shell convenience, not the install contract. The guaranteed command is `~/.codex/bin/cs`; optional global shims are explicit and recorded so uninstall can remove them safely.

## Hook Events

| Event | What Sentinel Does |
| --- | --- |
| `SessionStart` | Records the session start and injects the latest project packet |
| `UserPromptSubmit` | Records the prompt, infers `set goal:` objectives, and injects the packet |
| `PreToolUse` | Records intended tool use and warns on repeated command patterns when the selected hooks profile includes hot hooks |
| `PermissionRequest` | Records approval context and repeated approval requests; never auto-approves |
| `PostToolUse` | Records compact tool result summaries and warns on repeated real failure loops when the selected hooks profile includes hot hooks |
| `Stop` | Records the turn end, runs the full stop-time warning check, and optionally continues active work when explicitly enabled |

## Packet V2

The packet is a ranked operating brief, not a raw log:

- Authority order: current files/results, checkpoint, acceptance criteria, recent trail.
- Active objective and confidence.
- Acceptance criteria in the user's natural language.
- Current state, next action, blockers, and evidence.
- Do-not-repeat items and regression warnings.
- Recent event trail only as supporting context.
- Resume contract that tells Codex to continue the live task, not restart from the last user message.

The default packet budget is intentionally moderate. Increase `max_packet_chars` for debugging, or lower it for very large sessions.

## Performance Modes

`performance_mode` controls how much work the runtime does on hot hooks:

- `full`: maximum capture.
- `balanced`: default; compact storage, throttled maintenance, and cheaper hot-hook warning checks.
- `light`: keeps startup/prompt/Stop/MCP continuity and records only compact failure or test/build milestones if hot hooks are still installed.

`hooks_profile light` goes further by removing `PreToolUse` and `PostToolUse` hook entries from the user-level hook install.

## Rich Checkpoints

A checkpoint can include:

```text
objective
acceptance_criteria
current_step
next_action
blockers
evidence
files_touched
commands_run
tests_passed
tests_failed
decisions_made
assumptions
do_not_repeat
last_verified_at
confidence
status
```

New active checkpoints supersede older active checkpoints for the same project. A complete checkpoint closes active work for that project.

Stop continuation state uses a hash of the project root instead of storing raw project paths in state keys. It is capped by `stop_continue_max_per_turn`, also capped by `stop_continue_max_per_checkpoint_per_turn`, and it honors `stop_continue_cooldown_seconds`. A zero turn cap disables Stop continuation even when `auto_continue` is enabled.

## Regression Warnings

Sentinel fingerprints prompts, tool calls, permission requests, and tool results after normalizing paths, hashes, numbers, and secrets. It detects:

- Same command loops.
- Same failure loops.
- Stale restart signals.
- Step regression or revert risk.
- Oscillation/revert-risk commands.
- Fake completion and tool-output blindness.
- Investigation loops.
- Repeated permission requests.

This is a guardrail, not a security boundary. Current Codex hook interception does not cover every possible tool path.

Failure detection is command-aware. Structured nonzero outcomes always count as failures. Read-only commands such as `cat`, `sed`, `rg`, `git show`, and `python -m json.tool` count as failures only when the tool output starts with a real read/shell/validation error, not merely because the file contents mention words like "failed" or "exception." Test and build commands still treat failing assertions, tracebacks, and failed-suite output as real failures.

## Fail Open

Hooks should never break normal development. If a hook handler crashes, the CLI logs the error and returns `{}` so Codex can continue.

## Local Data

Sentinel stores compact local text in SQLite. It redacts common API keys, tokens, private keys, bearer tokens, env-style secrets, and high-entropy strings before storing hook text by default. It does not intentionally store full transcripts.
