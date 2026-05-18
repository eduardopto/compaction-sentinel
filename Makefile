.PHONY: test compile doctor

PYTHON ?= python3

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests

compile:
	$(PYTHON) -m compileall -q src scripts tests plugin_entry.py

doctor:
	PYTHONPATH=src $(PYTHON) -m compaction_sentinel.cli doctor
