FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

ARG PACKAGE_INDEX_URL=https://pypi.org/simple

COPY pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir --index-url "$PACKAGE_INDEX_URL" uv \
    && UV_DEFAULT_INDEX="$PACKAGE_INDEX_URL" uv sync --frozen --no-dev

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY web ./web
