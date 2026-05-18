# Security

Compaction Sentinel stores compact local continuity state under `~/.codex/compaction-sentinel/`.

## Data Handling

- It does not intentionally store full transcripts.
- It redacts common API-key, token, password, and bearer-secret patterns before writing hook text.
- It stores prompt excerpts, tool summaries, checkpoint text, and notes locally in SQLite.

## Reporting Issues

Please report security issues privately by opening a GitHub security advisory if available, or by contacting the repository owner directly.

## Hardening Notes

Hook schemas can change. If Codex changes hook payload shapes, Compaction Sentinel should fail open and avoid blocking normal work.
