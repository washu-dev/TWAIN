# TWAIN — common dev & deploy tasks. Run `make help` for the list.
# Python tasks use the pixi env (has psycopg2 + boto3 + dotenv).
PY := pixi run python

.DEFAULT_GOAL := help

.PHONY: help dev preflight preflight-aws migrate migrate-dry \
        secrets secrets-apply provision provision-apply \
        test test-api test-runner test-pipeline

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

dev: ## Run the full local stack (DB + API + runner + web app)
	./dev.sh

preflight: ## Check local readiness (env, DB, migrations, LLM creds, auth)
	$(PY) scripts/preflight.py

preflight-aws: ## Also verify AWS resources exist (ECR/ECS/log groups/secrets)
	$(PY) scripts/preflight.py --aws

migrate: ## Apply DB migrations now (idempotent)
	cd api && $(PY) migrate.py

migrate-dry: ## Show which migrations WOULD apply (no changes)
	cd api && $(PY) migrate.py --dry-run

secrets: ## PLAN: what setup_secrets.sh would do (no changes)
	scripts/aws/setup_secrets.sh

secrets-apply: ## Wire the Terraform-managed runner secret ARNs into the runner task def
	scripts/aws/setup_secrets.sh --apply

provision: ## PLAN: what provision.sh would create (no changes)
	scripts/aws/provision.sh

provision-apply: ## Create ECR/log-groups/cluster + register task defs
	scripts/aws/provision.sh --apply

test: test-api test-runner test-pipeline ## Run all test suites

test-api: ## API unit tests
	cd api && $(PY) -m pytest -q --import-mode=importlib

test-runner: ## Runner unit tests
	$(PY) -m pytest runner/tests -q

test-pipeline: ## Engine / pipeline unit tests
	pixi run pytest tests/ -q
