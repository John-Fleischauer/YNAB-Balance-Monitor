# YNAB Balance Monitor

Projects the lowest balance each of your [YNAB](https://www.ynab.com/) accounts will reach
by the end of the month, from its scheduled transactions and the money set aside to pay
your credit cards, and tells you through [Apprise](https://github.com/caronc/apprise)
(ntfy, Telegram, Discord, email and 100+ more) if it will drop below a threshold.

Useful if you keep most of your cash in a high-yield savings account and want early warning to
move money into checking.

## How it works

On each check, for each account:

1. Fetches its current balance and the budget's scheduled transactions, and expands each
   recurring one into every date it falls on before the end of the month (or `MONITOR_DAYS`).
2. For accounts that pay your credit cards: fetches the balances of your credit card payment
   categories. Card payments already scheduled as transfers are counted on their dates; the
   rest is taken off today, to be safe.
3. Walks day by day to the **lowest** projected balance, not just the balance at the end.
4. If that's below the account's threshold, sends an **alert**. It can also send a routine
   **update** with the projected minimum (below).

If a check fails (YNAB still unreachable after two retries, a revoked token), it says so in a
notification. On a schedule it tries again at the next run, and doesn't repeat the notice
while it keeps failing the same way. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Setup

### 1. YNAB token and account IDs

- In YNAB, go to [Account Settings → Developer Settings](https://app.ynab.com/settings/developer)
  and create a personal access token.
- Find each account's ID: open the account in YNAB and read it from the address bar,
  `https://app.ynab.com/<budget ID>/accounts/<account ID>`.

### 2. Notifications

`APPRISE_URLS` takes one or more [Apprise URLs](https://github.com/caronc/apprise/wiki),
separated by commas, semicolons or spaces. For example:

```bash
APPRISE_URLS=ntfy://ntfy.sh/your-topic
APPRISE_URLS=tgram://123456789:bot-token/chat-id,discord://webhook-id/webhook-token
```

Each URL must load in Apprise, or the app refuses to start and says which entry is wrong (by
number: it never prints the URL). Treat these URLs as secrets: most carry a token.

### 3. Accounts and thresholds

Each account gets a threshold in whole dollars, written `account-id:threshold` (0 if left out).
List several with commas:

```bash
# Accounts that pay your credit cards: card payments are counted against them.
YNAB_ACCOUNT_ID_CC=everyday-checking-id:500
# Accounts that don't: card payments are ignored.
YNAB_ACCOUNT_ID_NO_CC=bills-checking-id:100
```

Each `_CC` account is projected as if it paid every card's balance, so normally only one
account goes there. To count only some cards, list their payment categories by name or ID in
`YNAB_CC_CATEGORIES`.

The older form still works for a single account: `YNAB_ACCOUNT_ID=<id>` with
`MIN_BALANCE=<threshold>` (card payments counted). Use one form or the other, not both.

### 4. Schedule and updates

- `SCHEDULE`: when to check. `08:00` checks daily at 8am (in `TZ`); `6h` checks every 6 hours,
  starting at once. With both `SCHEDULE` and `UPDATE_SCHEDULE` empty, it checks once and exits.
- `UPDATE_SCHEDULE`: when to send a routine **update**, in the same format. Each update run is
  also a full check. Without it, every scheduled check sends an update as well as any alert.
  A run-once check sends only alerts.
- `UPDATE_APPRISE_URLS`: where updates go, if not to `APPRISE_URLS` (a quieter channel, say).
  Alerts and failure notices always go to `APPRISE_URLS`.

Alerts aren't deduplicated: each check that still projects below the threshold alerts again.

### 5. Configure

```bash
cp .env.example .env
# Edit .env: every variable is listed there with a comment.
```

## Running it

**With Docker** (the image is built by CI and pulled from GHCR):

```bash
# With SCHEDULE set in .env:
docker compose up -d
docker compose logs -f
```

The container runs as a long-lived service; its healthcheck expects a schedule.

**Once, without a schedule** (leave `SCHEDULE` and `UPDATE_SCHEDULE` empty):

```bash
docker compose run --rm monitor

# Or without Docker, with uv:
uv run --locked --env-file .env monitor.py
```

It exits 0 if the check finished, 1 if it failed or the configuration was refused, and 143
if stopped.

## Example output

Made-up accounts and amounts, from a scheduled check with no `UPDATE_SCHEDULE`. Only the
app's own lines are shown; Apprise also logs a line per send (`apprise: …`):

```
YNAB Balance Monitor starting, revision 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b
Check schedule: daily at 08:00
Next event in 0.5 hours
============================================================
YNAB Balance Monitor — 2026-03-02 08:00
Projecting through 2026-03-31, 2 account(s)
============================================================

Credit card payments to account for: $660.00
  Example Card                    $    520.00
  Store Card                      $    140.00

————————————————————————————————————————
Account: Everyday Checking
Current balance: $1,200.00

Scheduled transactions through 2026-03-31: 5
  2026-03-03  Rent (monthly)                            $ -1,450.00
  2026-03-06  Paycheck (everyOtherWeek)                 $  2,100.00
  2026-03-12  Transfer : Example Card (monthly)         $   -300.00
  2026-03-20  Paycheck (everyOtherWeek)                 $  2,100.00
  2026-03-20  Car Insurance                             $   -180.00

Unscheduled CC payments (applied today): $360.00

Projected minimum balance: $-610.00 on 2026-03-03

⚠ ALERT: Everyday Checking projected balance drops $1,110.00 below threshold!

Alert notification sent via Apprise

Update notification sent via Apprise

————————————————————————————————————————
Account: Bills Checking
Current balance: $400.00

Scheduled transactions through 2026-03-31: 1
  2026-03-15  Internet (monthly)                        $    -95.00

Projected minimum balance: $305.00 on 2026-03-15

✓ Bills Checking stays above $100.00 threshold.

Update notification sent via Apprise
```

The notifications from that check:

- **YNAB Balance Alert:** "Your Everyday Checking balance is projected to drop to $-610.00 by
  Mar 03, 2026. Minimum threshold: $500.00."
- **YNAB Balance Update:** "Everyday Checking projected minimum: $-610.00 on Mar 03 (through
  Mar 31, 2026). Threshold: $500.00 — below threshold."
- **YNAB Balance Update:** "Bills Checking projected minimum: $305.00 on Mar 15 (through Mar 31,
  2026). Threshold: $100.00 — on track."

## Deploying

How this repo deploys (CI, the image, the Dockhand stack, every variable, rollback, and what
the logs say when a check fails): [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## License

MIT. See [LICENSE](LICENSE).
