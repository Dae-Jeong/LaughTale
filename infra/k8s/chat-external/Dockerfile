# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.14.7
FROM python:${PYTHON_VERSION}-slim-trixie AS python-base
FROM python-base AS build
ARG SERVICE
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY contracts/platform-simulator/pyproject.toml contracts/platform-simulator/README.md ./contracts/platform-simulator/
COPY contracts/platform-simulator/src ./contracts/platform-simulator/src
COPY services/${SERVICE}/pyproject.toml services/${SERVICE}/uv.lock services/${SERVICE}/README.md ./services/${SERVICE}/
COPY services/${SERVICE}/src ./services/${SERVICE}/src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --project services/${SERVICE} --locked --no-dev --no-editable

FROM python-base
ARG SERVICE
WORKDIR /app
COPY --from=build /app/services/${SERVICE}/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
# The deployment selects the native python -m entrypoint. No startup migrations.
