# uv from its official image, pinned. A named stage, so Dependabot's docker
# ecosystem reads it as a FROM line and bumps it.
FROM ghcr.io/astral-sh/uv:0.13.8 AS uv

FROM python:3.13-slim

COPY --from=uv /uv /bin/uv

# Unbuffered, so log lines reach `docker logs` as they are written.
# Use the image's Python, never a downloaded one.
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so a code-only change reuses this layer.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY monitor.py .

ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "monitor.py"]
