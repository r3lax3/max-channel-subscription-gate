FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    "aiohttp>=3.9" \
    "sqlalchemy[asyncio]>=2.0" \
    "asyncpg>=0.29" \
    "alembic>=1.13" \
    "pydantic-settings>=2.2"

COPY . .

CMD ["sh", "-c", "alembic upgrade head && python main.py"]
