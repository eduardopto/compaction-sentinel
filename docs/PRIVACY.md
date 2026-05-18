# Privacy

Compaction Sentinel records compact local continuity facts so Codex Desktop can resume long work. It is designed to be local-first and conservative.

## What Is Stored

Stored in `~/.codex/compaction-sentinel/sentinel.sqlite`:

- Project root and project name.
- Hook event type and timestamps.
- Compact prompt, tool, permission-request, tool-result, and stop summaries.
- Checkpoints, notes, evidence, and do-not-repeat items.
- Small state keys for Stop continuation caps.

## What Is Not Intentionally Stored

- Full transcripts.
- Raw transcript files.
- Remote analytics.
- Cloud sync.
- Automatic network upload.

Codex hook payloads may include transcript paths, but Sentinel does not parse transcript files because transcript format is not a stable hook interface.

## Redaction

Redaction is on by default:

```bash
cs config set redact true
```

Sentinel redacts common patterns before storing hook text:

- OpenAI API keys.
- GitHub PATs: `ghp_`, `github_pat_`.
- AWS access keys.
- Google API keys.
- Slack tokens.
- JWTs.
- Private key blocks.
- Bearer tokens.
- Env-style secrets such as `API_KEY=...`, `TOKEN=...`, `PASSWORD=...`.
- High-entropy mixed strings.

Redaction is best effort. Do not paste secrets into Codex prompts if you can avoid it.

## Export

```bash
cs export --project --output sentinel-export.json
```

This exports the current project's Sentinel ledger data as JSON for inspection or bug reports.

## Scrub

```bash
cs scrub --project
cs scrub --all
```

`--project` deletes data for the current project root. `--all` deletes events, notes, checkpoints, and Sentinel state for every project.

## Retention

```bash
cs retention set --days 14
```

Retention is applied during normal hook writes. Active and blocked checkpoints are preserved even when older than the retention window.
