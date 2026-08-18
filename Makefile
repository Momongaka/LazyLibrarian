# Makefile
# Simplified commands using Ruff for formatting and linting

.PHONY: help install install-dev format lint test coverage clean pre-commit security all

# Default target
.DEFAULT_GOAL := help

# Colors for output
BLUE := \033[0;34m
GREEN := \033[0;32m
YELLOW := \033[0;33m
RED := \033[0;31m
NC := \033[0m # No Color

help: ## Show this help message
	@echo "$(BLUE)LazyLibrarian Development Commands$(NC)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'

install: ## Install production dependencies with uv
	@echo "$(BLUE)Installing production dependencies...$(NC)"
	#pip install uv
	uv venv
	. .venv/bin/activate && uv pip install -r requirements.txt

install-dev: ## Install development dependencies
	@echo "$(BLUE)Installing development dependencies...$(NC)"
	#pip install uv
	uv venv
	. .venv/bin/activate && uv pip install -r requirements.txt
	pre-commit install

format: ## Format code with Ruff
	@echo "$(BLUE)Formatting code with Ruff...$(NC)"
	ruff format lazylibrarian/
	@echo "$(GREEN)✓ Code formatted$(NC)"

format-check: ## Check code formatting without modifying
	@echo "$(BLUE)Checking code formatting...$(NC)"
	ruff format --check --diff lazylibrarian/

lint: ## Lint code with Ruff (auto-fix)
	@echo "$(BLUE)Linting and fixing with Ruff...$(NC)"
	ruff check lazylibrarian/ --fix
	@echo "$(GREEN)✓ Linting complete$(NC)"

lint-check: ## Lint code without fixing
	@echo "$(BLUE)Checking code with Ruff...$(NC)"
	ruff check lazylibrarian/

lint-errors-only: ## Check for errors only (like pylint -E)
	@echo "$(BLUE)Checking for errors only...$(NC)"
	ruff check lazylibrarian/ --select E,F

typecheck: ## Run type checking with mypy
	@echo "$(BLUE)Running type checker...$(NC)"
	mypy lazylibrarian/ --ignore-missing-imports

pre-commit: ## Run pre-commit hooks on all files
	@echo "$(BLUE)Running pre-commit hooks...$(NC)"
	pre-commit run --all-files

pre-commit-update: ## Update pre-commit hooks
	@echo "$(BLUE)Updating pre-commit hooks...$(NC)"
	pre-commit autoupdate

deps-check: ## Check for outdated dependencies
	@echo "$(BLUE)Checking for outdated dependencies...$(NC)"
	uv pip list --outdated

deps-compile: ## Compile requirements.txt from requirements.in
	@echo "$(BLUE)Compiling dependencies...$(NC)"
	uv pip compile requirements.in -o requirements.txt

deps-sync: ## Sync installed packages with requirements.txt
	@echo "$(BLUE)Syncing dependencies...$(NC)"
	uv pip sync requirements.txt

clean: ## Clean build artifacts and cache
	@echo "$(BLUE)Cleaning up...$(NC)"
	find . -type f -name '*.pyc' -delete
	find . -type d -name '__pycache__' -delete
	find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true
	rm -rf build dist .eggs htmlcov .coverage .pytest_cache .mypy_cache .ruff_cache
	@echo "$(GREEN)✓ Cleaned$(NC)"

clean-all: clean ## Clean everything including virtual environment
	rm -rf .venv
	@echo "$(GREEN)✓ Deep clean complete$(NC)"

ci-local: ## Run all CI checks locally
	@echo "$(BLUE)Running full CI pipeline locally...$(NC)"
	@$(MAKE) format-check
	@$(MAKE) lint-errors-only
	@echo "$(GREEN)✓ All CI checks passed$(NC)"

all: format lint typecheck ## Run format, lint, typecheck, and test

fix: ## Auto-fix all issues (format + lint)
	@echo "$(BLUE)Auto-fixing issues...$(NC)"
	@$(MAKE) format
	@$(MAKE) lint
	@echo "$(GREEN)✓ Auto-fixes applied$(NC)"

setup: install-dev pre-commit ## Initial setup for development
	@echo "$(GREEN)✓ Development environment ready!$(NC)"
	@echo ""
	@echo "$(YELLOW)Next steps:$(NC)"
	@echo "  1. Activate virtualenv: source .venv/bin/activate"
	@echo "  2. Format code: make format"
	@echo "  3. Run linters: make lint"
	@echo "  4. Check errors: make lint-errors-only"

stats: ## Show code statistics with Ruff
	@echo "$(BLUE)Code statistics...$(NC)"
	ruff check lazylibrarian/ --statistics
