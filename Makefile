# FinanceHub shortcut targets (build.md Sec. 4).
.DEFAULT_GOAL := help
.PHONY: help env infra down schema migrate revision test clean ps logs seed seed-kafka

PY := .venv/bin/python
ifeq ($(OS),Windows_NT)
	PY := .venv/Scripts/python.exe
endif

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[1m%-10s\033[0m %s\n", $$1, $$2}'

env:  ## Create the venv and install the shared foundation
	python -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r shared/requirements.txt
	@test -f .env || cp .env.example .env

infra:  ## Bring up postgres + redis + kafka and wait for healthy
	docker compose up -d postgres redis kafka
	@echo "waiting for health..."
	@until [ "$$(docker compose ps --format '{{.Health}}' postgres redis kafka | grep -c healthy)" = "3" ]; do sleep 2; done
	@docker compose ps

ps:  ## Show container status
	docker compose ps

logs:  ## Tail infrastructure logs
	docker compose logs -f postgres redis kafka

schema:  ## Apply db/schema.sql by hand (the entrypoint already does this on a fresh volume)
	docker compose exec -T postgres psql -U financehub -d financehub < db/schema.sql

migrate:  ## Run alembic migrations to head
	cd db && ../$(PY) -m alembic upgrade head

revision:  ## Autogenerate a migration: make revision m="add x"
	cd db && ../$(PY) -m alembic revision --autogenerate -m "$(m)"

test:  ## Run the test suite
	$(PY) -m pytest

seed:  ## Generate a corpus to data/seed: make seed n=2000
	$(PY) tools/seed.py --count $(or $(n),400) --sink files --out data/seed

seed-kafka:  ## Publish a corpus to the raw topic (needs `make infra`): make seed-kafka n=2000
	$(PY) tools/seed.py --count $(or $(n),400) --sink kafka --out data/seed

down:  ## Stop everything (keeps volumes)
	docker compose down

clean:  ## Stop everything and destroy the data volumes
	docker compose down -v
