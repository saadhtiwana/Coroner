# The brain image. It holds a model credential and no cluster credentials,
# which is what makes the approval gate enforceable rather than advisory.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /src
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, so a code change does not re-resolve the environment.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
RUN useradd --uid 65532 --create-home --shell /usr/sbin/nologin coroner
COPY --from=build --chown=65532:65532 /src /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app
USER 65532:65532
EXPOSE 8000
ENTRYPOINT ["uvicorn", "coroner_brain.api:app", "--host", "0.0.0.0", "--port", "8000"]
