.PHONY: test test-slow test-live lint fmt typecheck docker-maybe check

test:
	uv run pytest

test-slow:
	uv run pytest -m slow

test-live:
	uv run pytest -m live

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy src

docker-maybe:
	@if [ -f Dockerfile ] && docker info >/dev/null 2>&1; then \
		docker build -t satudatascape . ; \
	else \
		echo "skip docker: no Dockerfile or no daemon"; \
	fi

check: lint typecheck test docker-maybe
