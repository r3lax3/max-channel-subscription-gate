# Сборка из корня репозитория: зависимости ставятся строго по uv.lock.
FROM ghcr.io/astral-sh/uv:0.8.22-python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY gatebot/ ./gatebot/
WORKDIR /app/gatebot
ENV PATH="/app/.venv/bin:$PATH"

CMD ["sh", "-c", "alembic upgrade head && exec python main.py"]
