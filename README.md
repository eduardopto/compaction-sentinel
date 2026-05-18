# Compaction Sentinel

[![test](https://github.com/eduardopto/compaction-sentinel/actions/workflows/test.yml/badge.svg)](https://github.com/eduardopto/compaction-sentinel/actions/workflows/test.yml)

Compaction Sentinel is a Codex Desktop continuity layer for long-running work. It combines a skill, hooks, a local SQLite ledger, loop detection, and MCP tools so Codex can resume the actual live process after compaction instead of restarting from the last visible message.

It is Codex Desktop-first and dependency-free at runtime.

## Why It Exists

Long coding sessions fail in a specific way: the visible task still exists, but after compaction the agent may forget the investigation path, repeat a stale loop, or treat work from two minutes ago as old history. Compaction Sentinel records compact, factual state at the moments that matter and injects a resume packet when Codex starts, resumes, or receives the next user prompt.

## What It Adds

- A `compaction-sentinel` skill for agent behavior after compaction.
- Hooks for `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop`.
- A local SQLite ledger under `~/.codex/compaction-sentinel/`.
- MCP tools: `compaction_checkpoint`, `compaction_note`, `compaction_packet`, `compaction_search`, and `compaction_status`.
- A CLI: `cs checkpoint`, `cs note`, `cs packet`, `cs status`, `cs search`, `cs doctor`.
- Secret redaction before prompts, tool inputs, or tool outputs are stored.
- Loop warnings when the same prompt/tool/result pattern repeats.
- Optional Stop-hook continuation mode for users who explicitly want Codex to keep going on active checkpoints.

## Install

From this checkout:

```bash
python3 scripts/install.py
```

Then restart Codex Desktop and review/trust the new hooks when Codex asks.

On macOS the installer also creates `~/.codex/bin/cs`. If that folder is not on your shell path, run commands with the full path:

```bash
~/.codex/bin/cs doctor
```

For stronger unattended behavior:

```bash
python3 scripts/install.py --enable-stop-continue
```

That mode asks Codex to continue from the latest active checkpoint when a turn stops before completion. It is intentionally opt-in.

## Quick Use

Create a checkpoint before a long or risky stretch:

```bash
~/.codex/bin/cs checkpoint \
  --objective "Fix checkout reliability and verify on device" \
  --current-step "Generation retry path is fixed and tests pass" \
  --next-action "Install on the physical phone and report locked-device launch separately" \
  --evidence "Unit tests passed; native sync completed"
```

Show the packet Codex will receive:

```bash
~/.codex/bin/cs packet
```

Add a durable note:

```bash
~/.codex/bin/cs note "Do not reopen broad roster redesign; visible-face quality is the only remaining blocker."
```

## Public Plugin Shape

This repository is also packaged as a Codex plugin:

- `.codex-plugin/plugin.json`
- `skills/compaction-sentinel/SKILL.md`
- `hooks/hooks.json`
- `.mcp.json`

Codex plugin hooks are currently opt-in in Codex releases, so the installer also writes user-level hooks into `~/.codex/hooks.json` for reliable Desktop behavior.

## Safety

Compaction Sentinel does not replace verification. It preserves state and interrupts obvious repetition, but agents must still read current files, run real checks, and report what was verified.

By default it does not block prompts or force continuation. It records, injects context, and warns. The Stop-hook continuation path only turns on with `--enable-stop-continue`.

## Development

Run the test suite:

```bash
make test
```

Run hook smoke tests by piping sample payloads:

```bash
printf '{"cwd":"%s","session_id":"dev","prompt":"set goal: build the package"}' "$PWD" \
  | PYTHONPATH=src python3 -m compaction_sentinel.cli --codex-home /tmp/cs-home hook UserPromptSubmit
```

See [docs/MACOS_INSTALL.md](docs/MACOS_INSTALL.md) and [docs/VERIFICATION.md](docs/VERIFICATION.md) for the Mac install and release checklist.

## Sources Behind The Design

- Codex hooks support `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop`, including `additionalContext` injection and Stop-hook continuations.
- Codex skills are focused `SKILL.md` directories and plugins are the recommended distribution unit for reusable skills.
- OpenAI API compaction carries state forward, but its compacted item is opaque; this project adds a human-readable local ledger for Codex Desktop workflows.
