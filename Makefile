SHELL := /bin/bash
# =============================================================================
# Variables
# =============================================================================

.DEFAULT_GOAL := help
UV_OPTS ?=
UV      ?= uv $(UV_OPTS)

.PHONY: help
help: ## Display this help text for Makefile
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

# =============================================================================
# Setup & Installation
# =============================================================================

##@ Setup & Installation

.PHONY: install upgrade
install: ## Install the project + dev tools
	@echo "=> Installing dependencies"
	@$(UV) sync

upgrade: ## Upgrade dependencies (including the sidequest-common/sidequest-api git pins to their current rev)
	@$(UV) lock --upgrade

# =============================================================================
# Code Quality
# =============================================================================

##@ Code Quality

.PHONY: fmt fmt-check lint type-check type-baseline test coverage evals check-all ci
fmt: ## Format and autofix (ruff)
	@$(UV) run --no-sync ruff format .
	@$(UV) run --no-sync ruff check --fix .

fmt-check: ## Check formatting without changing files
	@$(UV) run --no-sync ruff format --check .

lint: ## Run ruff without changing files
	@$(UV) run --no-sync ruff check .

type-check: ## Type check with pyrefly (new errors only; known ones live in pyrefly-baseline.json)
	@$(UV) run --no-sync pyrefly check

type-baseline: ## Re-record pyrefly-baseline.json (after fixing or deliberately accepting errors)
	@$(UV) run --no-sync pyrefly check --update-baseline

test: ## Run the test suite
	@PYTHONDONTWRITEBYTECODE=1 $(UV) run --no-sync pytest -q -W error::DeprecationWarning

coverage: ## Run the test suite with a coverage report
	@PYTHONDONTWRITEBYTECODE=1 $(UV) run --no-sync pytest -q --cov=sidequest_bot --cov-report=term-missing --cov-report=xml

evals: ## Run the AI helper evals against a real model (needs AI_API_KEY; EVAL_MODELS=a,b compares models)
	@PYTHONDONTWRITEBYTECODE=1 $(UV) run --no-sync pytest evals -q --force-enable-socket -p no:cacheprovider

check-all: fmt-check lint type-check test ## Run every check CI runs

ci: check-all ## Run all checks for CI

# =============================================================================
# Run
# =============================================================================

##@ Run

.PHONY: bot
bot: ## Run the bot (needs DISCORD_TOKEN, API_KEY, API_URL in the env or .env; API_URL must point at a running API)
	@$(UV) run sidequest-bot

# =============================================================================
# Docker
# =============================================================================

##@ Docker

.PHONY: docker-build
docker-build: ## Build the bot image
	@docker build -t sidequest-bot .

# =============================================================================
# Main
# =============================================================================

##@ Main

.PHONY: clean
clean: ## Remove caches and build output
	@rm -rf .pytest_cache .ruff_cache .coverage coverage.xml htmlcov dist
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
