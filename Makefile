# igbot — convenience targets
PY := .venv/bin/python

.PHONY: help install test doctor baseline watch selftest stats probe clean

help:
	@echo "make install    - install dependencies and the browser"
	@echo "make test       - run the test suite (mock Instagram, no account needed)"
	@echo "make doctor     - check the live setup"
	@echo "make baseline   - read the inbox and show what would be replied"
	@echo "make watch      - run the 24/7 loop"
	@echo "make selftest   - test the send path against your own account"
	@echo "make stats      - show counters"
	@echo "make probe      - dump the live page structure (maintenance)"

install:
	./install.sh

test:
	$(PY) -m pytest tests/ -q

doctor:
	$(PY) -m igbot doctor

baseline:
	$(PY) -m igbot baseline

watch:
	$(PY) -m igbot watch

selftest:
	$(PY) -m igbot selftest

stats:
	$(PY) -m igbot stats

probe:
	$(PY) tools/probe_live.py --out probe

clean:
	rm -rf probe .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
