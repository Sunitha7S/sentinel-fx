# One image, many entrypoints (see docs/07). In M0-M1 the only entrypoint is the CLI.
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
# Quality trust root (ADR 0015 §4): the reviewed configurations and approval records are
# baked into the image, owned by root and read-only to the runtime user. The build fails
# unless they have the trust root pinned in the source; nothing is mounted or read from the
# host at run time. No test tooling is installed, so fixture registries authorise nothing.
COPY config/quality ./config/quality
RUN uv run --no-sync sentinel quality-registry
RUN useradd --system --uid 10001 sentinel
USER sentinel
ENTRYPOINT ["uv", "run", "--no-sync", "sentinel"]
