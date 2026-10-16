# homing: one-command setup, run, and verification.
SHELL := /bin/bash
UV    := uv run
INPUTS ?= inputs/local/github-v0.1.0.json
RECIPE ?= github
HEADED ?=
MOCK_PORT ?= 8080
TEMPLATES := src/homing/web/templates mock_portal/templates

.DEFAULT_GOAL := help
.PHONY: help setup bootstrap clean-drafts run serve break corrupt-state \
	mock-portal mail up down demo lint fix typecheck hooks test test-unit \
	test-offline test-mock test-leaks verify-live schema

help:  ## Show targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) \
		| awk -F':.*## ' \
			'{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup:  ## Install deps + Chromium, create .env from the example
	uv sync
	$(UV) playwright install chromium
	$(UV) prek install
	@test -f .env || { \
		key=$$($(UV) homing keygen); \
		sed "s|^SESSION_KEY=$$|SESSION_KEY=$$key|" .env.example > .env; \
		echo "→ created .env with a fresh SESSION_KEY;" \
			"fill in the GITHUB_* values"; }

bootstrap:  ## Create private OWNER/homing-scratch + releases (uses gh)
	$(UV) python tools/bootstrap_github.py bootstrap

clean-drafts:  ## Delete draft releases created by runs
	$(UV) python tools/bootstrap_github.py clean-drafts

run:  ## Run a recipe: make run [RECIPE=github] [INPUTS=...] [HEADED=1]
	$(UV) homing run -r $(RECIPE) -i $(INPUTS) $(if $(HEADED),--headed,)

break:  ## Deliberately broken recipe → failure evidence bundle
	$(UV) python tools/verify_live.py break

corrupt-state:  ## Overwrite the stored session with garbage
	@mkdir -p state
	@echo "garbage" > state/$(RECIPE)__default.json.enc
	@echo "corrupted state/$(RECIPE)__default.json.enc"

mail:  ## Start GreenMail (SMTP 3025 / IMAP 3143) for the mock portal
	docker compose up -d greenmail

mock-portal: mail  ## Run the mock payer portal on :$(MOCK_PORT)
	ACME_MFA=email $(UV) uvicorn mock_portal.app:app --port $(MOCK_PORT)

demo:  ## Re-record docs/demo.gif (needs vhs; run make mock-portal first)
	vhs docs/demo.tape

serve:  ## Open the dashboard on :8000 (also runs queued runs)
	$(UV) homing serve

up:  ## docker compose up (test infra)
	docker compose up -d --build

down:  ## docker compose down
	docker compose down

lint:  ## Every check, changes nothing (style, types, deps, docs, CI)
	$(UV) ruff check .
	$(UV) ruff format --check .
	$(UV) ty check
	$(UV) vulture
	$(UV) deptry .
	$(UV) typos
	$(UV) djlint $(TEMPLATES) --check
	$(UV) djlint src/homing/web/templates --lint
	$(UV) yamllint --strict .
	$(UV) mdformat --check README.md
	$(UV) ec
	$(UV) validate-pyproject pyproject.toml
	$(UV) pyproject-fmt --check pyproject.toml
	$(UV) zizmor --quiet .github/workflows
	$(UV) actionlint
	uv lock --check

fix:  ## Apply every auto-fix (then run `make lint`)
	$(UV) ruff check --fix .
	$(UV) ruff format .
	$(UV) djlint $(TEMPLATES) --reformat --quiet || true
	$(UV) pyproject-fmt pyproject.toml || true
	$(UV) typos --write-changes
	$(UV) mdformat README.md

typecheck:  ## ty (Astral)
	$(UV) ty check

hooks:  ## Install git hooks (prek runs .pre-commit-config.yaml)
	$(UV) prek install

schema:  ## Export recipes/schema.json
	$(UV) homing schema

test-unit:  ## Unit + dashboard route tests
	$(UV) pytest tests/unit tests/web -q

test-offline:  ## Resolver vs saved fixtures + mutated variants (no network)
	$(UV) pytest tests/offline -q

test-mock:  ## Full runs vs the mock portal (+ GreenMail via docker)
	$(UV) pytest tests/mock -q

test-leaks:  ## Plant secrets, run failing flows, grep every output: zero hits
	$(UV) pytest tests/leaks -q

test: lint  ## Everything (no GitHub account needed), with coverage
	$(UV) pytest tests --cov --cov-report=term --cov-report=xml

verify-live:  ## Real GitHub: the evaluation table end to end
	$(UV) python tools/verify_live.py
