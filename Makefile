.PHONY: install test lint format typecheck ci clean

VENV := .venv
PIP := $(VENV)/bin/pip

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

test:
	$(VENV)/bin/pytest

lint:
	$(VENV)/bin/ruff check ai_review.py tests
	$(VENV)/bin/ruff format --check ai_review.py tests

format:
	$(VENV)/bin/ruff format ai_review.py tests
	$(VENV)/bin/ruff check --fix ai_review.py tests

typecheck:
	$(VENV)/bin/mypy ai_review.py

ci: lint typecheck test
	@echo "All local CI checks passed."

clean:
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache .coverage
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +
