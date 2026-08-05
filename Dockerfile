# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS web-builder
WORKDIR /build/web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

FROM python:3.12-slim-bookworm AS python-builder
WORKDIR /build
RUN python -m pip install --no-cache-dir build
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY plugins/ ./plugins/
RUN python -m build --wheel

FROM python:3.12-slim-bookworm AS runtime
ENV FASTCLAW_DATA_ROOT=/data \
    FASTCLAW_DATABASE_URL=sqlite+aiosqlite:////data/fastclaw.db \
    FASTCLAW_WEB_ROOT=/opt/fastclaw/web \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN groupadd --system --gid 10001 fastclaw \
    && useradd --system --uid 10001 --gid fastclaw --home-dir /data fastclaw \
    && install -d -o fastclaw -g fastclaw -m 0700 /data /opt/fastclaw/web
COPY --from=python-builder /build/dist/*.whl /tmp/fastclaw.whl
RUN python -m pip install --no-cache-dir /tmp/fastclaw.whl 'uvicorn>=0.35,<1.0' \
    && rm /tmp/fastclaw.whl
COPY --from=web-builder --chown=fastclaw:fastclaw /build/web/out/ /opt/fastclaw/web/
USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 18954
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18954/healthz', timeout=3).read()"]
CMD ["uvicorn", "fastclaw.app:app", "--host", "0.0.0.0", "--port", "18954", "--no-access-log"]
