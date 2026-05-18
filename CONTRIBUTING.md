# Contributing

Thanks for helping make long Codex Desktop sessions less fragile.

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
make test
```

Runtime code must stay dependency-free unless there is a strong reason. Hooks need to be fast, quiet, and reliable inside Codex Desktop.

## Pull Request Checklist

- Add or update tests for hook behavior.
- Keep secret redaction intact.
- Verify `make test`.
- Verify a hook smoke command from `docs/VERIFICATION.md`.
- Avoid storing full transcripts or unbounded tool output.
