PY := .venv/bin/python

.PHONY: test coverage docs check

test: ## Run the full test suite in parallel (same tests CI runs serially)
	@$(PY) -c "import pytest, xdist" 2>/dev/null \
		|| uv pip install -q --python $(PY) pytest pytest-xdist 2>/dev/null \
		|| $(PY) -m pip install -q pytest pytest-xdist
	@$(PY) -m pytest -n auto -q tests/test_documate.py

coverage: ## Line coverage of documate's source → terminal table + coverage/html/index.html
	@$(PY) -m coverage --version >/dev/null 2>&1 \
		|| uv pip install -q --python $(PY) coverage 2>/dev/null \
		|| $(PY) -m pip install -q coverage
	@mkdir -p coverage
	@$(PY) -m coverage run --source=src/documate -m unittest tests.test_documate \
		>coverage/test.log 2>&1 || { cat coverage/test.log; exit 1; }
	@$(PY) -m coverage html -q -d coverage/html
	@$(PY) -m coverage json -q -o coverage/coverage.json
	@$(PY) scripts/coverage_report.py coverage/coverage.json
	@echo "   html: coverage/html/index.html"

docs: ## The whole job: regenerate docs/ and gate them (+ static site: make docs HTML=1)
	@.venv/bin/documate . $(if $(HTML),--html)

check: ## The gate alone: docs fresh, anchors real, no drift vs origin/main
	@.venv/bin/documate --check .
