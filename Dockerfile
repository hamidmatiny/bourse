# One image for every BOURSE service; compose services differ only by command (ADR-0034).
FROM ghcr.io/astral-sh/uv:0.11.26 AS uv

FROM python:3.11-slim
# dnsmasq: egress-dns resolver; curl: egress-probe test service.
RUN apt-get update \
    && apt-get install -y --no-install-recommends dnsmasq curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini otel-collector.yaml ./
COPY config ./config
COPY tests/fixtures/datasets ./tests/fixtures/datasets
RUN uv sync --locked --no-dev \
    && useradd --uid 10001 --create-home bourse \
    && mkdir -p /app/data \
    && chown bourse /app/data
USER bourse
