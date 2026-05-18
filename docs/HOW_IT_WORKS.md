# How It Works

Compaction Sentinel is not a second agent and not a replacement for OpenAI's server-side compaction. It is a local continuity layer around Codex Desktop on macOS.

## The Short Version

1. Hooks run automatically when Codex starts a session, receives your prompt, uses tools, receives tool output, and ends a turn.
2. The hook runtime records compact facts in `~/.codex/compaction-sentinel/sentinel.sqlite`.
3. On the next session or prompt, the runtime injects a `<compaction-sentinel>` resume packet as extra developer context.
4. Codex sees the live objective, current step, next action, blockers, recent evidence, and loop warnings.
5. Codex can also use MCP tools to save explicit checkpoints and notes.

You do not need to say "use Compaction Sentinel" in every chat after installation and restart.

## Runtime Files

- Runtime: `~/.codex/compaction-sentinel/`
- Database: `~/.codex/compaction-sentinel/sentinel.sqlite`
- User hooks: `~/.codex/hooks.json`
- MCP config: `~/.codex/config.toml`
- CLI: `~/.codex/bin/cs` or `~/.codex/compaction-sentinel/bin/cs`
- Skill copy: `~/.codex/skills/compaction-sentinel`

## Hook Events

| Event | What Sentinel Does |
| --- | --- |
| `SessionStart` | Injects the latest project resume packet |
| `UserPromptSubmit` | Records the prompt, infers `set goal:` objectives, injects a packet |
| `PreToolUse` | Records intended tool use and warns on repeated patterns |
| `PostToolUse` | Records tool result summaries and warns on repeated result loops |
| `Stop` | Records the turn end; optionally asks Codex to continue active work |

## Checkpoints

A checkpoint is the durable sentence-level truth of the work:

```text
objective: what we are trying to finish
current_step: where the work really is
next_action: the next concrete thing to do
blockers: what prevents progress
evidence: what has been verified
```

When a new active checkpoint is saved, older active checkpoints for the same project are marked `superseded`. When a complete checkpoint is saved, older active checkpoints are marked `complete`.

## Loop Warnings

Sentinel fingerprints prompts, tool calls, and tool results after normalizing paths, hashes, and numbers. If the same pattern appears repeatedly, it injects a warning telling Codex to inspect the latest concrete artifact and change the hypothesis before repeating the loop.

This is a guardrail, not a security boundary. Current Codex hook interception does not cover every possible tool path.

## Fail-Open Design

Hooks should never break normal development. If a hook handler crashes, the CLI logs the error and returns `{}` so Codex can continue.

## Local Data

Sentinel stores compact local text in SQLite. It redacts common API-key, token, password, authorization, and bearer-secret patterns before storing hook text. It does not intentionally store full transcripts.
