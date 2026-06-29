# GCEX Pilot — one-command targets. Local targets need no GCP.
.PHONY: help backend backend-install backend-test openapi enable-apis venv clean demo test

PY := backend/.venv/bin/python3
PIP := backend/.venv/bin/pip
UVICORN := backend/.venv/bin/uvicorn

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## create backend virtualenv
	cd backend && python3 -m venv .venv

backend-install: venv ## install backend deps
	$(PIP) install -q -r backend/requirements.txt

backend: ## run mock lending API on :8080 (Ctrl-C to stop)
	cd backend && .venv/bin/uvicorn app:app --reload --port 8080

backend-test: ## smoke-test the backend (must be running)
	cd backend && bash smoke_test.sh

openapi: ## regenerate backend/openapi.json from the FastAPI app
	cd backend && .venv/bin/python3 -c "import json,app; print(json.dumps(app.app.openapi(), indent=2))" > openapi.json
	@echo "wrote backend/openapi.json"

enable-apis: ## enable GCP APIs (after trial-account gate; needs project set)
	bash infra/enable-apis.sh

clean: ## remove venv and caches
	rm -rf backend/.venv backend/__pycache__

demo: ## play the 5-turn live demo transcript
	./backend/.venv/bin/python3 scripts/demo.py

test: ## run backend unit tests (inr formatting + payoff math + verification gate)
	$(PIP) install -q pytest
	./backend/.venv/bin/python -m pytest tests/ -q
