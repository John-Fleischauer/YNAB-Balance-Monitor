FROM python:3.13-slim

# uv from its official image, pinned; Dependabot's docker ecosystem bumps it.
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

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
