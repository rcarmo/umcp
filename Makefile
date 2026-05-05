# umcp -- developer convenience targets
#
# Most targets shell out to ``uv`` so the test environment is hermetic
# and the Python version is pinned.  Override the Python version with::
#
#     make test PY=3.11
#
# Falls back to plain ``python -m pytest`` if uv is not installed.

PY ?= 3.12
PYTEST_ARGS ?=

UV := $(shell command -v uv 2>/dev/null)

ifdef UV
RUN := uv run --with pytest --with pytest-asyncio --python $(PY)
RUN_COV := uv run --with pytest --with pytest-asyncio --with coverage --python $(PY)
else
RUN := python
RUN_COV := python
endif

.PHONY: help test test-fast coverage coverage-html lint clean install dev

help:
	@echo "Targets:"
	@echo "  test          -- run the full test suite ($(PY))"
	@echo "  test-fast     -- run the suite with -x and quiet output"
	@echo "  coverage      -- run the suite under coverage and print a report"
	@echo "  coverage-html -- run the suite under coverage and emit htmlcov/"
	@echo "  lint          -- run ruff (if installed) over umcp.py / aioumcp.py"
	@echo "  clean         -- remove caches, coverage data, log files"
	@echo "  install       -- install umcp into the current environment (pip)"
	@echo ""
	@echo "Override the Python version with PY=3.11 (default 3.12)"

test:
	$(RUN) python -m pytest tests/ $(PYTEST_ARGS)

test-fast:
	$(RUN) python -m pytest tests/ -x -q $(PYTEST_ARGS)

coverage:
	$(RUN_COV) python -m coverage run --source=umcp,aioumcp -m pytest tests/ -q
	$(RUN_COV) python -m coverage report

coverage-html: coverage
	$(RUN_COV) python -m coverage html
	@echo "Report written to htmlcov/index.html"

lint:
	@if command -v ruff >/dev/null; then \
		ruff check umcp.py aioumcp.py examples/ tests/; \
	else \
		echo "ruff not installed; skipping"; \
	fi

clean:
	rm -rf .pytest_cache .coverage htmlcov __pycache__ .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
	find . -type f -name 'mcpserver.log' -delete

install:
	pip install .

dev:
	pip install -e '.[dev]' || pip install -e .
