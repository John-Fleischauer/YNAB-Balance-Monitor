# YNAB Balance Monitor

A Python service that projects the minimum balance of one or more YNAB accounts through the end
of the month (or `MONITOR_DAYS`) and notifies through Apprise: an alert below a threshold,
routine updates, and a notice when a check fails. One file, `monitor.py`; Python 3.13 with uv;
runs as a long-lived container with its own scheduler, or once and exits. Follows
https://github.com/ki4hrg/standards (Python service profile). The `standards` plugin is enabled
in `.claude/settings.json`; its `ship` skill is how work lands. How it works:
`docs/ARCHITECTURE.md` · deploy and variables: `docs/DEPLOYMENT.md` · decisions:
`docs/DECISIONS.md`.

## Commands

- `uv sync`: the dev environment in `.venv`.
- `uv run ruff check && uv run ruff format --check && uv run pytest`: all three pass before
  anything is called done. CI runs the same.
- One check against YNAB: `uv run --locked --env-file .env monitor.py`, with `.env` copied from
  `.env.example` and `SCHEDULE` and `UPDATE_SCHEDULE` left empty. It sends real notifications.

## Rules that are easy to break

- **Heartbeat.** Scheduled mode writes `/tmp/heartbeat` as the loop starts, from the sleep
  loop and from retry waits (never from the rest of `run_cycle`), read by the compose healthcheck (10 min window).
  A retry wait is a planned pause of up to 6 min a check, so it keeps the heartbeat fresh; a
  hung network call must still let it go stale. Run-once mode exits and has no heartbeat.
- **Unbuffered output.** The Dockerfile keeps `ENV PYTHONUNBUFFERED=1`: without it, `print`
  output doesn't reach `docker logs` until a buffer fills.
- **Guards.** The app has no inbound entry points; its inputs are the environment and YNAB's
  replies, and the app's own variables are validated by `Settings` at boot.

## Layout

- `monitor.py`: everything, in sections: log redaction, `Settings`, YNAB API (`ynab_get`,
  retries), projection, notifications, `run_guarded_cycle`, scheduling and `main()`
- `tests/`: pytest, YNAB and Apprise mocked or on localhost · `Dockerfile` · `docker-compose.yml` (the Dockhand
  stack's file) · `.env.example`: every variable
- `.github/workflows/ci.yml`: the standards' caller · `docs/plans/`: the standards sweep
- `docs/claude-candidates.md`: proposed rules, not binding

## Exceptions to the standards

Recorded in `docs/DECISIONS.md`: `monitor.py` stays a single file at the root; no
`/api/health` (no HTTP; the revision is checked by hand); the compose file keeps the name
`docker-compose.yml`; unversioned.
