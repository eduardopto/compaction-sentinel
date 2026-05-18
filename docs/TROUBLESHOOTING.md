# Troubleshooting

## Run Doctor First

```bash
~/.codex/bin/cs doctor
```

Look at:

- `ok`
- `issues`
- `warnings`
- `hooks_by_event`
- `mcp_present`
- `auto_continue`

If `~/.codex/bin/cs` is not available, use:

```bash
~/.codex/compaction-sentinel/bin/cs doctor
```

## Codex Does Not Seem To Remember Anything

1. Restart Codex Desktop.
2. Trust the Compaction Sentinel hooks if Codex asks.
3. Run `~/.codex/bin/cs doctor`.
4. Run `codex mcp list` and confirm `compaction_sentinel` appears.
5. In the project folder, run `~/.codex/bin/cs packet` and confirm a packet prints.

## The Skill Does Not Appear

The skill is not the primary automatic layer. Hooks are. Still, the installer copies the skill to:

- `~/.codex/skills/compaction-sentinel`
- `~/.agents/skills/compaction-sentinel`

Restart Codex Desktop after install so skill discovery refreshes.

## I Already Have A `cs` Command

The installer avoids overwriting a non-Sentinel `~/.codex/bin/cs`. Use the full internal command:

```bash
~/.codex/compaction-sentinel/bin/cs status
```

`doctor` will warn if `~/.codex/bin/cs` exists but is not owned by Sentinel.

## `codex mcp list` Does Not Show `compaction_sentinel`

Run:

```bash
python3 scripts/install.py
codex mcp list
```

The installer appends a `[mcp_servers.compaction_sentinel]` block to `~/.codex/config.toml`. It creates timestamped backups under `~/.codex/backups/compaction-sentinel/` before changing config files.

## Stop Continuation Is Too Aggressive

Turn it off:

```bash
python3 scripts/install.py --auto-continue off
```

By default, Sentinel does not force continuation.

## Remove The Hook And MCP Registration

```bash
python3 scripts/uninstall.py
```

The uninstall command leaves the SQLite database in place for recovery.
