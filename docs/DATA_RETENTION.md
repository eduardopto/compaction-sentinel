# Data Retention

Sentinel keeps continuity data local under:

```text
~/.codex/compaction-sentinel/
```

## Default Retention

The default retention window is 30 days.

```bash
~/.codex/bin/cs config show
```

## Change Retention

```bash
~/.codex/bin/cs retention set --days 14
```

Set `0` to disable automatic age-based pruning.

## What Gets Pruned

During normal hook writes, Sentinel prunes:

- Old events.
- Closed notes.
- Complete or superseded checkpoints older than the retention window.

Active and blocked checkpoints are kept because they may be needed to resume live work.

## Manual Deletion

```bash
~/.codex/bin/cs scrub --project --cwd "$PWD"
~/.codex/bin/cs scrub --all
```

Use `--project` before sharing a repo-specific bug report. Use `--all` before uninstalling with `--purge` if you want an explicit ledger wipe first.
