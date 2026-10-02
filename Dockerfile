FROM node:24-bookworm-slim AS frontend

WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app \
    && install -d -o app -g app /app /data /data/uploads /data/out /mcp-files

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
RUN install -d -m 0755 /opt/tiktoken-cache \
    && python -c 'import tiktoken; [tiktoken.get_encoding(name) for name in tiktoken.list_encoding_names()]'

COPY app/ ./app/
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh
COPY --from=frontend /src/frontend/dist ./frontend/dist
RUN chmod 0555 /app/scripts/docker-entrypoint.sh \
    && chown -R app:app /app

USER app

EXPOSE 8000 8001
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["web"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8000/healthz
