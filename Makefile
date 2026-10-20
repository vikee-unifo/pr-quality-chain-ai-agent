# One command after cloning:  make
# Creates .venv, installs dependencies, creates .env from .env.example, runs the offline tests and a demo review.
# Requires Python 3.10+ and make (Linux, macOS, or WSL on Windows). Nothing here calls a real model except `make live`.

PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin
STAMP := $(VENV)/.installed

.PHONY: all setup test lint demo evaluate serve live clean

all: test demo

$(STAMP): pyproject.toml
	@$(PYTHON) -c 'import sys; sys.exit("Python 3.10+ is required (found %s)" % sys.version.split()[0]) if sys.version_info < (3, 10) else None'
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --quiet --upgrade pip
	$(BIN)/pip install --quiet -e ".[dev,llm]"
	@touch $(STAMP)

.env:
	cp .env.example .env
	@echo "Created .env from .env.example (mock provider: offline and free). Edit it to use a real model."

setup: $(STAMP) .env

test: setup  ## offline test suite; any network access fails the run
	$(BIN)/python -m pytest -q

lint: setup
	$(BIN)/ruff check app tests
	$(BIN)/ruff format --check app tests

demo: setup  ## scripted mock review of the benchmark PR (no API key, no network)
	$(BIN)/python -m app.cli review --case authorization-bypass --publishable

evaluate: setup  ## mock benchmark: tests the pipeline, not a model
	$(BIN)/python -m app.cli evaluate --db mock-eval.db

serve: setup  ## API on http://localhost:8000 (uses the provider configured in .env)
	$(BIN)/uvicorn app.main:create_app --factory --port 8000

# Real model calls (uses quota/credits). Needs LLM_PROVIDER=litellm, LLM_MODEL and the provider key in .env.
# Refuses to overwrite earlier results. Override names with: make live RUN=2
RUN ?= 1
live: setup
	@for f in live-eval-$(RUN).md live-eval-$(RUN).log live-results-$(RUN).db; do \
		if [ -e $$f ]; then echo "refusing: $$f exists (use RUN=<n>)"; exit 1; fi; done
	$(BIN)/python -m app.cli evaluate --live --out live-eval-$(RUN).md --db live-results-$(RUN).db 2> live-eval-$(RUN).log; \
		code=$$?; echo "exit=$$code (0 complete, 3 partial, 1 not measured, 2 configuration error); log: live-eval-$(RUN).log"; exit $$code

clean:  ## remove the virtualenv and caches (keeps .env and results)
	rm -rf $(VENV) .pytest_cache .ruff_cache *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
