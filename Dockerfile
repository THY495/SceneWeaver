FROM node:22-slim@sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46 AS web
WORKDIR /web
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.1 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY LICENSE ./LICENSE
COPY src/ src/
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
COPY --from=web /web/dist apps/web/dist/
COPY alembic.ini ./
COPY migrations/ migrations/
RUN useradd --uid 10001 --create-home continuum && mkdir data && chown continuum:continuum data
USER continuum
EXPOSE 8766
CMD ["sceneweaver", "serve"]
