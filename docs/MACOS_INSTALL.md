# macOS Install

Compaction Sentinel is designed for Codex Desktop on macOS.

## Requirements

- macOS with Codex Desktop installed.
- Python 3.11 or newer.
- Codex hooks enabled. The installer ensures `hooks = true` in `~/.codex/config.toml`.

## Install From A Checkout

```bash
git clone https://github.com/eduardopto/compaction-sentinel.git
cd compaction-sentinel
python3 scripts/install.py --doctor
```

## Install With pipx Or uvx

```bash
pipx install git+https://github.com/eduardopto/compaction-sentinel.git
compaction-sentinel install --doctor
```

or:

```bash
uvx --from git+https://github.com/eduardopto/compaction-sentinel.git compaction-sentinel install --doctor
```

Package installs include the skill assets, so they do not require a source checkout after installation.

## What The Installer Writes

- `~/.codex/compaction-sentinel/` runtime files and SQLite database.
- `~/.codex/hooks.json` entries for the selected hook profile. The default `balanced` profile installs `SessionStart`, `UserPromptSubmit`, `PreCompact`, `PostCompact`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, and `Stop`.
- `~/.codex/config.toml` MCP entry.
- `~/.codex/skills/compaction-sentinel` skill copy when `--skills-target codex` or `both` is used.
- `~/.agents/skills/compaction-sentinel` skill copy when `--skills-target agents` or `both` is used.
- `~/.codex/bin/cs` and `~/.codex/bin/compaction-sentinel` shims when those names are available.

The guaranteed CLI path is `~/.codex/bin/cs`. Plain `cs` only works if your shell PATH includes `~/.codex/bin` or if you opt in to a global shim.

For normal Full Access Codex Desktop runs, the skill uses the guaranteed CLI path for checkpoint/evidence/status writes. MCP remains installed, but it is secondary because MCP/plugin calls can have a separate approval prompt even when shell/filesystem access is already Full Access.

## Hook Profiles

The default profile is `balanced`:

```bash
python3 scripts/install.py --doctor --hooks-profile balanced
```

Profiles:

- `full`: installs all hook events and keeps maximum hot-hook capture.
- `balanced`: installs all hook events but uses cheaper default runtime behavior.
- `light`: installs only `SessionStart`, `UserPromptSubmit`, `PermissionRequest`, and `Stop`. MCP tools and the skill still work, but hot `PreToolUse`/`PostToolUse` loop checks are omitted.

You can switch an installed profile with:

```bash
~/.codex/bin/cs config set hooks_profile light
~/.codex/bin/cs doctor --fix --explain
```

Before changing `~/.codex/hooks.json` or `~/.codex/config.toml`, the installer writes backups under:

```bash
~/.codex/backups/compaction-sentinel/
```

## After Install

Restart Codex Desktop. If Codex asks you to review new hooks, trust the Compaction Sentinel hooks.

Verify:

```bash
~/.codex/bin/cs doctor --explain
codex mcp list
```

You should see `compaction_sentinel` in the MCP list and `doctor` should return `"ok": true`.

## Repair

```bash
~/.codex/bin/cs doctor --fix --explain
```

This repairs missing hook entries, the MCP block, the hooks feature flag, the configured Sentinel-owned skill copy, and Sentinel-owned CLI links. It does not erase your ledger.

## Optional Global CLI Shims

Sentinel does not silently write into global shell directories. If you want plain `cs` to work in shells that already include `/opt/homebrew/bin`, opt in explicitly:

```bash
~/.codex/bin/cs doctor --fix --global-bin /opt/homebrew/bin --explain
```

or during install:

```bash
python3 scripts/install.py --doctor --global-bin /opt/homebrew/bin
```

`doctor` records opt-in global shim directories in Sentinel config. `uninstall` removes only recorded Sentinel-owned global shims; it will not remove unrelated commands.

## Backups

```bash
~/.codex/bin/cs backup list
~/.codex/bin/cs backup restore hooks.json.20260518-120000.bak
```

Backups are normal files. Restoring creates another backup of the current target first.

## Auto-Continue Policy

The default is safe mode:

```bash
python3 scripts/install.py --auto-continue off
```

For long unattended runs:

```bash
python3 scripts/install.py --auto-continue gentle
```

`gentle` is capped per session/turn and per checkpoint. It will not continue if the last assistant message looks like verified completion, a cooldown is active, the same checkpoint/next action was already used, or loop warnings are already firing. `strict` is available but intentionally scary.

## Uninstall

```bash
~/.codex/bin/cs uninstall
```

To remove the local runtime and ledger too:

```bash
~/.codex/bin/cs uninstall --purge
```

Uninstall also removes Sentinel-owned skill copies. If a skill directory exists with unexpected non-Sentinel content, it is preserved and reported by `doctor` instead of being deleted.
