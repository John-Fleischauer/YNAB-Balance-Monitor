# Deployment

## How it deploys

**Mode A: a Dockhand git stack.** The stack tracks `docker-compose.yml` on `main`, and has
**Force redeploy** and **Re-pull images** turned on.

1. A merge to `main` runs `.github/workflows/ci.yml`: checks and the dependency audit, then
   `image` builds `linux/amd64` and pushes `ghcr.io/john-fleischauer/ynab-balance-monitor` as
   `:main` and `:sha-<7 chars>`, with the commit in the `org.opencontainers.image.revision`
   label and in the `GIT_REVISION` build arg.
2. `deploy` calls the stack's webhook (a `GET` with `?secret=`) and fails unless the reply says
   `success: true`. With Force redeploy and Re-pull on, that means the container was recreated
   from the new image; compose also has `pull_policy: always`.

A docs-only push (`docs/**`, `*.md`) doesn't rebuild or redeploy. `workflow_dispatch` on `main`
rebuilds and redeploys by hand.

## Host

- One self-hosted Docker host, `linux/amd64`.
- **No inbound exposure:** no published ports, no reverse proxy. The app serves nothing.
- **Outbound only:** the YNAB API (`api.ynab.com`) and whatever notification services the
  Apprise URLs name.
- Runs as the image's non-root `app` user (uid 10001). It writes only `/tmp/heartbeat`, inside
  the container; there are no volumes.

## Checking what's running

The app has no HTTP, so there's no `/api/health` and the deploy job can't check the revision
(`docs/DECISIONS.md`). After a deploy, on the host:

```sh
# The commit the image was built from: should be the merged commit on main.
docker inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' <container>

# The same commit, as the app reports it at startup.
docker logs <container> 2>&1 | grep 'starting, revision'

# Not root (uid 10001), and healthy once the scheduler has written its heartbeat.
docker exec <container> id
docker inspect -f '{{ .State.Health.Status }}' <container>
```

The startup line is printed before the configuration is checked, so a container that refuses
its config still logs what it is running, followed by one `Error:` line per problem.

## Variables

The stack supplies them through Dockhand's stack environment, which compose interpolates into
the `environment:` list (`docker-compose.yml`). There's no `env_file`: a variable the compose
file doesn't list never reaches the container. A missing required variable stops `compose up`
with `set <NAME>`. The app validates the rest at boot (`Settings` in `monitor.py`) and exits 1
with a readable error, never printing a value. A blank value counts as unset.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `YNAB_API_TOKEN` | **yes** (compose guard) | | YNAB personal access token. Secret |
| `APPRISE_URLS` | **yes** (compose guard) | | Apprise URLs for alerts, separated by commas, semicolons or spaces. Each must load in Apprise at boot. Secret: they carry credentials |
| `UPDATE_APPRISE_URLS` | no | `APPRISE_URLS` | Apprise URLs for routine updates, same format and boot check. Secret |
| `YNAB_ACCOUNT_ID_CC` | one account form | | `id:threshold,id2:threshold`; CC payments counted. Threshold in whole dollars, default 0 |
| `YNAB_ACCOUNT_ID_NO_CC` | one account form | | Same format; CC payments not counted |
| `YNAB_ACCOUNT_ID` | one account form | | Older form: one account ID, CC payments counted |
| `MIN_BALANCE` | no | `0` | Threshold for `YNAB_ACCOUNT_ID`, whole dollars; ignored with the newer form |
| `YNAB_BUDGET_ID` | no | `last-used` | Budget to read |
| `YNAB_CC_CATEGORIES` | no | all | Comma-separated credit card payment category IDs or names |
| `MONITOR_DAYS` | no | end of month | Days to project ahead, 0 or more |
| `SCHEDULE` | no | run once | `HH:MM` daily (24h) or `Nh` every N hours (0.1 to 8760) |
| `UPDATE_SCHEDULE` | no | every check | Same format; when routine updates go out |
| `TZ` | no | UTC | Time zone for daily schedules, e.g. `America/New_York` |
| `APP_IMAGE` | no | `…:main` | Read by compose only: pins another image tag (rollback) |

Accounts: set `YNAB_ACCOUNT_ID_CC` and/or `YNAB_ACCOUNT_ID_NO_CC`, **or** `YNAB_ACCOUNT_ID`;
both forms together, neither, or a malformed entry fails the boot. The stack runs with a
`SCHEDULE`: the healthcheck reads the heartbeat that only the scheduling loop writes.

The image sets `GIT_REVISION` and `PYTHONUNBUFFERED`; the stack doesn't.

## Backups

None needed. There's no database and no volume: the container's only file is the heartbeat,
which is rewritten every minute. Everything the app works from lives in YNAB, and its
configuration is the stack environment in Dockhand and this repo.

## Rollback

Preferred: revert the bad commit on `main` in a PR. Its merge builds and deploys the
previous code the normal way.

Faster, while the revert is on its way: in the stack environment, set
`APP_IMAGE=ghcr.io/john-fleischauer/ynab-balance-monitor:sha-<7 chars of the good commit>`
and redeploy the stack. Check the revision label as above. Remove `APP_IMAGE` once the fix is
on `main`, or every later deploy keeps running the pinned image.

## Stopping

In scheduled mode, once the scheduler is running, `docker stop` (SIGTERM) ends its wait at
once: the log says `Shutting down...` and the process exits 0. A check already running
finishes first, unless it's waiting to retry a request, which a stop ends at once with no
failure notification. Compose gives the stop 45 s (`stop_grace_period`), not Docker's default
10 s, because one YNAB request can take up to 30 s. In run-once mode a stop ends the check where it is: the log says `Stopped
before the check finished`, the process exits 143 and no failure notification goes out (a
notification already being sent finishes or times out first: Apprise sends from a thread). A
stop during startup takes Python's default action (exit 143, no log line); that's not a
fault.

## When a check fails

A network error, or HTTP 5xx or 429 from YNAB, is retried within the check: after 60 s, then
after 300 s, each retry logged (`<reason>; retrying in 60 s`). The two waits are shared by the
whole check, so a check never waits more than 6 minutes, inside the healthcheck's 10-minute
heartbeat window. Any other 4xx (a revoked token is 401) or an unexpected reply fails at once.

A failed check (retries used up, another YNAB error, or a bug) ends that check, not the
container. The log gets one `Check failed: <reason>` line. In scheduled mode the loop then
waits for the next run; run-once mode exits 1 instead.

Only the first failure in a row is notified: the `APPRISE_URLS` services get a "YNAB Balance
Monitor failed" notification with the reason. Later failures only log `Check still failing
(<n> in a row); not notified again`. The first check that finishes after them sends one "YNAB
Balance Monitor working again" notification. The count lives in memory: a restart starts
again, so the first failure after a restart is notified even if the one before it was too. If
a failure or recovery notification can't be sent, the log says `Could not send the failure
notification` (or `recovery`) and the loop carries on.

Each YNAB request gives up after 30 s without a reply (`YNAB_TIMEOUT` in `monitor.py`), so a
hung connection fails the check instead of blocking it. Apprise's services use Apprise's own
timeouts (4 s to connect and per read).

No log line or notification of the app's own holds the YNAB token, an Apprise URL or any part
of one, or an account or budget ID. An Apprise URL that doesn't load fails the boot, named
by its variable and entry number only (`Error: APPRISE_URLS: entry 2 doesn't load in Apprise;
check its format`); entries are counted after splitting on commas, semicolons and
whitespace, as Apprise splits them, with blanks skipped. Apprise's logging is off while the
URLs are checked, since it logs a URL it can't load. In the log lines of Apprise
and other libraries, the token, each URL and each part of a URL 6 characters or longer are
masked as `***`, tracebacks included; a shorter part (a 5-character password) isn't.
