# Security Notes

Compaction Sentinel is a continuity tool, not a security product.

## Permission Requests

Sentinel records `PermissionRequest` context so Codex can avoid repeated risky approval loops. It does not return allow or deny decisions. Normal Codex approval UX remains in control.

## Hook Limits

Codex hooks are useful for observability and guardrails, but they do not intercept every possible action path. Do not rely on Sentinel as the only protection against destructive commands.

## Secret Handling

Redaction is enabled by default and covers common key/token formats. It is still best effort. If sensitive data may have been stored:

```bash
~/.codex/bin/cs export --project --cwd "$PWD" --output sentinel-export.json
~/.codex/bin/cs scrub --project --cwd "$PWD"
```

## Reporting Issues

For security issues, avoid posting secrets in public GitHub issues. Share a minimal reproduction with redacted `~/.codex/bin/cs export --project --cwd "$PWD"` output when possible.
