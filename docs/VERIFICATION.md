# Verification

This file records the checks expected before release.

## Local Checks

```bash
make compile
make test
PYTHONPATH=src python3 -m compaction_sentinel.cli --codex-home /tmp/cs-home doctor
```

## Hook Smoke

```bash
printf '{"cwd":"%s","session_id":"smoke","turn_id":"t1","prompt":"set goal: verify sentinel"}' "$PWD" \
  | PYTHONPATH=src python3 -m compaction_sentinel.cli --codex-home /tmp/cs-home hook UserPromptSubmit
```

Expected: JSON with `hookSpecificOutput.additionalContext` containing a `<compaction-sentinel ...>` packet.

## MCP Smoke

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | PYTHONPATH=src python3 -m compaction_sentinel.cli --codex-home /tmp/cs-home mcp
```

Expected: initialize response and tools including `compaction_checkpoint`.

## macOS Install Smoke

```bash
python3 scripts/install.py
~/.codex/bin/cs doctor
codex mcp list
```

Expected: runtime exists, hooks present, MCP present, and `compaction_sentinel` enabled or visible in Codex MCP output.
