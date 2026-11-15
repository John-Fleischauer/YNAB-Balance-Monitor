# Architecture

How the monitor is built. Deploy, variables and operations: `docs/DEPLOYMENT.md`. Why things
are the way they are: `docs/DECISIONS.md`.

## Shape

- **One module:** `monitor.py` at the repo root holds everything (a recorded exception to the
  profile's `src/` layout, `docs/DECISIONS.md`). Runtime dependencies are `apprise` (notifications)
  and `pydantic-settings` (config); YNAB is called with the standard library (`urllib`, `json`).
- **One process, no server:** it serves no HTTP, has no database and writes no file but the
  heartbeat. It either runs one check and exits, or runs as a long-lived container with its own
  scheduler.
- **Tests:** `tests/` with pytest, YNAB and Apprise mocked or served on localhost
  (`test_monitor.py`: projection, recurrences, accounts, schedules and config;
  `test_errors.py`: errors, retries, notifications, redaction, boot URL checks and the
  heartbeat; `test_shutdown.py`: signals, with a real subprocess).

Sections of `monitor.py`, in order: log redaction, configuration (`Settings`), YNAB API helpers
(`ynab_get`, `YNABError`, retries), core logic (recurrences, CC payments, projection),
notification, the cycle and its error handler, scheduling and `main()`.

## Inputs and outputs

There are no inbound entry points, so there is nothing to authenticate. What comes in:

| Input | Where it's checked |
|---|---|
| Environment variables | `Settings`, at boot; the app refuses to start on any problem (below) |
| YNAB API replies | `ynab_get`: non-JSON or a reply without `data` is a `YNABError`; any other surprise in a reply fails the cycle through `run_guarded_cycle` |
| SIGTERM / SIGINT | `_Stop` (scheduled mode) or `_raise_stopped` (run-once mode) |

What goes out: HTTPS `GET`s to `https://api.ynab.com/v1` with the token in the `Authorization`
header, Apprise notifications, and log lines on stdout and stderr.

## The cycle

`run_cycle(accounts, send_update)` is one check of every configured account:

1. Works out the end date: today plus `MONITOR_DAYS`, or the last day of the current month.
   "Today" is the container's local date, so `TZ` matters.
2. Fetches the budget's scheduled transactions, once for all accounts.
3. If any account counts CC payments, fetches the budget's accounts and categories, once, and
   works out the CC payments (below).
4. For each account: fetches its balance and name, expands its scheduled transactions over
   the window, projects the minimum, alerts if the minimum is below the account's threshold,
   and sends an update if `send_update` is set.

A cycle makes `1 + N` YNAB requests for N accounts, plus 2 when CC payments are counted.
YNAB allows 200 requests an hour per token, which is why an interval schedule can't be shorter
than 0.1 hours.

YNAB amounts are milliunits (1 dollar = 1000); `milliunits_to_dollars` converts them on the
way in, and everything after that is in dollars.

## The projection

### Recurrences

YNAB returns only the next occurrence of each scheduled transaction (`date_next`, or
`date_first` if that's missing). `get_scheduled_transactions` keeps the account's transactions
that aren't deleted and `_expand_occurrences` turns each into every date it falls on between
today and the end date, inclusive:

- `daily`, `weekly`, `everyOtherWeek`, `every4Weeks`: fixed steps of days or weeks.
- `monthly`, `everyOtherMonth`, `every3Months`, `every4Months`, `twiceAYear`, `yearly`,
  `everyOtherYear`: steps of months by `_add_months`, which clamps to the last day of a shorter
  month. Each step starts from the previous, clamped date, so a 31st drifts after February
  (Jan 31, Feb 28, Mar 28); issue #12.
- `twiceAMonth`: approximated as two days a month, the next date's day and that day + 15
  (or − 15 if it's after the 15th), clamped to each month's length. It starts a month back,
  so it can also give a date between today and `date_next`.
- `never`, or a frequency it doesn't know: one occurrence, if it falls in the window.

### The minimum

`project_minimum_balance` starts from the account's current balance, takes off the CC
payments not already scheduled (below), then walks day by day from today to the end date,
adding each day's scheduled amounts. It returns the lowest balance it saw and its date (the
first such day), not the balance at the end. If nothing goes lower, the minimum is today's
balance after the CC payments.

### Credit card payments

A YNAB "Credit Card Payments" category holds money already set aside to pay a card, which will
leave the paying account even if no payment is scheduled. `get_cc_payment_amounts`:

- reads the categories in the group named exactly `Credit Card Payments`, skipping deleted
  and hidden ones, and, if `YNAB_CC_CATEGORIES` is set, any whose ID or name isn't listed;
- matches each category to an open, undeleted credit card account **by name** (YNAB names
  them the same), and keeps those with a positive balance.

Then, per account that counts CC payments, `project_minimum_balance` subtracts what is
already scheduled: each scheduled transaction in the window whose transfer account is one of
those cards covers up to its amount (either sign) of that card's payment. What's left is applied **today**,
on the conservative assumption that it could leave at any time. Scheduled transfers are
still applied on their own dates as ordinary transactions.

`YNAB_ACCOUNT_ID_CC` accounts count CC payments; `YNAB_ACCOUNT_ID_NO_CC` accounts don't (for
an account that doesn't pay the cards). Every `_CC` account counts the same full set of card
payments, less its own scheduled transfers: with two, each is projected as if it paid them all.

## Accounts and thresholds

`parse_account_configs` builds the list from `YNAB_ACCOUNT_ID_CC` and `YNAB_ACCOUNT_ID_NO_CC`
(`id:threshold` entries, comma-separated, threshold in whole dollars, 0 if omitted), or from
the older `YNAB_ACCOUNT_ID` with `MIN_BALANCE` (one account, CC payments counted). Using both
forms, neither, or a malformed entry is a boot error; `MIN_BALANCE` is ignored with the newer
form. An account's name comes from YNAB on each cycle.

An account alerts when its projected minimum is **below** its threshold; exactly at the
threshold doesn't.

## Notifications

All through Apprise, by `_send_notification`: the URLs are loaded fresh for each send, and a
send that fails is tried once more after 5 s.

| Notification | When | To | Apprise type |
|---|---|---|---|
| "YNAB Balance Alert" | Every check whose projected minimum is below the threshold, per account. Not deduplicated: each check that's still below alerts again | `APPRISE_URLS` | `failure` if the minimum is negative, else `warning` |
| "YNAB Balance Update" | When the cycle is asked for one (below), per account, whatever the balance | `UPDATE_APPRISE_URLS`, else `APPRISE_URLS` | `warning` if below the threshold, else `success` |
| "YNAB Balance Monitor failed" | A failed cycle, first of a run (below) | `APPRISE_URLS` | `failure` |
| "YNAB Balance Monitor working again" | The first cycle that finishes after a notified failure | `APPRISE_URLS` | `success` |

When updates go out:

- with `UPDATE_SCHEDULE` set: on its schedule (each such run is a full check, alerts
  included);
- without it, in scheduled mode: with every check;
- in run-once mode: never. Run-once only alerts.

## Errors, retries and the failure notice

`ynab_get` turns every failed request into a `YNABError` whose message is safe to log and
send: it never holds the token, the request path (it has the budget and account IDs) or the
reply body; only YNAB's short error name, such as `unauthorized`. Each request gives up
after 30 s without a reply, to connect or on any one read (`YNAB_TIMEOUT`).

- **Retried:** a network error, HTTP 5xx or 429. Retries wait 60 s, then 300 s
  (`RETRY_WAITS`), from one 6-minute budget shared by the whole cycle (`RETRY_BUDGET`). A 429
  with a usable `Retry-After` waits that long instead, unless it asks for more than 300 s or
  more than the budget has left, which fails the cycle at once.
- **Not retried:** any other 4xx, or a reply that isn't what YNAB sends.

`run_guarded_cycle` is the one place a cycle's errors are handled. It resets the retry budget,
runs the cycle and catches any `Exception`. A `YNABError` is reported by its message; anything
else by its type and where it was raised, never its message, which could quote data. It logs
`Check failed: <reason>` and returns, so the scheduled loop carries on. Notifying:

- the first failure in a row is notified;
- later ones of the same kind (`network`, `HTTP 401`, an exception type) only log;
- a new kind is notified again. Only a notice that was sent records its kind, so after one
  that couldn't be sent, the next failure is notified unless it's the kind last notified;
- the first cycle that finishes after a notified failure sends the recovery notice.

The count lives in memory, so a restart starts over. A failed notice is logged and never ends
the loop. Exact log lines: `docs/DEPLOYMENT.md`, When a check fails.

## Scheduling

`SCHEDULE` (checks) and `UPDATE_SCHEDULE` (updates) each take `HH:MM` (daily, 24h, local time)
or `Nh` (every N hours, 0.1 to 8760). `_parse_schedule` validates them at boot.

- **Both empty:** run-once mode. One guarded cycle, then exit 0 if it finished, 1 if it failed.
- **Either set:** scheduled mode. An interval schedule fires at once on startup; a daily one
  waits for its first time. The loop sleeps until the sooner of the two next times, then runs
  one cycle: an update is sent if the update schedule is due, or if a check is due and there's
  no `UPDATE_SCHEDULE`. When both are due together, it's one cycle. With only
  `UPDATE_SCHEDULE`, checks happen only on its runs.

The next run is computed from when the cycle ends, so interval runs drift by the cycle's
length.

### Stopping

- **Scheduled mode:** `_Stop` turns SIGTERM and SIGINT into a flag and wakes the wait at once
  (a wakeup pipe). A cycle in progress makes no further YNAB request: the request in flight
  finishes, and the cycle ends at its next request (or at once if it's in a retry wait),
  without a failure notice. Work between requests, such as an account's alert, still
  happens. Then the loop logs
  `Shutting down...` and exits 0.
- **Run-once mode:** the signal raises `_Stopped` (a `BaseException`, so the cycle's handler
  doesn't catch it) wherever the cycle is, even mid-request; the process exits 128 + the
  signal number with no failure notice.

Compose's `stop_grace_period: 45s` covers a request in flight running into its 30 s
timeout.

## Liveness

In scheduled mode the app writes `/tmp/heartbeat`:

- once as the loop starts;
- at least once a minute while it waits for the next run;
- in a retry wait, at its start and at least once a minute (`_heartbeat_wait`).

Nothing else in a cycle writes it. The compose healthcheck fails if the file is older than
10 minutes. A retry wait is a planned pause (up to 6 minutes a check), so it keeps the
heartbeat fresh; a hung request must not, so that a stuck cycle shows as unhealthy. Run-once
mode writes no heartbeat and the healthcheck doesn't apply. There's no `/api/health`; the
revision is checked by hand (`docs/DECISIONS.md`, `docs/DEPLOYMENT.md`).

## Configuration and boot

`main()` first prints `YNAB Balance Monitor starting, revision <GIT_REVISION>`, so the log
says what's running even if the config is then refused.

`Settings` (`pydantic-settings`) reads each variable by its name (case-insensitively):

- `env_ignore_empty`: a blank variable counts as unset, since compose passes every optional
  one as `${VAR:-}`;
- the token and both URL lists are `SecretStr`, kept out of reprs and errors;
- validators: the token isn't blank; `APPRISE_URLS` holds at least one URL and each entry
  loads in Apprise; `UPDATE_APPRISE_URLS` the same if set; `MONITOR_DAYS` is a whole number,
  0 or more; both schedules parse; and the account variables build a non-empty list.

`load_settings` reports every problem in one boot, one `Error:` line each, and exits 1. No
secret's value is printed (pydantic's input is left out); a bad schedule's message does
quote the schedule. It runs the account check itself when another field failed,
since pydantic skips it then. `_apply` copies the validated values into the module-level
names the cycle reads and installs the log redaction.

A variable reaches the container only if `docker-compose.yml` lists it under `environment:`:
there's no `env_file`. A new variable goes into `Settings`, that list (`${VAR:?…}` if required,
`${VAR:-}` if not), `.env.example` and the table in `docs/DEPLOYMENT.md`.

## Keeping secrets out of logs

- The app's own messages name an Apprise URL by its variable and entry number, never by any
  part of it. Entries are counted as Apprise's own parser splits the list (`_split_urls`).
- While URLs are checked at boot, logging is switched off, because Apprise logs a URL it can't
  load in full.
- `_RedactSecrets`, on the root log handlers, masks the token, each URL and each part of a
  URL of 6 characters or more as `***` in every log record, tracebacks included. That covers
  Apprise's and other libraries' log lines.
- A failed cycle is reported by a `YNABError`'s safe message, or by an exception's type and
  location only (`_describe_failure`; Errors, above).

## Container

`Dockerfile`: `python:3.13-slim` with uv copied from its official image, dependencies from
`uv.lock` (`uv sync --locked --no-dev`), non-root user `app` (uid 10001), `PYTHONUNBUFFERED=1`
so `print` output reaches `docker logs` as it's written, and `GIT_REVISION` from the build
arg. It runs `python monitor.py`.
