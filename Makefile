.PHONY: test compile replay benchmark doctor

PYTHON ?= python3

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests

replay:
	$(PYTHON) scripts/replay_hooks.py tests/fixtures/scenarios/*.jsonl

benchmark:
	$(PYTHON) scripts/benchmark_hooks.py

compile:
	$(PYTHON) -m compileall -q src scripts tests plugin_entry.py

doctor:
	PYTHONPATH=src $(PYTHON) -m compaction_sentinel.cli doctor
