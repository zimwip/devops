# Makefile — Platform developer shortcuts
# Usage: make <target>

.PHONY: help install dev-api dev-ui dev test lint build clean \
        env-list svc-list poc-create poc-destroy

PYTHON   := python3
PIP      := pip3
API_PORT := 5173
UI_PORT  := 5174

# ── Help ──────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  Platform — available targets"
	@echo "  ─────────────────────────────────────────────────────────"
	@echo "  make install        Install all Python + Node dependencies"
	@echo "  make dev            Start API + UI in parallel (tmux or two shells)"
	@echo "  make dev-api        Start FastAPI backend only  → :$(API_PORT)"
	@echo "  make dev-ui         Start React frontend only   → :$(UI_PORT)"
	@echo "  make test           Run backend pytest suite"
	@echo "  make lint           Ruff + eslint"
	@echo "  make build          Build React frontend for production"
	@echo "  make clean          Remove build artefacts"
	@echo ""
	@echo "  Platform CLI shortcuts:"
	@echo "  make env-list       List all environments"
	@echo "  make svc-list       List all services"
	@echo "  make poc-create NAME=my-poc BASE=staging"
	@echo "  make poc-destroy NAME=poc-my-poc-20260403"
	@echo ""

# ── Install ───────────────────────────────────────────────────────────────────
install:
	@echo "→ Installing Python dependencies..."
	$(PIP) install -r scripts/requirements.txt --break-system-packages
	@echo "→ Installing Node dependencies..."
	cd dashboard/frontend && npm install
	@echo "✓ Done. Run 'make dev' to start."

# ── Development servers ───────────────────────────────────────────────────────
dev-api:
	@echo "→ Starting FastAPI on :$(API_PORT)  (Swagger UI at http://localhost:$(API_PORT)/docs)"
	cd dashboard/backend && \
	    PYTHONPATH=../../scripts:. \
	    uvicorn app:app --reload --port $(API_PORT) \
	    --reload-dir ../../scripts

dev-ui:
	@echo "→ Starting React dev server on :$(UI_PORT)  (proxies /api → :$(API_PORT))"
	cd dashboard/frontend && npm run dev

dev:
	@echo "→ Starting API + UI. Open http://localhost:$(UI_PORT)"
	@$(MAKE) -j2 dev-api dev-ui

# ── Test ──────────────────────────────────────────────────────────────────────
test:
	@echo "→ Running backend tests..."
	PYTHONPATH=scripts:dashboard/backend \
	    $(PYTHON) -m pytest dashboard/backend/tests/ -v --tb=short

# ── Lint ──────────────────────────────────────────────────────────────────────
lint:
	@echo "→ Linting Python..."
	$(PYTHON) -m ruff check scripts/ dashboard/backend/ || true
	@echo "→ Linting JavaScript..."
	cd dashboard/frontend && npx eslint src/ || true

# ── Build (production) ────────────────────────────────────────────────────────
build:
	@echo "→ Building React frontend..."
	cd dashboard/frontend && npm run build
	@echo "✓ Frontend built → dashboard/frontend/dist/"
	@echo "  Run 'make dev-api' to serve both UI and API on :$(API_PORT)"

# ── Clean ─────────────────────────────────────────────────────────────────────
clean:
	rm -rf dashboard/frontend/dist/
	rm -rf dashboard/frontend/node_modules/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete

# ── CLI shortcuts ─────────────────────────────────────────────────────────────
env-list:
	$(PYTHON) scripts/platform_cli.py env list

svc-list:
	$(PYTHON) scripts/platform_cli.py service list

poc-create:
	@test -n "$(NAME)" || (echo "Usage: make poc-create NAME=my-poc BASE=staging [FORCE=1]" && exit 1)
	$(PYTHON) scripts/platform_cli.py env create \
	    --name "$(NAME)" \
	    --type poc \
	    --base "$(or $(BASE),staging)" \
	    $(if $(FORCE),--force,)

poc-destroy:
	@test -n "$(NAME)" || (echo "Usage: make poc-destroy NAME=poc-my-poc-20260403" && exit 1)
	$(PYTHON) scripts/platform_cli.py env destroy --name "$(NAME)" --force

svc-create:
	@test -n "$(NAME)" || (echo "Usage: make svc-create NAME=my-svc TEMPLATE=springboot OWNER=team-x [FORCE=1]" && exit 1)
	$(PYTHON) scripts/platform_cli.py service create \
	    --name "$(NAME)" \
	    --template "$(or $(TEMPLATE),springboot)" \
	    --owner "$(or $(OWNER),unknown)" \
	    $(if $(FORCE),--force,)

deploy:
	@test -n "$(SVC)" || (echo "Usage: make deploy SVC=service-auth VERSION=2.3.0 ENV=dev [FORCE=1]" && exit 1)
	$(PYTHON) scripts/platform_cli.py deploy \
	    --service "$(SVC)" \
	    --version "$(VERSION)" \
	    --env "$(or $(ENV),dev)" \
	    $(if $(FORCE),--force,)
