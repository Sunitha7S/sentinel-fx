# One image, many entrypoints (see docs/07). In M0-M1 the only entrypoint is the CLI.
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
RUN useradd --system --uid 10001 sentinel
USER sentinel
ENTRYPOINT ["uv", "run", "--no-sync", "sentinel"]
