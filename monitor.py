#!/usr/bin/env python3
"""YNAB Balance Monitor - Projects minimum account balances and alerts via Apprise."""

import calendar
import json
import logging
import math
import os
import re
import select
import signal
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote
from urllib.request import Request, urlopen

import apprise
from apprise.utils.parse import parse_urls
from pydantic import (
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# Surface Apprise's internal errors (network failures, auth errors, etc.)
logging.basicConfig(
    stream=sys.stderr, level=logging.WARNING, format="%(name)s: %(message)s"
)
logging.getLogger("apprise").setLevel(logging.INFO)


class _RedactSecrets(logging.Filter):
    """Mask the token and every part of the Apprise URLs in any log record.

    Apprise logs a URL it can't load ("Unparseable Telegram URL tgram://<bot token>/"),
    and the URLs carry their credentials. Installed on the root handlers by _apply(), so
    it sees Apprise's records (they propagate to root) as well as any other library's.
    """

    def __init__(self, secrets):
        super().__init__()
        # Longest first, so a whole URL is masked before the parts inside it.
        self._secrets = sorted(set(secrets), key=len, reverse=True)

    def _redact(self, text):
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 -- a bad log call must not raise from here
            # logging's own error report would print the raw arguments; drop them.
            message = f"{record.msg!s} (log arguments didn't match)"
        record.msg, record.args = self._redact(message), None
        # A traceback or stack is formatted after the filters run: mask it now, and
        # cache it in exc_text, which the formatter uses instead of formatting again.
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redact(record.stack_info)
        return True


def _split_urls(urls_str):
    """The entries of an Apprise URL list, split by Apprise's own parser: on commas,
    semicolons and whitespace, but only where the next entry starts with a scheme, so a
    comma inside a URL (`mailto://…?to=a@x.com,b@x.com`) stays in it. Entry numbers in
    messages count from 1 in this list."""
    return parse_urls(urls_str)


def _unloadable_entries(urls_str):
    """The numbers of the entries Apprise can't load. Apprise logs a URL it can't load,
    credentials and all, so logging is off while it tries."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        return [
            number
            for number, url in enumerate(_split_urls(urls_str), start=1)
            if not apprise.Apprise().add(url)
        ]
    finally:
        logging.disable(previous)


def _check_urls_load(urls_str):
    """ValueError naming the entries that don't load; never the URLs themselves."""
    bad = _unloadable_entries(urls_str)
    if len(bad) == 1:
        raise ValueError(f"entry {bad[0]} doesn't load in Apprise; check its format")
    if bad:
        numbers = ", ".join(str(n) for n in bad)
        raise ValueError(f"entries {numbers} don't load in Apprise; check their format")


def _secret_parts(urls_str):
    """Each Apprise URL and the parts of it that may be a credential.

    Apprise URLs put credentials anywhere after the scheme: user and password, the host
    (a Telegram bot token), path segments (a Discord webhook token) or query values.
    Parts under 6 characters are left alone, or common words in log lines would be
    masked too; Apprise may log a part percent-decoded, so both forms are listed.
    """
    parts = []
    for url in _split_urls(urls_str):
        parts.append(url)
        _, _, rest = url.partition("://")
        for piece in re.split(r"[/:@?&=#,;\s]", rest):
            # Query keys get masked too; harmless, and simpler than parsing the query.
            for form in (piece, unquote(piece)):
                if len(form) >= 6:
                    parts.append(form)
    return parts


def _install_redaction(secrets):
    """Mask these values in every record the root logger's handlers write."""
    redact = _RedactSecrets([s for s in secrets if s])
    for handler in logging.getLogger().handlers:
        for old in [f for f in handler.filters if isinstance(f, _RedactSecrets)]:
            handler.removeFilter(old)
        handler.addFilter(redact)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Set by main() from Settings once the environment has been validated.
YNAB_API_TOKEN = ""
YNAB_BUDGET_ID = "last-used"
YNAB_CC_CATEGORIES = ""  # comma-separated IDs or names, empty = all
MONITOR_DAYS = None  # None = end of current month
APPRISE_URLS = ""  # comma-separated Apprise URLs
UPDATE_APPRISE_URLS = ""  # empty = APPRISE_URLS

YNAB_BASE = "https://api.ynab.com/v1"

# Liveness marker for the container HEALTHCHECK (scheduled mode only).
HEARTBEAT_PATH = "/tmp/heartbeat"


@dataclass
class AccountConfig:
    account_id: str
    min_balance: int
    include_cc: bool
    name: str = ""  # populated at runtime from YNAB API


def parse_account_configs(cc_raw, no_cc_raw, legacy_raw, min_balance_raw):
    """Build the account list from the account variables; ValueError if they're unusable.

    New format:
      YNAB_ACCOUNT_ID_CC=id:threshold,id2:threshold   (CC payments included)
      YNAB_ACCOUNT_ID_NO_CC=id:threshold,id2           (CC payments excluded)
      Threshold defaults to 0 if omitted.

    Legacy format (only when neither new variable is set):
      YNAB_ACCOUNT_ID=id  +  MIN_BALANCE=threshold     (CC payments included)
      MIN_BALANCE is read only here, so a leftover value is ignored with the new format.
    """
    cc_raw, no_cc_raw, legacy_raw = (
        cc_raw.strip(),
        no_cc_raw.strip(),
        legacy_raw.strip(),
    )

    if legacy_raw and (cc_raw or no_cc_raw):
        raise ValueError(
            "Set YNAB_ACCOUNT_ID_CC/YNAB_ACCOUNT_ID_NO_CC or the older YNAB_ACCOUNT_ID, not both"
        )

    if legacy_raw:
        if "," in legacy_raw or ":" in legacy_raw:
            raise ValueError(
                "YNAB_ACCOUNT_ID takes one account ID, with its threshold in MIN_BALANCE; "
                "for several accounts use YNAB_ACCOUNT_ID_CC or YNAB_ACCOUNT_ID_NO_CC"
            )
        try:
            min_balance = int(min_balance_raw.strip() or "0")
        except ValueError:
            raise ValueError("MIN_BALANCE must be a whole number of dollars") from None
        return [
            AccountConfig(
                account_id=legacy_raw, min_balance=min_balance, include_cc=True
            )
        ]

    accounts = [
        AccountConfig(account_id=acct_id, min_balance=threshold, include_cc=True)
        for acct_id, threshold in _parse_account_entries(cc_raw, "YNAB_ACCOUNT_ID_CC")
    ] + [
        AccountConfig(account_id=acct_id, min_balance=threshold, include_cc=False)
        for acct_id, threshold in _parse_account_entries(
            no_cc_raw, "YNAB_ACCOUNT_ID_NO_CC"
        )
    ]
    if not accounts:
        raise ValueError(
            "No accounts configured. Set YNAB_ACCOUNT_ID_CC, YNAB_ACCOUNT_ID_NO_CC, or YNAB_ACCOUNT_ID"
        )
    return accounts


def _parse_account_entries(raw, variable):
    """Parse 'id:threshold,id2:threshold,...' into list of (account_id, min_balance) tuples.

    A malformed entry raises ValueError naming the variable and the entry's position.
    """
    entries = []
    for number, part in enumerate(raw.split(","), start=1):
        part = part.strip()
        if not part:
            continue
        acct_id, has_threshold, threshold = part.partition(":")
        acct_id = acct_id.strip()
        if not acct_id:
            raise ValueError(f"{variable} entry {number} has no account ID")
        if not has_threshold:
            entries.append((acct_id, 0))
            continue
        try:
            entries.append((acct_id, int(threshold.strip())))
        except ValueError:
            raise ValueError(
                f"{variable} entry {number}: the threshold after ':' must be a whole number of dollars"
            ) from None
    return entries


class Settings(BaseSettings):
    """The environment, validated at boot. Each field is read from its upper-case name."""

    # A blank variable counts as unset: compose passes every optional one as ${VAR:-},
    # so an unset value arrives as "" and must still get its default.
    model_config = SettingsConfigDict(env_ignore_empty=True)

    # SecretStr keeps the values out of reprs and validation errors.
    ynab_api_token: SecretStr
    apprise_urls: SecretStr
    update_apprise_urls: SecretStr | None = None

    ynab_budget_id: str = "last-used"
    ynab_cc_categories: str = ""
    # None (unset) projects through the end of the current month.
    monitor_days: int | None = Field(default=None, ge=0)
    schedule: str = ""
    update_schedule: str = ""

    ynab_account_id_cc: str = ""
    ynab_account_id_no_cc: str = ""
    ynab_account_id: str = ""
    # A string: parsed with YNAB_ACCOUNT_ID only, as parse_account_configs says.
    min_balance: str = ""

    _accounts: list[AccountConfig] = PrivateAttr(default_factory=list)

    @field_validator("ynab_api_token")
    @classmethod
    def _token_not_blank(cls, value):
        stripped = value.get_secret_value().strip()
        if not stripped:
            raise ValueError("must not be blank")
        return SecretStr(stripped)

    @field_validator("apprise_urls")
    @classmethod
    def _has_a_url(cls, value):
        stripped = value.get_secret_value().strip()
        if not _split_urls(stripped):
            raise ValueError("must hold at least one URL")
        _check_urls_load(stripped)
        return SecretStr(stripped)

    @field_validator("update_apprise_urls")
    @classmethod
    def _no_urls_is_unset(cls, value):
        # Whitespace or bare separators: env_ignore_empty only catches "".
        if value is None or not _split_urls(value.get_secret_value()):
            return None
        stripped = value.get_secret_value().strip()
        _check_urls_load(stripped)
        return SecretStr(stripped)

    @field_validator("ynab_budget_id")
    @classmethod
    def _strip(cls, value):
        return value.strip() or "last-used"

    @field_validator("schedule", "update_schedule")
    @classmethod
    def _valid_schedule(cls, value):
        _parse_schedule(value)
        return value.strip()

    @model_validator(mode="after")
    def _build_accounts(self):
        self._accounts = parse_account_configs(
            self.ynab_account_id_cc,
            self.ynab_account_id_no_cc,
            self.ynab_account_id,
            self.min_balance,
        )
        return self

    @property
    def accounts(self):
        return self._accounts


def _describe_error(error):
    """One readable line for a settings error. Never includes the value that failed."""
    name = str(error["loc"][0]).upper() if error["loc"] else ""
    if error["type"] == "missing":
        return f"{name} is required"
    message = error["msg"].removeprefix("Value error, ")
    return f"{name}: {message}" if name else message


def load_settings():
    """Read and validate the environment; print every problem and exit 1 if it's unusable."""
    try:
        return Settings()
    except ValidationError as e:
        # include_input=False: the failing value may be a secret.
        problems = [
            _describe_error(error)
            for error in e.errors(include_input=False, include_url=False)
        ]
    # pydantic skips the account check (a model validator) when any field failed;
    # run it here too, so one boot reports every problem.
    try:
        parse_account_configs(
            *(
                os.environ.get(name, "")
                for name in (
                    "YNAB_ACCOUNT_ID_CC",
                    "YNAB_ACCOUNT_ID_NO_CC",
                    "YNAB_ACCOUNT_ID",
                    "MIN_BALANCE",
                )
            )
        )
    except ValueError as account_problem:
        if str(account_problem) not in problems:
            problems.append(str(account_problem))
    for problem in problems:
        print(f"Error: {problem}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# YNAB API helpers
# ---------------------------------------------------------------------------


# Seconds urlopen waits to connect, and then for each read, before giving up. YNAB
# normally answers in well under a second; 30 s allows a slow response but ends a hung
# connection, so the cycle fails and is reported instead of blocking until restarted.
YNAB_TIMEOUT = 30


# Waits, in seconds, before the first and second retry of a YNAB request that failed
# on the network or with HTTP 5xx/429; then the cycle fails. One budget per cycle, not
# per request, so a cycle never spends more than 6 minutes (RETRY_BUDGET) waiting. A
# 429's Retry-After replaces the wait, up to MAX_RETRY_WAIT and within the budget.
RETRY_WAITS = (60, 300)
RETRY_BUDGET = sum(RETRY_WAITS)
MAX_RETRY_WAIT = 300


class YNABError(Exception):
    """A YNAB request failed. The message is safe to log and to send in a notification:
    it never holds the token, the request URL (budget and account IDs) or the reply body.

    `retryable`: a network error or HTTP 5xx/429, which may pass. Any other 4xx, or a
    reply that isn't what YNAB sends, won't fix itself by waiting.
    """

    def __init__(self, message, retryable=False, kind="reply", retry_after=None):
        super().__init__(message)
        self.retryable = retryable
        # Seconds a 429's Retry-After asked for; None without a usable header.
        self.retry_after = retry_after
        # What went wrong, coarsely ("network", "HTTP 401"): a change of kind during
        # an outage is notified again (run_guarded_cycle).
        self.kind = kind


def _sleep(seconds):
    time.sleep(seconds)
    return False


# How a retry waits; returns True if a stop was requested during the wait. The
# scheduled loop points it at _Stop.wait, so docker stop ends a retry wait at once. In
# run-once mode time.sleep is interrupted by _Stopped from the signal handler.
_retry_wait = _sleep
# What's left of this cycle's RETRY_WAITS and of its RETRY_BUDGET in seconds;
# run_guarded_cycle refills both.
_retries_left = []
_wait_budget = 0
# True once a stop is requested (the scheduled loop points it at its _Stop): the
# check then makes no further YNAB request, so a stop doesn't wait out the whole check.
_stopping = bool


def _retry_after_seconds(value, now=None):
    """Seconds a Retry-After header asks for (delay-seconds or an HTTP date), rounded
    up; None if it's missing, neither form, or a date that isn't in the future. Only
    this number is ever logged.

    A date already past is likely clock skew between YNAB and this host; retrying at
    once would spend a retry for nothing, so it gets the normal waits instead.
    """
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        return int(value)  # any size: a huge value just fails the check at once
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:  # "-0000": an HTTP date is GMT
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    except (TypeError, ValueError, IndexError, OverflowError):
        # OverflowError: a year too large for datetime.
        return None
    return math.ceil(seconds) if seconds > 0 else None


def _ynab_error_name(http_error):
    """The error name from YNAB's JSON error body ("unauthorized"), or "" if there's none."""
    try:
        name = json.loads(http_error.read().decode())["error"]["name"]
    except (OSError, HTTPException, ValueError, KeyError, TypeError):
        return ""
    # A short identifier, by YNAB's API docs; anything else isn't printed.
    return name if isinstance(name, str) and re.fullmatch(r"[a-z_]{1,40}", name) else ""


def ynab_get(path, what):
    """GET from the YNAB API and return the reply's `data`; YNABError if that fails.

    `what` names the request in the error ("scheduled transactions"), since the path
    holds the budget and account IDs. A retryable error is retried while this cycle's
    RETRY_WAITS last; a stop requested during a wait raises _Stopped.
    """
    global _wait_budget
    while True:
        if _stopping():
            raise _Stopped(signal.SIGTERM)
        try:
            return _ynab_get_once(path, what)
        except YNABError as e:
            if _stopping():
                # Likely cut off by the stop itself: not a failure to report.
                raise _Stopped(signal.SIGTERM) from None
            if not e.retryable or not _retries_left:
                raise
            slot = _retries_left.pop(0)
            if e.retry_after is not None:
                if e.retry_after > min(MAX_RETRY_WAIT, _wait_budget):
                    # Waiting can't help within this check: fail now, not later.
                    raise YNABError(
                        f"{e}; YNAB asked for a {e.retry_after} s wait, "
                        "more than this check can wait",
                        kind=e.kind,
                    ) from None
                delay, why = e.retry_after, " (YNAB's Retry-After)"
            else:
                delay, why = min(slot, _wait_budget), ""
                if delay <= 0:
                    raise
            _wait_budget -= delay
            print(f"{e}; retrying in {delay} s{why}", file=sys.stderr)
            if _retry_wait(delay):
                raise _Stopped(signal.SIGTERM) from None


def _ynab_get_once(path, what):
    """One GET; YNABError, retryable or not, if it fails."""
    url = f"{YNAB_BASE}{path}"
    req = Request(url, headers={"Authorization": f"Bearer {YNAB_API_TOKEN}"})
    try:
        with urlopen(req, timeout=YNAB_TIMEOUT) as resp:
            return json.loads(resp.read().decode())["data"]
    except HTTPError as e:
        name = _ynab_error_name(e)
        detail = f" ({name})" if name else ""
        raise YNABError(
            f"YNAB API error fetching {what}: HTTP {e.code}{detail}",
            retryable=e.code >= 500 or e.code == 429,
            kind=f"HTTP {e.code}",
            retry_after=(
                _retry_after_seconds((e.headers or {}).get("Retry-After"))
                if e.code == 429
                else None
            ),
        ) from None
    except URLError as e:
        # e.reason is the socket error ("Name or service not known"), never the URL.
        raise YNABError(
            f"Network error fetching {what}: {e.reason}", retryable=True, kind="network"
        ) from None
    except TimeoutError:
        # A read that times out raises this directly, not wrapped in URLError.
        raise YNABError(
            f"Network error fetching {what}: no reply within {YNAB_TIMEOUT} s",
            retryable=True,
            kind="network",
        ) from None
    except (OSError, HTTPException) as e:
        # A dropped connection or a short read: the type says enough.
        raise YNABError(
            f"Network error fetching {what}: {type(e).__name__}",
            retryable=True,
            kind="network",
        ) from None
    except (ValueError, KeyError, TypeError):
        # Not JSON, or JSON without `data`: say so without echoing the body.
        raise YNABError(f"Unexpected reply from YNAB fetching {what}") from None


def milliunits_to_dollars(milliunits):
    """YNAB stores amounts in milliunits (1 dollar = 1000 milliunits)."""
    return milliunits / 1000.0


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def get_end_date():
    """Compute the projection end date.

    If MONITOR_DAYS is set, project that many days forward.
    Otherwise, project through the end of the current month.
    """
    today = datetime.now().date()
    if MONITOR_DAYS is not None:
        return today + timedelta(days=MONITOR_DAYS)
    last_day = calendar.monthrange(today.year, today.month)[1]
    return date(today.year, today.month, last_day)


def get_account_balance(account_id):
    """Get the current balance and name of the given account."""
    data = ynab_get(f"/budgets/{YNAB_BUDGET_ID}/accounts/{account_id}", "an account")
    account = data["account"]
    balance = milliunits_to_dollars(account["balance"])
    print(f"Account: {account['name']}")
    print(f"Current balance: ${balance:,.2f}")
    return balance, account["name"]


def _add_months(d, months):
    """Add months to a date, clamping to the last day of the target month."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _expand_occurrences(next_date, frequency, start, end):
    """Generate all occurrence dates of a recurring transaction within [start, end].

    next_date:  the next scheduled occurrence (from the API)
    frequency:  YNAB frequency string
    start/end:  the monitoring window bounds
    """
    # Map YNAB frequency to a delta-generating function.
    # Each function returns the next date given the current one.
    DELTAS = {
        "daily": lambda d: d + timedelta(days=1),
        "weekly": lambda d: d + timedelta(weeks=1),
        "everyOtherWeek": lambda d: d + timedelta(weeks=2),
        "every4Weeks": lambda d: d + timedelta(weeks=4),
        "monthly": lambda d: _add_months(d, 1),
        "everyOtherMonth": lambda d: _add_months(d, 2),
        "every3Months": lambda d: _add_months(d, 3),
        "every4Months": lambda d: _add_months(d, 4),
        "twiceAMonth": None,  # special case
        "twiceAYear": lambda d: _add_months(d, 6),
        "yearly": lambda d: _add_months(d, 12),
        "everyOtherYear": lambda d: _add_months(d, 24),
    }

    if frequency == "never" or frequency not in DELTAS:
        # One-time transaction — just return it if in range
        if start <= next_date <= end:
            return [next_date]
        return []

    # Special handling for twiceAMonth: YNAB schedules on the 1st & 15th
    # (or the original day and that day + ~15). We approximate by using
    # the next_date's day-of-month and that day ± 15.
    if frequency == "twiceAMonth":
        dates = []
        # Generate monthly anchors, then add both the "first" and "second" hit
        day1 = next_date.day
        day2 = day1 + 15 if day1 <= 15 else day1 - 15
        d = next_date.replace(day=1)  # start of month
        # Back up one month to make sure we don't miss anything
        d = _add_months(d, -1)
        month_end = end
        while d <= month_end:
            last_day = calendar.monthrange(d.year, d.month)[1]
            for target_day in (day1, day2):
                clamped = min(target_day, last_day)
                candidate = date(d.year, d.month, clamped)
                if start <= candidate <= end:
                    dates.append(candidate)
            d = _add_months(d, 1)
        return sorted(set(dates))

    # General case: walk forward from next_date using the delta function
    advance = DELTAS[frequency]
    dates = []
    d = next_date
    while d <= end:
        if d >= start:
            dates.append(d)
        d = advance(d)
    return dates


def fetch_all_scheduled_transactions():
    """Fetch all scheduled transactions from the budget (single API call)."""
    data = ynab_get(
        f"/budgets/{YNAB_BUDGET_ID}/scheduled_transactions", "scheduled transactions"
    )
    return data["scheduled_transactions"]


def get_scheduled_transactions(all_scheduled, account_id, end_date):
    """Filter and expand scheduled transactions for one account.

    all_scheduled: raw list from fetch_all_scheduled_transactions()
    account_id: the account to filter for
    end_date: projection end date
    """
    today = datetime.now().date()

    transactions = []
    for txn in all_scheduled:
        if txn["account_id"] != account_id:
            continue
        if txn.get("deleted", False):
            continue

        next_date = datetime.strptime(
            txn.get("date_next") or txn.get("date_first", ""), "%Y-%m-%d"
        ).date()
        frequency = txn.get("frequency", "never")
        amount = milliunits_to_dollars(txn["amount"])
        payee = txn.get("payee_name", "Unknown")
        transfer_account_id = txn.get("transfer_account_id")

        occurrences = _expand_occurrences(next_date, frequency, today, end_date)
        for occ_date in occurrences:
            freq_label = f" ({frequency})" if frequency != "never" else ""
            transactions.append(
                {
                    "date": occ_date,
                    "amount": amount,
                    "payee": payee,
                    "transfer_account_id": transfer_account_id,
                    "frequency": frequency,
                    "label": f"{payee}{freq_label}",
                }
            )

    transactions.sort(key=lambda t: t["date"])
    print(f"\nScheduled transactions through {end_date}: {len(transactions)}")
    for t in transactions:
        print(f"  {t['date']}  {t['label']:40s}  ${t['amount']:>10,.2f}")
    return transactions


def get_cc_payment_amounts(cc_categories_filter=""):
    """Get credit card payment category available balances.

    cc_categories_filter: comma-separated category IDs/names, or empty for all.
    Returns a dict of {account_id: {name, amount}} and the total amount.
    """
    # Get all accounts to identify credit card accounts and map category names
    accounts_data = ynab_get(f"/budgets/{YNAB_BUDGET_ID}/accounts", "accounts")
    cc_accounts = {}
    for acct in accounts_data["accounts"]:
        if (
            acct["type"] == "creditCard"
            and not acct.get("deleted", False)
            and not acct.get("closed", False)
        ):
            cc_accounts[acct["name"]] = acct["id"]

    # Get categories and find the Credit Card Payments group
    categories_data = ynab_get(f"/budgets/{YNAB_BUDGET_ID}/categories", "categories")

    # Parse user-specified CC categories filter
    cc_filter = set()
    if cc_categories_filter:
        cc_filter = {c.strip() for c in cc_categories_filter.split(",")}

    cc_payments = {}
    for group in categories_data["category_groups"]:
        if group["name"] != "Credit Card Payments":
            continue
        for cat in group["categories"]:
            if cat.get("deleted", False) or cat.get("hidden", False):
                continue
            # If user specified specific categories, filter
            if (
                cc_filter
                and cat["id"] not in cc_filter
                and cat["name"] not in cc_filter
            ):
                continue

            available = milliunits_to_dollars(cat["balance"])
            # Map category name back to account ID
            account_id = cc_accounts.get(cat["name"])
            if account_id and available > 0:
                cc_payments[account_id] = {
                    "name": cat["name"],
                    "amount": available,
                }

    total = sum(p["amount"] for p in cc_payments.values())
    print(f"\nCredit card payments to account for: ${total:,.2f}")
    for p in cc_payments.values():
        print(f"  {p['name']:30s}  ${p['amount']:>10,.2f}")
    return cc_payments, total


def project_minimum_balance(
    current_balance, scheduled_transactions, cc_payments, end_date
):
    """Walk day-by-day to find the minimum projected balance.

    CC payments that are already in scheduled_transactions (as transfers to CC
    accounts) are not double-counted. Any remaining CC payment amounts are
    applied on day 1 (conservative: assumes they could hit at any time).
    """
    today = datetime.now().date()

    # Identify which CC payments are already covered by scheduled transfers
    remaining_cc = dict(cc_payments)  # shallow copy of outer dict
    for txn in scheduled_transactions:
        transfer_id = txn["transfer_account_id"]
        if transfer_id and transfer_id in remaining_cc:
            # This scheduled transaction already covers (part of) the CC payment
            covered = min(remaining_cc[transfer_id]["amount"], abs(txn["amount"]))
            remaining_cc[transfer_id] = {
                **remaining_cc[transfer_id],
                "amount": remaining_cc[transfer_id]["amount"] - covered,
            }
            if remaining_cc[transfer_id]["amount"] <= 0.005:
                del remaining_cc[transfer_id]

    # Unscheduled CC payment total — apply on day 1
    unscheduled_cc_total = sum(p["amount"] for p in remaining_cc.values())
    if unscheduled_cc_total > 0:
        print(
            f"\nUnscheduled CC payments (applied today): ${unscheduled_cc_total:,.2f}"
        )

    # Build day-by-day projection
    balance = current_balance - unscheduled_cc_total
    min_balance = balance
    min_date = today

    # Group scheduled transactions by date
    txn_by_date = {}
    for txn in scheduled_transactions:
        txn_by_date.setdefault(txn["date"], []).append(txn)

    day = today
    while day <= end_date:
        if day in txn_by_date:
            for txn in txn_by_date[day]:
                balance += txn["amount"]
        if balance < min_balance:
            min_balance = balance
            min_date = day
        day += timedelta(days=1)

    print(f"\nProjected minimum balance: ${min_balance:,.2f} on {min_date}")
    return min_balance, min_date


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


def _build_notifier(urls_str, variable):
    """Build an Apprise notifier from a comma-separated URL string.

    A URL that fails to load is reported by `variable` and entry number only: the URL
    carries its credentials, so no part of it is printed.
    """
    notifier = apprise.Apprise()
    for number, url in enumerate(_split_urls(urls_str), start=1):
        if not notifier.add(url):
            print(f"Apprise: {variable} entry {number} failed to load", file=sys.stderr)
    return notifier


def _send_notification(urls_str, variable, title, message, notify_type):
    """Send a notification via Apprise with retry logic.

    `variable` names where urls_str came from, for the load-failure message.
    Returns True on success, False on failure.
    """
    notifier = _build_notifier(urls_str, variable)
    if len(notifier) == 0:
        print("Apprise: no valid notification URLs loaded", file=sys.stderr)
        return False

    for attempt in range(2):
        result = notifier.notify(title=title, body=message, notify_type=notify_type)
        # Apprise 2.x returns an AppriseResult, not a bool -- `is True` never matches
        # it, so every successful send was retried (and delivered twice).
        if result:
            return True
        if attempt == 0:
            print("Apprise: notification failed, retrying in 5s...", file=sys.stderr)
            time.sleep(5)

    print("Apprise: notification failed after retry", file=sys.stderr)
    return False


def send_alert_notification(min_balance, min_date, account_name, min_threshold):
    """Send a below-threshold alert via Apprise."""
    title = "YNAB Balance Alert"
    message = (
        f"Your {account_name} balance is projected to drop to "
        f"${min_balance:,.2f} by {min_date.strftime('%b %d, %Y')}. "
        f"Minimum threshold: ${min_threshold:,.2f}."
    )

    notify_type = (
        apprise.NotifyType.FAILURE if min_balance < 0 else apprise.NotifyType.WARNING
    )

    if _send_notification(APPRISE_URLS, "APPRISE_URLS", title, message, notify_type):
        print("\nAlert notification sent via Apprise")


def send_update_notification(
    min_balance, min_date, end_date, account_name, min_threshold
):
    """Send a routine projected-balance update via Apprise."""
    title = "YNAB Balance Update"
    status = "below threshold" if min_balance < min_threshold else "on track"
    message = (
        f"{account_name} projected minimum: ${min_balance:,.2f} on {min_date.strftime('%b %d')} "
        f"(through {end_date.strftime('%b %d, %Y')}). "
        f"Threshold: ${min_threshold:,.2f} — {status}."
    )

    if UPDATE_APPRISE_URLS:
        urls, variable = UPDATE_APPRISE_URLS, "UPDATE_APPRISE_URLS"
    else:
        urls, variable = APPRISE_URLS, "APPRISE_URLS"
    notify_type = (
        apprise.NotifyType.WARNING
        if min_balance < min_threshold
        else apprise.NotifyType.SUCCESS
    )

    if _send_notification(urls, variable, title, message, notify_type):
        print("\nUpdate notification sent via Apprise")


def send_failure_notification(reason, will_retry):
    """Tell the alert URLs that a cycle failed; True if sent. Never raises: a failure
    here is logged.

    `reason` must already be safe to send (no token, URLs or account IDs).
    """
    message = f"The balance check didn't finish: {reason}."
    if will_retry:
        message += " It runs again at the next scheduled time."
    return _send_status(
        "failure",
        "YNAB Balance Monitor failed",
        message,
        apprise.NotifyType.FAILURE,
    )


def send_recovery_notification(failures):
    """Tell the alert URLs that a cycle finished after `failures` failed ones in a row."""
    checks = "check" if failures == 1 else "checks"
    _send_status(
        "recovery",
        "YNAB Balance Monitor working again",
        f"The balance check finished again after {failures} failed {checks}.",
        apprise.NotifyType.SUCCESS,
    )


def _send_status(kind, title, message, notify_type):
    """Send a failure or recovery notice to APPRISE_URLS; True if sent. Never raises:
    a failure is logged."""
    try:
        sent = _send_notification(
            APPRISE_URLS, "APPRISE_URLS", title, message, notify_type
        )
    except Exception as e:  # noqa: BLE001 -- a failed notify must not end the loop
        # Only the type: an exception from inside Apprise may quote a URL.
        print(
            f"Could not send the {kind} notification: {type(e).__name__}",
            file=sys.stderr,
        )
        return False
    if sent:
        print(f"{kind.capitalize()} notification sent via Apprise")
    else:
        print(f"Could not send the {kind} notification", file=sys.stderr)
    return bool(sent)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_cycle(accounts, send_update=False):
    """Run one balance check cycle for all accounts.

    Fetches budget-wide data once, then checks each account.
    """
    end_date = get_end_date()

    print("=" * 60)
    print(f"YNAB Balance Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Projecting through {end_date}, {len(accounts)} account(s)")
    print("=" * 60)

    # Budget-wide fetches (done once per cycle)
    all_scheduled = fetch_all_scheduled_transactions()

    need_cc = any(a.include_cc for a in accounts)
    if need_cc:
        cc_payments, _ = get_cc_payment_amounts(YNAB_CC_CATEGORIES)
    else:
        cc_payments = {}

    for acct in accounts:
        print(f"\n{'—' * 40}")
        balance, name = get_account_balance(acct.account_id)
        acct.name = name

        transactions = get_scheduled_transactions(
            all_scheduled, acct.account_id, end_date
        )

        if acct.include_cc:
            acct_cc = cc_payments
        else:
            acct_cc = {}

        min_balance, min_date = project_minimum_balance(
            balance, transactions, acct_cc, end_date
        )

        if min_balance < acct.min_balance:
            shortfall = acct.min_balance - min_balance
            print(
                f"\n⚠ ALERT: {name} projected balance drops ${shortfall:,.2f} below threshold!"
            )
            send_alert_notification(min_balance, min_date, name, acct.min_balance)
        else:
            print(f"\n✓ {name} stays above ${acct.min_balance:,.2f} threshold.")

        if send_update:
            send_update_notification(
                min_balance, min_date, end_date, name, acct.min_balance
            )


def _describe_failure(error):
    """A one-line reason for a failed cycle that is safe to log and to send.

    A YNABError's message is built to be safe. Any other exception's message could
    quote data or a URL, so only its type and where it was raised are given.
    """
    if isinstance(error, YNABError):
        return str(error)
    frames = traceback.extract_tb(error.__traceback__)
    where = f" in {frames[-1].name}, line {frames[-1].lineno}" if frames else ""
    return f"unexpected {type(error).__name__}{where}"


def _failure_kind(error):
    """What went wrong, coarsely: a YNABError's kind, or the exception's type."""
    return error.kind if isinstance(error, YNABError) else type(error).__name__


# This process's run of failed cycles: how many in a row, and the kind of failure last
# notified (None: none notified yet). In memory: a restart starts again.
_failures_in_a_row = 0
_notified_kind = None


def run_guarded_cycle(accounts, send_update=False, will_retry=True):
    """Run one cycle; if it fails, log why, and notify unless it's already been told.

    The one place a cycle's errors are handled: a YNAB or network error, or a bug, ends
    this cycle but never the scheduling loop. In a run of failures, a notification goes
    out for the first one, and again only if the kind of failure changes (a network
    error, then HTTP 401) or the last one couldn't be sent; the rest only log, so an
    outage doesn't flood the alert channel. The first cycle that finishes after a
    notified failure sends a recovery notice. Returns True if the cycle finished.
    A stop (_Stopped, a BaseException) passes through and counts as neither.
    """
    global _retries_left, _wait_budget, _failures_in_a_row, _notified_kind
    _retries_left = list(RETRY_WAITS)
    _wait_budget = RETRY_BUDGET
    try:
        run_cycle(accounts, send_update=send_update)
    except Exception as e:  # noqa: BLE001 -- the one handler for a cycle's errors
        reason = _describe_failure(e)
        kind = _failure_kind(e)
        print(f"\nCheck failed: {reason}", file=sys.stderr)
        _failures_in_a_row += 1
        if kind != _notified_kind:
            if send_failure_notification(reason, will_retry):
                _notified_kind = kind
        else:
            print(
                f"Check still failing ({_failures_in_a_row} in a row); "
                "not notified again",
                file=sys.stderr,
            )
        return False
    if _notified_kind is not None:
        send_recovery_notification(_failures_in_a_row)
    _failures_in_a_row, _notified_kind = 0, None
    return True


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


MIN_INTERVAL_HOURS = 0.1
MAX_INTERVAL_HOURS = 8760


def _parse_schedule(schedule):
    """Parse a schedule string into a descriptor tuple; ValueError if it's malformed.

    Supported formats:
      "08:00"  - run daily at this time (24h format)
      "6h"     - run every N hours (0.1 <= N <= 8760)
      ""       - run once and exit (returns None)
    """
    s = schedule.strip()
    if not s:
        return None

    # Interval format: "6h", "12h", etc. Bounded: below 6 minutes, cycles run close to
    # back to back against YNAB's 200 requests/hour; above a year, the next run's date
    # overflows. NaN fails both comparisons.
    if s.endswith("h"):
        try:
            hours = float(s[:-1])
        except ValueError:
            hours = None
        if hours is not None and MIN_INTERVAL_HOURS <= hours <= MAX_INTERVAL_HOURS:
            return ("interval", hours * 3600)

    # Daily time format: "HH:MM"
    if ":" in s:
        try:
            hour, minute = (int(part) for part in s.split(":"))
        except ValueError:
            hour = minute = None
        if hour is not None and 0 <= hour <= 23 and 0 <= minute <= 59:
            return ("daily", hour, minute)

    raise ValueError(
        f"invalid schedule '{s}': use 'HH:MM' (24h) or 'Nh' "
        f"({MIN_INTERVAL_HOURS:g} <= N <= {MAX_INTERVAL_HOURS:g})"
    )


def touch_heartbeat():
    """Update the heartbeat file so the container HEALTHCHECK sees the scheduler as alive.

    Called from the scheduling loop and from retry waits (_heartbeat_wait), never from
    the rest of run_cycle: a retry wait is a planned pause, but a hung network call
    should let the heartbeat go stale.
    """
    try:
        Path(HEARTBEAT_PATH).touch()
    except OSError as e:
        print(f"Heartbeat: could not write {HEARTBEAT_PATH}: {e}", file=sys.stderr)


def _heartbeat_wait(stop):
    """A retry wait for the scheduled loop: ends at once on a stop, like _Stop.wait,
    and writes the heartbeat at its start and at least once a minute, so a 5-minute
    retry wait (up to 6 minutes a check) doesn't pass the healthcheck's window."""

    def wait(seconds):
        end = time.monotonic() + seconds
        touch_heartbeat()
        while (left := end - time.monotonic()) > 0:
            if stop.wait(min(left, 60)):
                return True
            touch_heartbeat()
        return stop.requested

    return wait


def _next_occurrence(schedule, after=None):
    """Return the datetime of the next occurrence of schedule after `after` (default: now)."""
    if after is None:
        after = datetime.now()
    if schedule[0] == "interval":
        return after + timedelta(seconds=schedule[1])
    else:
        _, hour, minute = schedule
        t = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if t <= after:
            t += timedelta(days=1)
        return t


def _describe_schedule(label, parsed):
    """Print a human-readable description of a parsed schedule."""
    if parsed[0] == "interval":
        print(f"{label}: every {parsed[1] / 3600:.4g} hours")
    else:
        _, hour, minute = parsed
        print(f"{label}: daily at {hour:02d}:{minute:02d}")


class _Stop:
    """SIGTERM/SIGINT as a flag, and a wait that a signal ends at once.

    The handler takes no lock (it runs in the main thread, which may hold any lock
    when the signal lands): it only sets a flag. The C-level handler also writes a
    byte to the wakeup pipe, which wakes a select() already waiting on it.
    """

    def __init__(self):
        self.requested = False
        self._read, write = os.pipe()
        os.set_blocking(self._read, False)
        os.set_blocking(write, False)
        signal.set_wakeup_fd(write)
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, signum, frame):
        self.requested = True

    def wait(self, seconds):
        """Sleep up to `seconds`; True as soon as a stop is requested."""
        if not self.requested:
            readable, _, _ = select.select([self._read], [], [], seconds)
            if readable:
                try:
                    os.read(self._read, 512)  # drain; the flag is what counts
                except BlockingIOError:
                    pass
        return self.requested


class _Stopped(BaseException):
    """SIGTERM/SIGINT in run-once mode: ends the cycle wherever it is.

    A BaseException, so the cycle's error handler lets it through: a stop isn't a
    failure to notify about.
    """

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def _raise_stopped(signum, frame):
    # Python runs this in the main thread between bytecodes; a socket read in progress
    # is interrupted (EINTR) and this exception propagates out of urlopen.
    raise _Stopped(signum)


def _run_once(accounts):
    """Run one cycle and exit: 0 if it finished, 1 if it failed, 128+signal if stopped.

    Unlike the scheduled loop, a stop here doesn't let the cycle finish: there's no
    wait to end, and a cycle stuck on a slow request would outlast docker stop's 10 s.
    """
    signal.signal(signal.SIGTERM, _raise_stopped)
    signal.signal(signal.SIGINT, _raise_stopped)
    try:
        finished = run_guarded_cycle(accounts, will_retry=False)
    except _Stopped as stop:
        print("Stopped before the check finished", file=sys.stderr)
        sys.exit(128 + stop.signum)
    finally:
        # Back to the defaults, so a signal from here on (a second one, or one just
        # after the cycle) ends the process as usual instead of raising uncaught.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(0 if finished else 1)


def _apply(settings):
    """Hand the validated settings to the module-level names the cycle reads."""
    global YNAB_API_TOKEN, YNAB_BUDGET_ID, YNAB_CC_CATEGORIES, MONITOR_DAYS
    global APPRISE_URLS, UPDATE_APPRISE_URLS
    YNAB_API_TOKEN = settings.ynab_api_token.get_secret_value()
    YNAB_BUDGET_ID = settings.ynab_budget_id
    YNAB_CC_CATEGORIES = settings.ynab_cc_categories
    MONITOR_DAYS = settings.monitor_days
    APPRISE_URLS = settings.apprise_urls.get_secret_value()
    UPDATE_APPRISE_URLS = (
        settings.update_apprise_urls.get_secret_value()
        if settings.update_apprise_urls
        else ""
    )
    _install_redaction(
        [
            YNAB_API_TOKEN,
            *_secret_parts(APPRISE_URLS),
            *_secret_parts(UPDATE_APPRISE_URLS),
        ]
    )


def main():
    # First, so `docker logs` shows what's running even when the config is refused.
    # GIT_REVISION is the image's build arg (the commit it was built from).
    print(
        f"YNAB Balance Monitor starting, revision {os.environ.get('GIT_REVISION') or 'unknown'}"
    )

    settings = load_settings()
    _apply(settings)
    accounts = settings.accounts

    schedule = _parse_schedule(settings.schedule)
    update_schedule = _parse_schedule(settings.update_schedule)

    if schedule is None and update_schedule is None:
        _run_once(accounts)  # exits
        return

    # Run on a schedule (at least one of check / update is recurring).
    # SIGTERM (docker stop) and SIGINT end the wait at once; a cycle already running
    # finishes first.
    stop = _Stop()
    # A retry wait ends at once on a stop, like the wait between runs, and keeps the
    # heartbeat fresh.
    global _retry_wait, _stopping
    _retry_wait = _heartbeat_wait(stop)
    _stopping = lambda: stop.requested

    # Determine initial next-run times.
    # Interval schedules fire immediately on startup; daily schedules wait for
    # their first configured time.
    now = datetime.now()

    if schedule:
        _describe_schedule("Check schedule", schedule)
        next_check = now if schedule[0] == "interval" else _next_occurrence(schedule)
    else:
        next_check = None

    if update_schedule:
        _describe_schedule("Update schedule", update_schedule)
        next_update = (
            now
            if update_schedule[0] == "interval"
            else _next_occurrence(update_schedule)
        )
    else:
        next_update = None

    touch_heartbeat()

    while not stop.requested:
        # Sleep until the sooner of the two pending events
        candidates = [t for t in [next_check, next_update] if t is not None]
        if not candidates:
            break
        wake_time = min(candidates)

        wait = (wake_time - datetime.now()).total_seconds()
        if wait > 0:
            print(f"Next event in {wait / 3600:.4g} hours")
            # Wake at least once a minute to refresh the heartbeat.
            while wait > 0 and not stop.wait(min(wait, 60)):
                touch_heartbeat()
                wait = (wake_time - datetime.now()).total_seconds()

        if stop.requested:
            break

        now = datetime.now()
        do_check = next_check is not None and now >= next_check
        do_update = next_update is not None and now >= next_update

        # Run the projection (always checks alert threshold; optionally sends
        # an update notification when the update schedule fires, or on every
        # check when no separate UPDATE_SCHEDULE is configured).
        # A failed cycle is logged and notified; the loop waits for the next run.
        try:
            run_guarded_cycle(
                accounts,
                send_update=do_update or (do_check and update_schedule is None),
            )
        except _Stopped:
            break  # stopped during a retry wait: a stop isn't a failure to report

        if do_check and schedule:
            next_check = _next_occurrence(schedule)
        if do_update and update_schedule:
            next_update = _next_occurrence(update_schedule)

    print("Shutting down...")


if __name__ == "__main__":
    main()
