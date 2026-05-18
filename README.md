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
<compaction-sentinel version="0.3.0" schema="packet-v2" reason="session-start">
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
- CLI commands for checkpoints, evidence, avoid-list items, scrub/export, retention, config, backups, doctor repair, and uninstall.
- Replay evals for stale restarts, repeated failures, objective changes, tool-output blindness, redaction, project switching, and Stop continuation loops.
- Secret redaction for OpenAI keys, GitHub PATs, AWS keys, Google API keys, Slack tokens, JWTs, private keys, bearer tokens, env-style secrets, and high-entropy values.
- Fail-open hooks. If Sentinel crashes, Codex work continues.

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
cs install --doctor
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
~/.codex/bin/cs evidence add "make test passed at 2026-05-18T18:20Z"
```

Record something not to repeat:

```bash
~/.codex/bin/cs avoid add "Do not rerun npm install; dependency issue was already ruled out"
```

Show the packet Codex will receive:

```bash
~/.codex/bin/cs packet
```

Privacy operations:

```bash
~/.codex/bin/cs export --project --output sentinel-export.json
~/.codex/bin/cs scrub --project
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

`gentle` continues only when there is an active checkpoint and no completion-looking final message. Stop continuation is capped per turn, remembers the last checkpoint/next action it used, and stops if loop warnings are already firing. `strict` is intentionally documented as risky.

## Plugin Positioning

The repo includes a Codex plugin shape:

- `.codex-plugin/plugin.json`
- `skills/compaction-sentinel/SKILL.md`
- `hooks/hooks.json`
- `.mcp.json`

Recommended install is still the user-level installer. Current Codex releases load user-level hooks by default, while plugin-bundled hooks require `plugin_hooks = true`.

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
- [Roadmap](ROADMAP.md)

## Sources Behind The Design

- [Codex hooks](https://developers.openai.com/codex/hooks) support lifecycle events, `additionalContext`, `PermissionRequest`, and Stop continuations.
- [Codex skills](https://developers.openai.com/codex/skills) use progressive disclosure and can be activated by relevant tasks.
- [Codex plugin docs](https://developers.openai.com/codex/plugins/build) describe plugin packaging and the current plugin-hook opt-in behavior.
- [OpenAI compaction docs](https://developers.openai.com/api/docs/guides/compaction) describe server-side/standalone compaction and opaque compaction items, which is why Sentinel focuses on a local human-readable ledger.
