.PHONY: install migrate run-api run-admin run-worker run-beat run-tests lint

install:
	uv sync --frozen --extra dev

migrate:
	uv run --frozen alembic upgrade head
	uv run --frozen python admin_panel/manage.py migrate --noinput

run-api:
	uv run --frozen uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload

run-admin:
	uv run --frozen python admin_panel/manage.py runserver 127.0.0.1:8001

run-worker:
	uv run --frozen celery -A api.core.celery_app.celery_app worker -Q invoices --concurrency=2 --loglevel=info

run-beat:
	mkdir -p .local/state
	uv run --frozen celery -A api.core.celery_app.celery_app beat --schedule=.local/state/celerybeat-schedule

run-tests:
	uv run --frozen --extra dev pytest
	uv run --frozen --extra dev python admin_panel/manage.py test devices

lint:
	uv run --frozen --extra dev ruff check .
	uv run --frozen --extra dev ruff format --check .
