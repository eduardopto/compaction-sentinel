# macOS Install

Compaction Sentinel is designed for Codex Desktop on macOS.

## Requirements

- macOS with Codex Desktop installed.
- Python 3.11 or newer available from Terminal.
- Codex hooks enabled. The installer ensures `hooks = true` in `~/.codex/config.toml`.

## Install

From the repository:

```bash
git clone https://github.com/eduardopto/compaction-sentinel.git
cd compaction-sentinel
python3 scripts/install.py
```

The installer writes:

- `~/.codex/compaction-sentinel/` runtime files and SQLite database.
- `~/.codex/hooks.json` hook entries.
- `~/.codex/config.toml` MCP entry.
- `~/.codex/skills/compaction-sentinel` skill copy.
- `~/.agents/skills/compaction-sentinel` skill copy.
- `~/.codex/bin/cs` and `~/.codex/bin/compaction-sentinel` command shims.

Before changing `~/.codex/hooks.json` or `~/.codex/config.toml`, the installer writes timestamped backups under:

```bash
~/.codex/backups/compaction-sentinel/
```

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

`doctor` should return `"ok": true`. If it does not, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Update

Pull the latest repository changes, then rerun the installer:

```bash
git pull
python3 scripts/install.py
```

## Auto-Continue Policy

The default is safe mode:

```bash
python3 scripts/install.py --auto-continue off
```

For long unattended runs:

```bash
python3 scripts/install.py --auto-continue gentle
```

`gentle` asks Codex to continue only when an active checkpoint exists and the last message does not look complete. `strict` is more aggressive and should be used carefully.

## Uninstall

```bash
python3 scripts/uninstall.py
```

The uninstall command removes the hook and MCP registrations. It leaves the local database in place so you can recover or remove it manually.
