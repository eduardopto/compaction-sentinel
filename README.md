# Compaction Sentinel

[![test](https://github.com/eduardopto/compaction-sentinel/actions/workflows/test.yml/badge.svg)](https://github.com/eduardopto/compaction-sentinel/actions/workflows/test.yml)

Compaction Sentinel is a macOS-only continuity layer for Codex Desktop. It uses Codex hooks, a local SQLite ledger, a skill, MCP tools, replay evals, privacy controls, and regression warnings so Codex can resume the real live task after compaction instead of drifting back to stale work.

It does not try to control OpenAI server-side compaction. It makes compaction failures rarer, visible, recoverable, and measurable.

## The Failure Mode

Before compaction:

- Codex fixed the auth flow.
- Tests failed only on the device install.
- The next action was to run the device build.

After compaction without Sentinel:

- Codex restarts from the last visible user message.
- It reopens old auth files.
- It repeats the same failing command.
- It claims progress while losing the real next step.

With Sentinel:

- The active objective is restored.
- Last verified evidence is restored.
- Repeated command and failure warnings are injected.
- The packet says the exact next action first.

Example packet excerpt:

```xml
<compaction-sentinel version="0.4.2" schema="packet-v2" reason="session-start">
  <active_objective>
  status: active
  objective: Finish auth repair and device verification
  </active_objective>
  <next_action>
  Run the device build first.
  </next_action>
  <do_not_repeat>
  - Same failure loop repeated 3 times: pytest tests/test_checkout.py ...
  </do_not_repeat>
</compaction-sentinel>
```

## What It Adds

- User-level Codex hooks for `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, and `Stop`.
- Packet v2: a ranked operating brief with authority, objective, acceptance criteria, current state, next action, do-not-repeat items, blockers, evidence, and recent trail.
- A local SQLite ledger under `~/.codex/compaction-sentinel/`.
- MCP tools: `compaction_checkpoint`, `compaction_evidence_add`, `compaction_avoid_add`, `compaction_note`, `compaction_packet`, `compaction_search`, and `compaction_status`.
- A strict MCP `cwd` contract so every tool call targets an explicit project.
- CLI commands for checkpoints, evidence, avoid-list items, scrub/export, retention, config, backups, doctor repair, and uninstall.
- Replay evals for stale restarts, repeated failures, false-positive doc/source reads, objective changes, tool-output blindness, redaction, project switching, and Stop continuation loops.
- Secret redaction for OpenAI keys, GitHub PATs, AWS keys, Google API keys, Slack tokens, JWTs, private keys, bearer tokens, env-style secrets, and high-entropy values.
- Fail-open hooks. If Sentinel crashes, Codex work continues.

## Dogfood Results

Internal dogfood now covers the Python package itself, a React/Electron app, an iOS/Capacitor app, and a large Unity repo for explicit `cwd` project resolution. The replay contract measures stale restart recovery, repeated loop detection, next-action preservation, false-completion handling, redaction, tiny packet budgets, and a 120-event noisy run.

See [Dogfood Results](docs/DOGFOOD.md) for the measured table and limitations.

## Install

Recommended from a checkout:

```bash
git clone https://github.com/eduardopto/compaction-sentinel.git
cd compaction-sentinel
python3 scripts/install.py --doctor
```

Pipx/uvx-friendly install from GitHub:

```bash
pipx install git+https://github.com/eduardopto/compaction-sentinel.git
compaction-sentinel install --doctor
```

or:

```bash
uvx --from git+https://github.com/eduardopto/compaction-sentinel.git compaction-sentinel install --doctor
```

Then restart Codex Desktop and review/trust the new hooks if Codex asks.

Verify:

```bash
~/.codex/bin/cs doctor --explain
codex mcp list
```

## Quick Use

Create a detailed checkpoint:

```bash
~/.codex/bin/cs checkpoint \
  --cwd "$PWD" \
  --objective "Fix checkout reliability and verify on device" \
  --acceptance-criteria "Saved answers never disappear; report locked-phone launch separately" \
  --current-step "Generation retry path is fixed" \
  --next-action "Install on the physical phone" \
  --evidence "Unit tests passed; native sync completed" \
  --files "src/api/delete.ts, tests/delete.test.ts" \
  --tests-passed "pytest tests/delete.test.ts passed" \
  --confidence high
```

Append evidence:

```bash
~/.codex/bin/cs evidence add --cwd "$PWD" "make test passed at 2026-05-18T18:20Z"
```

Record something not to repeat:

```bash
~/.codex/bin/cs avoid add --cwd "$PWD" "Do not rerun npm install; dependency issue was already ruled out"
```

Show the packet Codex will receive:

```bash
~/.codex/bin/cs packet --cwd "$PWD"
```

When Codex uses the MCP tools, it must pass the active project `cwd` every time. Missing `cwd` fails clearly instead of silently writing state to the wrong project.

Privacy operations:

```bash
~/.codex/bin/cs export --project --cwd "$PWD" --output sentinel-export.json
~/.codex/bin/cs scrub --project --cwd "$PWD"
~/.codex/bin/cs retention set --days 14
~/.codex/bin/cs config set redact true
```

Repair or roll back install files:

```bash
~/.codex/bin/cs doctor --fix --explain
~/.codex/bin/cs backup list
~/.codex/bin/cs backup restore hooks.json.20260518-120000.bak
```

Uninstall:

```bash
~/.codex/bin/cs uninstall
~/.codex/bin/cs uninstall --purge
```

`--purge` removes the local runtime and ledger. Plain uninstall leaves the ledger in place.

## Auto-Continue

Auto-continue is off by default and should stay opt-in:

```bash
python3 scripts/install.py --auto-continue off
python3 scripts/install.py --auto-continue gentle
python3 scripts/install.py --auto-continue strict
```

`gentle` continues only when there is an active checkpoint and no verified-completion final message. Stop continuation is capped by `stop_continue_max_per_turn`, also capped by `stop_continue_max_per_checkpoint_per_turn`, honors `stop_continue_cooldown_seconds`, remembers the last checkpoint/next action it used, and stops if loop warnings are already firing. A zero turn cap disables Stop continuation even when `auto_continue` is enabled. `strict` is intentionally documented as risky.

## Plugin Positioning

The repo includes a Codex plugin shape:

- `.codex-plugin/plugin.json`
- `skills/compaction-sentinel/SKILL.md`
- `hooks/hooks.json`
- `.mcp.json`

Recommended install is still the user-level installer. Current Codex releases load user-level hooks by default, while plugin-bundled hooks require `plugin_hooks = true`.

## Known Limitations

- Sentinel cannot control or replace OpenAI server-side compaction.
- Hook coverage is a continuity guardrail, not a security boundary for every possible tool path.
- Codex Desktop may need a restart, and sometimes hook trust approval, before newly installed hooks are active.
- Redaction is best effort. Do not paste secrets into Codex prompts if you can avoid it.
- Auto-continue is off by default because forced continuation can be risky in destructive or ambiguous tasks.
- Dogfood results are internal and replay-backed, not an independent third-party benchmark.

## Development

```bash
make compile
make test
make replay
python3 -m pip wheel . -w /tmp/compaction-sentinel-wheel
```

Replay scenarios live in [tests/fixtures/scenarios](tests/fixtures/scenarios). They are the product contract: correct objective, correct next action, loop warning, redaction, packet budget, project isolation, and Stop-loop caps.

## Documentation

- [How it works](docs/HOW_IT_WORKS.md)
- [macOS install](docs/MACOS_INSTALL.md)
- [Privacy](docs/PRIVACY.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Data retention](docs/DATA_RETENTION.md)
- [Security](docs/SECURITY.md)
- [Verification](docs/VERIFICATION.md)
- [Dogfood Results](docs/DOGFOOD.md)
- [Roadmap](ROADMAP.md)

## Sources Behind The Design

- [Codex hooks](https://developers.openai.com/codex/hooks) support lifecycle events, `additionalContext`, `PermissionRequest`, and Stop continuations.
- [Codex skills](https://developers.openai.com/codex/skills) use progressive disclosure and can be activated by relevant tasks.
- [Codex plugin docs](https://developers.openai.com/codex/plugins/build) describe plugin packaging and the current plugin-hook opt-in behavior.
- [OpenAI compaction docs](https://developers.openai.com/api/docs/guides/compaction) describe server-side/standalone compaction and opaque compaction items, which is why Sentinel focuses on a local human-readable ledger.
