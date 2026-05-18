.PHONY: test compile replay doctor

PYTHON ?= python3

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests

replay:
	$(PYTHON) scripts/replay_hooks.py tests/fixtures/scenarios/*.jsonl

compile:
	$(PYTHON) -m compileall -q src scripts tests plugin_entry.py

doctor:
	PYTHONPATH=src $(PYTHON) -m compaction_sentinel.cli doctor
