# Codex Desktop Notes

Compaction Sentinel targets Codex Desktop on macOS. It uses current Codex hook behavior and avoids unsupported assumptions.

## Hook Discovery

The installer writes to `~/.codex/hooks.json` because user-level hooks are reliable in Codex Desktop. The plugin also contains `hooks/hooks.json`, but plugin hooks are currently gated by the `plugin_hooks` feature, so the installer does not depend on that path.

## Skill Discovery

The installer copies the skill to both common user skill roots:

- `~/.codex/skills/compaction-sentinel`
- `~/.agents/skills/compaction-sentinel`

This handles current Desktop setups and the newer public skill path.

## MCP

The installer appends:

```toml
[mcp_servers.compaction_sentinel]
command = "python3"
args = ["/path/to/compaction-sentinel", "mcp"]
```

Use `codex mcp list` after installation to confirm visibility.

## Permission Requests

Sentinel records `PermissionRequest` events so repeated approval prompts can be surfaced in the next packet. It does not approve or deny requests. Normal Codex approval UX stays in charge.

## Stop Continuation

Codex supports using a `Stop` hook continuation to ask the agent to keep working. Compaction Sentinel leaves this off by default because a public tool should not unexpectedly keep every task alive. Users who want unattended continuation can install with:

```bash
python3 scripts/install.py --auto-continue gentle
```

Disable it again with:

```bash
python3 scripts/install.py --auto-continue off
```

`gentle` and `strict` modes are capped per turn and skip continuation when the last assistant message looks complete, when the same checkpoint/next action has already been used, or when loop warnings are active.

## Known Codex Limitations

- `PreToolUse` and `PostToolUse` do not intercept every possible tool path yet.
- Hooks are guardrails and observability, not a complete security boundary.
- `PostToolUse` runs after side effects have already happened.
- Plugin hooks are off by default unless `[features].plugin_hooks = true`.
- A Desktop restart is still the clearest way to ensure newly installed hooks, skills, and MCP config are loaded.
