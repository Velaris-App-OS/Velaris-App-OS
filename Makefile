.PHONY: help install infra-up infra-down engine-dev test lint format clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-22s\033[0m %s\n", $$1, $$2}'

install: ## Install all dependencies based on velaris.yaml
	uv sync --all-packages

infra-up: ## Start infrastructure (based on your velaris.yaml selections)
	docker compose -f deploy/docker-compose/docker-compose.yml up -d
	@echo "Waiting for services..."
	@sleep 10
	@echo "Infrastructure ready!"

infra-down: ## Stop infrastructure
	docker compose -f deploy/docker-compose/docker-compose.yml down

engine-dev: ## Run Velaris Flow Engine
	cd engine && uv run uvicorn helix_engine.main:app --reload --host 0.0.0.0 --port 8100

engine-worker: ## Run Temporal worker
	cd engine && uv run python -m helix_engine.temporal.worker

test: ## Run all tests
	uv run pytest

test-engine: ## Run engine tests
	cd engine && uv run pytest

lint: ## Lint all code
	uv run ruff check .
	uv run mypy engine/ libs/

format: ## Format all code
	uv run ruff format .
	uv run ruff check --fix .

clean: ## Clean build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true

wizard: ## Re-run the setup wizard
	@echo "Re-running wizard..."
	@bash setup-velaris.sh
