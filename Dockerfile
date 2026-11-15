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

# Runs as this user, not root. The app writes only the heartbeat in /tmp: Apprise's
# persistent storage is off unless a path is configured, and none is, so nothing needs
# the home directory useradd creates. No other directory is handed to the user.
RUN useradd --system --create-home --uid 10001 app

WORKDIR /app

# Dependencies first, so a code-only change reuses this layer.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY monitor.py .

ENV PATH="/app/.venv/bin:$PATH"

# The commit this image was built from (image.yml passes it), logged at startup.
# Last, so a new revision doesn't invalidate the layers above.
ARG GIT_REVISION=unknown
ENV GIT_REVISION=$GIT_REVISION

USER app
CMD ["python", "monitor.py"]
