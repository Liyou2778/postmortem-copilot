.PHONY: help test lint run up down logs clean

PY ?= python

help:
	@echo "make test   - run domain tests (stdlib only)"
	@echo "make lint   - ruff + compile check"
	@echo "make run    - run API locally (needs requirements installed)"
	@echo "make up     - start full stack with docker compose"
	@echo "make down   - stop stack"
	@echo "make logs   - tail core logs"

test:
	$(PY) -m unittest discover -s services/core/tests -v

lint:
	$(PY) -m compileall -q services/core/app
	-ruff check services/core

run:
	cd services/core && uvicorn app.api:app --reload --port 8000

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f core

clean:
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
	rm -f services/core/core.db
