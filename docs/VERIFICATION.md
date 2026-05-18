# Verification

This file records the checks expected before release.

## Local Checks

```bash
make compile
make test
make replay
```

Expected: compile succeeds, unit tests pass, and all replay fixtures pass.

## Replay Evals

Replay fixtures live in `tests/fixtures/scenarios/`:

- `stale_restart_after_compaction.jsonl`
- `repeated_test_failure_loop.jsonl`
- `objective_changed_midstream.jsonl`
- `tool_result_regression.jsonl`
- `sensitive_token_redaction.jsonl`
- `project_switching.jsonl`
- `stop_auto_continue_loop.jsonl`
- `mcp_cwd_contract.jsonl`
- `false_completion_stop_continue.jsonl`
- `long_noisy_packet_budget.jsonl`

Run one scenario:

```bash
python3 scripts/replay_hooks.py tests/fixtures/scenarios/stale_restart_after_compaction.jsonl
```

The harness asserts objective preservation, next action preservation, loop warnings, redaction, DB contents, state-key cleanup, packet budget, project isolation, MCP `cwd` enforcement, false-completion handling, and Stop continuation caps.

## Hook Smoke

```bash
printf '{"cwd":"%s","session_id":"smoke","turn_id":"t1","prompt":"set goal: verify sentinel"}' "$PWD" \
  | PYTHONPATH=src python3 -m compaction_sentinel.cli --codex-home /tmp/cs-home hook UserPromptSubmit
```

Expected: JSON with `hookSpecificOutput.additionalContext` containing a packet-v2 `<compaction-sentinel ...>` block.

## PermissionRequest Smoke

```bash
printf '{"cwd":"%s","session_id":"smoke","turn_id":"t1","tool_use_id":"approval-1","tool_name":"Bash","tool_input":{"command":"rm -rf build","description":"cleanup"}}' "$PWD" \
  | PYTHONPATH=src python3 -m compaction_sentinel.cli --codex-home /tmp/cs-home hook PermissionRequest
```

Expected: `{}` unless the same request repeats. Sentinel records the request but does not approve or deny it.

## MCP Smoke

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | PYTHONPATH=src python3 -m compaction_sentinel.cli --codex-home /tmp/cs-home mcp
```

Expected: initialize response and tools including `compaction_checkpoint`, `compaction_evidence_add`, and `compaction_avoid_add`. Every tool schema should require `cwd`.

## macOS Install Smoke

```bash
rm -rf /tmp/compaction-sentinel-install
python3 scripts/install.py --codex-home /tmp/compaction-sentinel-install --skills-target codex --doctor
/tmp/compaction-sentinel-install/bin/cs --codex-home /tmp/compaction-sentinel-install doctor
```

Expected: `"ok": true`, runtime exists, hooks are present for all events, MCP config is present, and the internal CLI exists.

## Real Codex Install Smoke

```bash
python3 scripts/install.py --doctor
~/.codex/bin/cs doctor --explain
codex mcp list
```

Expected: runtime exists, hooks present, MCP present, and `compaction_sentinel` enabled or visible in Codex MCP output.

## Package Build

```bash
rm -rf /tmp/compaction-sentinel-wheel
python3 -m pip wheel . -w /tmp/compaction-sentinel-wheel
```

Expected: a wheel builds and includes `compaction_sentinel/assets/skills/compaction-sentinel/SKILL.md`.
