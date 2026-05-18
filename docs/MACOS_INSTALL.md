# macOS Install

Compaction Sentinel is designed for Codex Desktop on macOS.

## Requirements

- macOS with Codex Desktop installed.
- `python3` available from Terminal.
- Codex hooks enabled. The installer ensures `hooks = true` in `~/.codex/config.toml`.

## Install

From the repository:

```bash
python3 scripts/install.py
```

The installer writes:

- `~/.codex/compaction-sentinel/` runtime files and SQLite database.
- `~/.codex/hooks.json` hook entries.
- `~/.codex/config.toml` MCP entry.
- `~/.codex/skills/compaction-sentinel` skill copy.
- `~/.agents/skills/compaction-sentinel` skill copy.
- `~/.codex/bin/cs` and `~/.codex/bin/compaction-sentinel` command shims.

If `~/.codex/bin` is not on your shell path, use the full command path:

```bash
~/.codex/bin/cs status
```

## After Install

Restart Codex Desktop. If Codex asks you to review new hooks, trust the Compaction Sentinel hooks.

Verify:

```bash
~/.codex/bin/cs doctor
codex mcp list
```

You should see `compaction_sentinel` in the MCP list.

## Uninstall

```bash
python3 scripts/uninstall.py
```

The uninstall command removes the hook and MCP registrations. It leaves the local database in place so you can recover or remove it manually.
