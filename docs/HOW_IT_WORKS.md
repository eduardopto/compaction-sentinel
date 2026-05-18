# How It Works

Compaction Sentinel is not a second agent and not a replacement for OpenAI server-side compaction. It is a local continuity recorder for Codex Desktop on macOS.

## Short Version

1. Hooks run when Codex starts/resumes, receives a prompt, prepares a tool call, asks for permission, receives tool output, and stops a turn.
2. The hook runtime records compact facts in `~/.codex/compaction-sentinel/sentinel.sqlite`.
3. On the next session or prompt, Sentinel injects a packet-v2 operating brief as extra context.
4. Codex sees the active objective, acceptance criteria, current state, next action, do-not-repeat list, blockers, evidence, and loop warnings.
5. Codex can also use MCP tools to save explicit checkpoints, evidence, notes, avoid-list entries, and status.

You do not need to say "use Compaction Sentinel" in every chat after installation and restart.

## Runtime Files

- Runtime: `~/.codex/compaction-sentinel/`
- Database: `~/.codex/compaction-sentinel/sentinel.sqlite`
- Config: `~/.codex/compaction-sentinel/config.json`
- User hooks: `~/.codex/hooks.json`
- MCP config: `~/.codex/config.toml`
- CLI: `~/.codex/bin/cs` or `~/.codex/compaction-sentinel/bin/cs`
- Skill copy: `~/.codex/skills/compaction-sentinel`

## Hook Events

| Event | What Sentinel Does |
| --- | --- |
| `SessionStart` | Records the session start and injects the latest project packet |
| `UserPromptSubmit` | Records the prompt, infers `set goal:` objectives, and injects the packet |
| `PreToolUse` | Records intended tool use and warns on repeated command/investigation patterns |
| `PermissionRequest` | Records approval context and repeated approval requests; never auto-approves |
| `PostToolUse` | Records tool result summaries and warns on repeated failure/result loops |
| `Stop` | Records the turn end; optionally continues active work when explicitly enabled |

## Packet V2

The packet is a ranked operating brief, not a raw log:

- Authority order: current files/results, checkpoint, acceptance criteria, recent trail.
- Active objective and confidence.
- Acceptance criteria in the user's natural language.
- Current state, next action, blockers, and evidence.
- Do-not-repeat items and regression warnings.
- Recent event trail only as supporting context.
- Resume contract that tells Codex to continue the live task, not restart from the last user message.

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

## Fail Open

Hooks should never break normal development. If a hook handler crashes, the CLI logs the error and returns `{}` so Codex can continue.

## Local Data

Sentinel stores compact local text in SQLite. It redacts common API keys, tokens, private keys, bearer tokens, env-style secrets, and high-entropy strings before storing hook text by default. It does not intentionally store full transcripts.
