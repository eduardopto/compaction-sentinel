# Troubleshooting

## Run Doctor First

```bash
~/.codex/bin/cs doctor --explain
```

Look at:

- `ok`
- `issues`
- `warnings`
- `hooks_by_event`
- `hooks_profile`
- `performance_mode`
- `mcp_present`
- `auto_continue`
- `retention_days`
- `redact`

If `~/.codex/bin/cs` is not available, use:

```bash
~/.codex/compaction-sentinel/bin/cs doctor --explain
```

Do not diagnose Sentinel as missing just because plain `cs` is absent from PATH. Hooks and MCP use absolute paths. The guaranteed CLI path is `~/.codex/bin/cs`.

## Repair Install Drift

```bash
~/.codex/bin/cs doctor --fix --explain
```

This rewrites Sentinel hook entries, the MCP block, the hooks feature flag, and Sentinel-owned CLI links.

If `doctor` says plain `cs` is not discoverable, either keep using the guaranteed path, add `~/.codex/bin` to your shell PATH, or opt in to global shims:

```bash
~/.codex/bin/cs doctor --fix --global-bin /opt/homebrew/bin --explain
```

## Codex Does Not Seem To Remember Anything

1. Restart Codex Desktop.
2. Trust the Compaction Sentinel hooks if Codex asks.
3. Run `~/.codex/bin/cs doctor --explain`.
4. Run `codex mcp list` and confirm `compaction_sentinel` appears.
5. In the project folder, run `~/.codex/bin/cs packet --cwd "$PWD"` and confirm a packet prints.

## The Skill Does Not Appear

The skill is not the primary automatic layer. Hooks are. Still, the installer copies the skill to:

- `~/.codex/skills/compaction-sentinel`
- `~/.agents/skills/compaction-sentinel`

Restart Codex Desktop after install so skill discovery refreshes.

## I Already Have A `cs` Command

The installer avoids overwriting a non-Sentinel `~/.codex/bin/cs`. Use the full internal command:

```bash
~/.codex/compaction-sentinel/bin/cs status --cwd "$PWD"
```

`doctor` will warn if `~/.codex/bin/cs` exists but is not owned by Sentinel.

## Plain `cs` Is Missing From PATH

This is not a hook or MCP failure. Use:

```bash
~/.codex/bin/cs status --cwd "$PWD"
```

To make plain `cs` work globally without editing shell startup files, opt in:

```bash
~/.codex/bin/cs doctor --fix --global-bin /opt/homebrew/bin --explain
```

Sentinel records that global shim directory so `~/.codex/bin/cs uninstall` can remove the Sentinel-owned global links later.

## Codex Feels Slow In Huge Sessions

First check the current mode:

```bash
~/.codex/bin/cs config show
~/.codex/bin/cs status --cwd "$PWD"
```

Try the lower-overhead runtime settings:

```bash
~/.codex/bin/cs config set max_packet_chars 4000
~/.codex/bin/cs config set auto_continue off
~/.codex/bin/cs config set performance_mode light
~/.codex/bin/cs retention set --days 7
```

For the lightest hook install, remove hot tool hooks and keep startup, prompt, permission, Stop, skill, and MCP continuity:

```bash
~/.codex/bin/cs config set hooks_profile light
~/.codex/bin/cs doctor --fix --explain
```

For an A/B test, uninstall without purging the ledger, restart Codex, compare for 20-30 minutes, then reinstall:

```bash
~/.codex/bin/cs uninstall
python3 scripts/install.py --doctor
```

This is not proof that every slowdown is Sentinel. Huge Codex sessions can also slow down because of model context, project size, many tools, app rendering, and compaction itself.

You can run the non-strict hook benchmark from a source checkout:

```bash
python scripts/benchmark_hooks.py
```

## `codex mcp list` Does Not Show `compaction_sentinel`

Run:

```bash
~/.codex/bin/cs doctor --fix --explain
codex mcp list
```

The installer appends a `[mcp_servers.compaction_sentinel]` block to `~/.codex/config.toml`. It creates timestamped backups under `~/.codex/backups/compaction-sentinel/` before changing config files.

## Stop Continuation Is Too Aggressive

Turn it off:

```bash
python3 scripts/install.py --auto-continue off
```

By default, Sentinel does not force continuation. `gentle` and `strict` are opt-in.

## I Need To Delete Stored Data

```bash
~/.codex/bin/cs export --project --cwd "$PWD" --output sentinel-export.json
~/.codex/bin/cs scrub --project --cwd "$PWD"
```

To wipe all Sentinel ledger data:

```bash
~/.codex/bin/cs scrub --all
```

## Restore A Backup

```bash
~/.codex/bin/cs backup list
~/.codex/bin/cs backup restore config.toml.20260518-120000.bak
```

## Remove The Hook And MCP Registration

```bash
~/.codex/bin/cs uninstall
```

To remove runtime and ledger:

```bash
~/.codex/bin/cs uninstall --purge
```
