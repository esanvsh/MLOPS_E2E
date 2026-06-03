.PHONY: help up down logs health ml-train verify clean

GREEN  := \033[0;32m
YELLOW := \033[0;33m
RED    := \033[0;31m
RESET  := \033[0m

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "$(GREEN)%-20s$(RESET) %s\n", $$1, $$2}'

up: ## Start all local services
	docker compose up -d
	@echo "$(GREEN)✅ Services started$(RESET)"
	@make health

down: ## Stop all local services
	docker compose down
	@echo "$(YELLOW)🛑 Services stopped$(RESET)"

restart: ## Restart all services
	docker compose down && docker compose up -d

logs: ## Tail all logs
	docker compose logs -f

logs-api: ## Tail fraud model API logs
	docker compose logs -f fraud-model-api

health: ## Check health of all services
	@echo "$(YELLOW)Checking services...$(RESET)"
	@curl -sf http://localhost:8001/health > /dev/null && echo "$(GREEN)✅ backend-bff$(RESET)"         || echo "$(RED)❌ backend-bff$(RESET)"
	@curl -sf http://localhost:8002/health > /dev/null && echo "$(GREEN)✅ auth-service$(RESET)"        || echo "$(RED)❌ auth-service$(RESET)"
	@curl -sf http://localhost:8003/health > /dev/null && echo "$(GREEN)✅ transaction-service$(RESET)" || echo "$(RED)❌ transaction-service$(RESET)"
	@curl -sf http://localhost:8004/health > /dev/null && echo "$(GREEN)✅ fraud-model-api$(RESET)"     || echo "$(RED)❌ fraud-model-api$(RESET)"
	@curl -sf http://localhost:5050/health > /dev/null && echo "$(GREEN)✅ mlflow$(RESET)"              || echo "$(RED)❌ mlflow$(RESET)"
	@curl -sf http://localhost:9000/minio/health/live > /dev/null && echo "$(GREEN)✅ minio$(RESET)"    || echo "$(RED)❌ minio$(RESET)"

setup: ## First-time local stack setup
	docker compose up -d
	sleep 30
	./scripts/setup-local-stack.sh
	@echo "$(GREEN)✅ Setup complete! Run 'make ml-train' next$(RESET)"

install: ## Install all Python dependencies into .venv
	.venv/bin/pip install -r services/auth-service/requirements.txt
	.venv/bin/pip install -r services/transaction-service/requirements.txt
	.venv/bin/pip install -r services/fraud-model-api/requirements.txt
	.venv/bin/pip install -r ml/requirements.txt

frontend-install: ## Install frontend npm packages
	cd frontend && npm install

frontend-dev: ## Start Vite dev server
	cd frontend && npm run dev

ml-features: ## Run feature engineering pipeline
	.venv/bin/python ml/feature_engineering/build_features.py

ml-train: ## Train fraud model
	.venv/bin/python ml/training/train.py

ml-notebook: ## Start Jupyter Lab
	.venv/bin/jupyter lab ml/notebooks/

test: ## Run all tests
	.venv/bin/python -m pytest services/ ml/ -v

test-ml: ## Run ML tests only
	.venv/bin/python -m pytest ml/tests/ -v

drift-check: ## Run drift check manually
	docker compose exec drift-monitor python app/drift_check.py

drift-simulate: ## Simulate drift
	docker compose exec drift-monitor python app/simulate_drift.py

verify: ## Verify local environment
	@echo "$(YELLOW)Verifying environment...$(RESET)"
	@python --version 2>&1 | grep -q "3.11" && echo "$(GREEN)✅ Python 3.11$(RESET)"    || echo "$(RED)❌ Python version wrong$(RESET)"
	@node --version 2>&1 | grep -q "v20"    && echo "$(GREEN)✅ Node 20$(RESET)"         || echo "$(RED)❌ Node version wrong$(RESET)"
	@docker info > /dev/null 2>&1            && echo "$(GREEN)✅ Docker running$(RESET)"  || echo "$(RED)❌ Docker not running$(RESET)"
	@claude --version > /dev/null 2>&1       && echo "$(GREEN)✅ Claude Code$(RESET)"     || echo "$(RED)❌ Claude Code not found$(RESET)"
	@aws --version > /dev/null 2>&1          && echo "$(GREEN)✅ AWS CLI$(RESET)"         || echo "$(RED)❌ AWS CLI not found$(RESET)"
	@helm version > /dev/null 2>&1           && echo "$(GREEN)✅ Helm$(RESET)"            || echo "$(RED)❌ Helm not found$(RESET)"
	@terraform --version > /dev/null 2>&1    && echo "$(GREEN)✅ Terraform$(RESET)"       || echo "$(RED)❌ Terraform not found$(RESET)"
	@[ -f .env ] && echo "$(GREEN)✅ .env file$(RESET)"                                   || echo "$(RED)❌ .env missing$(RESET)"
	@[ -d .venv ] && echo "$(GREEN)✅ .venv exists$(RESET)"                               || echo "$(RED)❌ .venv missing$(RESET)"

clean: ## Clean build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	docker compose down -v 2>/dev/null || true
