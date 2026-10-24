.DEFAULT_GOAL := help
COMPOSE := docker compose

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "\033[36m%-18s\033[0m %s\n",$$1,$$2}'

up: ## Start the stack
	$(COMPOSE) up -d --build
down: ## Stop the stack
	$(COMPOSE) down
logs: ## Tail api logs
	$(COMPOSE) logs -f api
shell: ## Shell into the api container
	$(COMPOSE) exec api bash
migrate: ## Apply migrations
	$(COMPOSE) exec api alembic upgrade head
revision: ## Autogenerate a migration: make revision m="add fees"
	$(COMPOSE) exec api alembic revision --autogenerate -m "$(m)"
seed: ## Load the demo school
	$(COMPOSE) exec api python -m scripts.seed_demo
test: ## Run the test suite
	$(COMPOSE) exec api pytest -q
lint: ## Ruff + mypy + architecture contracts
	$(COMPOSE) exec api ruff check app tests
	$(COMPOSE) exec api mypy app
	$(COMPOSE) exec api lint-imports

arch: ## Check the layering contracts only
	$(COMPOSE) exec api lint-imports
fmt: ## Format
	$(COMPOSE) exec api ruff format app tests
openapi: ## Dump the OpenAPI schema for the web repo
	$(COMPOSE) exec -T api python -m scripts.dump_openapi > openapi.json

.PHONY: help up down logs shell migrate revision seed test lint arch fmt openapi

keys: ## Generate the local RS256 signing keypair (gitignored)
	@mkdir -p secrets
	@test -f secrets/jwt_private.pem || openssl genpkey -algorithm RSA \
		-out secrets/jwt_private.pem -pkeyopt rsa_keygen_bits:2048 2>/dev/null
	@test -f secrets/jwt_public.pem || openssl rsa -pubout \
		-in secrets/jwt_private.pem -out secrets/jwt_public.pem 2>/dev/null
	@echo "secrets/jwt_private.pem and secrets/jwt_public.pem ready"

.PHONY: keys

storage-check: ## Prove the configured object store works, end to end
	$(COMPOSE) exec api python -m scripts.check_storage

.PHONY: storage-check
