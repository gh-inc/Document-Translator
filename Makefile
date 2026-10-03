.PHONY: setup dev frontend-dev test test-live lint format typecheck up down build logs mcp-share-check

setup:
	uv sync
	uv run pre-commit install

dev:
	uv run uvicorn app.api.main:app --reload --port 8000

frontend-dev:
	npm --prefix frontend run dev

test:
	uv run pytest -m "not live"

test-live:
	uv run pytest -m live

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy app

up:
	docker compose up --build -d

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f

# Read-only host permission check for the MCP shared directory.
mcp-share-check:
	./scripts/prepare-mcp-share.sh
