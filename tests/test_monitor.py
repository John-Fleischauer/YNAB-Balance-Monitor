"""Tests of the pure logic: recurrence expansion, projection, thresholds, config parsing.

Nothing here calls YNAB or Apprise; run_cycle's API and notification calls are replaced.
"""

from datetime import date, datetime, timedelta

import pytest

import monitor


def txn(day, amount, transfer_account_id=None):
    """A scheduled transaction as get_scheduled_transactions returns it."""
    return {
        "date": day,
        "amount": amount,
        "payee": "Payee",
        "transfer_account_id": transfer_account_id,
        "frequency": "never",
        "label": "Payee",
    }


# ---------------------------------------------------------------------------
# _add_months
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 1, 15), 1, date(2026, 2, 15)),
        (date(2026, 1, 31), 1, date(2026, 2, 28)),  # clamped to month end
        (date(2028, 1, 31), 1, date(2028, 2, 29)),  # leap year
        (date(2026, 11, 30), 3, date(2027, 2, 28)),  # across a year end
        (date(2026, 3, 31), -1, date(2026, 2, 28)),  # backwards
        (date(2026, 1, 1), -1, date(2025, 12, 1)),
    ],
)
def test_add_months_clamps_to_month_end(start, months, expected):
    assert monitor._add_months(start, months) == expected


# ---------------------------------------------------------------------------
# _expand_occurrences
# ---------------------------------------------------------------------------

START = date(2026, 3, 1)
END = date(2026, 3, 31)


def test_one_time_inside_window():
    assert monitor._expand_occurrences(date(2026, 3, 10), "never", START, END) == [
        date(2026, 3, 10)
    ]


def test_one_time_outside_window():
    assert monitor._expand_occurrences(date(2026, 4, 1), "never", START, END) == []


def test_unknown_frequency_is_treated_as_one_time():
    assert monitor._expand_occurrences(date(2026, 3, 5), "fortnightly", START, END) == [
        date(2026, 3, 5)
    ]


def test_weekly_includes_both_window_ends():
    assert monitor._expand_occurrences(date(2026, 3, 3), "weekly", START, END) == [
        date(2026, 3, 3),
        date(2026, 3, 10),
        date(2026, 3, 17),
        date(2026, 3, 24),
        date(2026, 3, 31),
    ]


def test_every_other_week():
    assert monitor._expand_occurrences(
        date(2026, 3, 2), "everyOtherWeek", START, END
    ) == [date(2026, 3, 2), date(2026, 3, 16), date(2026, 3, 30)]


def test_daily_counts_every_day():
    assert len(monitor._expand_occurrences(START, "daily", START, END)) == 31


def test_monthly_after_window_is_empty():
    assert monitor._expand_occurrences(date(2026, 4, 1), "monthly", START, END) == []


def test_monthly_clamps_to_a_short_month():
    start, end = date(2026, 2, 1), date(2026, 2, 28)
    assert monitor._expand_occurrences(date(2026, 1, 31), "monthly", start, end) == [
        date(2026, 2, 28)
    ]


# Not tested: after one clamp the walk continues from the clamped day (Jan 31 -> Feb 28
# -> Mar 28), where YNAB would post on Mar 31. That's a known bug, left for its own fix;
# a test here would pin the wrong dates.


def test_next_date_before_window_start_is_skipped():
    # A next date before today (overdue) is walked forward, not counted.
    assert monitor._expand_occurrences(date(2026, 2, 24), "weekly", START, END) == [
        date(2026, 3, 3),
        date(2026, 3, 10),
        date(2026, 3, 17),
        date(2026, 3, 24),
        date(2026, 3, 31),
    ]


def test_twice_a_month():
    assert monitor._expand_occurrences(date(2026, 3, 1), "twiceAMonth", START, END) == [
        date(2026, 3, 1),
        date(2026, 3, 16),
    ]


def test_twice_a_month_late_day_clamps_in_february():
    start, end = date(2026, 2, 1), date(2026, 2, 28)
    # Day 30 and day 15: February has no 30th, so it lands on the 28th.
    assert monitor._expand_occurrences(
        date(2026, 1, 30), "twiceAMonth", start, end
    ) == [
        date(2026, 2, 15),
        date(2026, 2, 28),
    ]


@pytest.mark.parametrize(
    "frequency",
    ["every3Months", "every4Months", "twiceAYear", "yearly", "everyOtherYear"],
)
def test_long_frequencies_occur_once_in_a_month(frequency):
    assert monitor._expand_occurrences(date(2026, 3, 20), frequency, START, END) == [
        date(2026, 3, 20)
    ]


# ---------------------------------------------------------------------------
# project_minimum_balance
# ---------------------------------------------------------------------------


TODAY = date(2026, 3, 10)


class FixedDatetime(datetime):
    """datetime with now() frozen at TODAY noon, so no test depends on the clock."""

    @classmethod
    def now(cls, tz=None):
        return cls(TODAY.year, TODAY.month, TODAY.day, 12, 0)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(monitor, "datetime", FixedDatetime)


@pytest.fixture
def today():
    return TODAY


def test_minimum_is_the_lowest_day_not_the_end(today):
    end = today + timedelta(days=10)
    txns = [
        txn(today + timedelta(days=2), -800.0),
        txn(today + timedelta(days=5), 1000.0),
    ]
    min_balance, min_date = monitor.project_minimum_balance(500.0, txns, {}, end)
    assert min_balance == -300.0
    assert min_date == today + timedelta(days=2)


def test_no_transactions_keeps_todays_balance(today):
    assert monitor.project_minimum_balance(250.0, [], {}, today) == (250.0, today)


def test_transactions_after_end_are_ignored(today):
    end = today + timedelta(days=3)
    txns = [txn(today + timedelta(days=4), -1000.0)]
    assert monitor.project_minimum_balance(100.0, txns, {}, end) == (100.0, today)


def test_unscheduled_cc_payment_applies_today(today):
    cc = {"cc1": {"name": "Card", "amount": 300.0}}
    min_balance, min_date = monitor.project_minimum_balance(
        1000.0, [], cc, today + timedelta(days=5)
    )
    assert min_balance == 700.0
    assert min_date == today


def test_scheduled_cc_transfer_is_not_counted_twice(today):
    cc = {"cc1": {"name": "Card", "amount": 300.0}}
    txns = [txn(today + timedelta(days=3), -300.0, transfer_account_id="cc1")]
    min_balance, min_date = monitor.project_minimum_balance(
        1000.0, txns, cc, today + timedelta(days=5)
    )
    # Only the scheduled transfer counts: 1000 - 300, on the transfer's day.
    assert min_balance == 700.0
    assert min_date == today + timedelta(days=3)


def test_partly_scheduled_cc_payment_applies_the_rest_today(today):
    cc = {"cc1": {"name": "Card", "amount": 500.0}}
    txns = [txn(today + timedelta(days=3), -200.0, transfer_account_id="cc1")]
    min_balance, min_date = monitor.project_minimum_balance(
        1000.0, txns, cc, today + timedelta(days=5)
    )
    # 300 unscheduled today, then the 200 transfer: 1000 - 300 - 200.
    assert min_balance == 500.0
    assert min_date == today + timedelta(days=3)


def test_cc_dedup_does_not_change_the_callers_dict(today):
    cc = {"cc1": {"name": "Card", "amount": 300.0}}
    txns = [txn(today, -300.0, transfer_account_id="cc1")]
    monitor.project_minimum_balance(1000.0, txns, cc, today)
    assert cc == {"cc1": {"name": "Card", "amount": 300.0}}


def test_transfer_to_another_account_is_a_plain_transaction(today):
    cc = {"cc1": {"name": "Card", "amount": 300.0}}
    txns = [txn(today + timedelta(days=1), -100.0, transfer_account_id="savings")]
    min_balance, _ = monitor.project_minimum_balance(
        1000.0, txns, cc, today + timedelta(days=2)
    )
    assert min_balance == 600.0


# ---------------------------------------------------------------------------
# Thresholds: run_cycle alerts below the threshold, not at it
# ---------------------------------------------------------------------------


@pytest.fixture
def cycle(monkeypatch, today):
    """Run run_cycle against a fake account; returns the alerts and updates sent."""
    sent = {"alerts": [], "updates": []}

    def run(balance, threshold, scheduled=(), include_cc=False, send_update=False):
        monkeypatch.setattr(monitor, "MONITOR_DAYS", 10)
        monkeypatch.setattr(
            monitor, "fetch_all_scheduled_transactions", lambda: list(scheduled)
        )
        monkeypatch.setattr(
            monitor, "get_account_balance", lambda account_id: (balance, "Checking")
        )
        monkeypatch.setattr(monitor, "get_cc_payment_amounts", lambda f="": ({}, 0))
        monkeypatch.setattr(
            monitor,
            "send_alert_notification",
            lambda *args: sent["alerts"].append(args),
        )
        monkeypatch.setattr(
            monitor,
            "send_update_notification",
            lambda *args: sent["updates"].append(args),
        )
        account = monitor.AccountConfig(
            account_id="acct", min_balance=threshold, include_cc=include_cc
        )
        monitor.run_cycle([account], send_update=send_update)
        return sent

    return run


def scheduled(account_id, day, milliunits):
    """A scheduled transaction as the YNAB API returns it."""
    return {
        "account_id": account_id,
        "date_next": day.isoformat(),
        "frequency": "never",
        "amount": milliunits,
        "payee_name": "Rent",
    }


def test_alert_when_projection_drops_below_threshold(cycle, today):
    sent = cycle(
        balance=1000.0,
        threshold=500,
        scheduled=[scheduled("acct", today + timedelta(days=2), -600_000)],
    )
    assert len(sent["alerts"]) == 1
    min_balance, min_date, name, threshold = sent["alerts"][0]
    assert (min_balance, min_date, name, threshold) == (
        400.0,
        today + timedelta(days=2),
        "Checking",
        500,
    )


def test_no_alert_at_exactly_the_threshold(cycle):
    assert cycle(balance=500.0, threshold=500)["alerts"] == []


def test_other_accounts_transactions_are_ignored(cycle, today):
    sent = cycle(
        balance=1000.0,
        threshold=500,
        scheduled=[scheduled("other", today + timedelta(days=1), -900_000)],
    )
    assert sent["alerts"] == []


def test_update_sent_only_when_asked(cycle):
    assert cycle(balance=1000.0, threshold=0)["updates"] == []
    assert len(cycle(balance=1000.0, threshold=0, send_update=True)["updates"]) == 1


# ---------------------------------------------------------------------------
# Account configuration
# ---------------------------------------------------------------------------


def test_parse_account_entries():
    assert monitor._parse_account_entries(" a:500, b ,,c:-20", "VAR") == [
        ("a", 500),
        ("b", 0),
        ("c", -20),
    ]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("a:500,b:lots", "VAR entry 2: the threshold"),
        ("a:", "VAR entry 1: the threshold"),
        ("a,:5", "VAR entry 2 has no account ID"),
    ],
)
def test_parse_account_entries_refuses_malformed_entries(raw, message):
    with pytest.raises(ValueError, match=message):
        monitor._parse_account_entries(raw, "VAR")


# Every variable Settings reads, cleared so the developer's own environment can't leak in.
SETTINGS_VARIABLES = (
    "YNAB_API_TOKEN",
    "APPRISE_URLS",
    "UPDATE_APPRISE_URLS",
    "YNAB_BUDGET_ID",
    "YNAB_CC_CATEGORIES",
    "MONITOR_DAYS",
    "SCHEDULE",
    "UPDATE_SCHEDULE",
    "YNAB_ACCOUNT_ID_CC",
    "YNAB_ACCOUNT_ID_NO_CC",
    "YNAB_ACCOUNT_ID",
    "MIN_BALANCE",
)


@pytest.fixture
def env(monkeypatch):
    """A minimal valid environment: token, Apprise URL and one account."""
    for name in SETTINGS_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("YNAB_API_TOKEN", "test-token")
    monkeypatch.setenv("APPRISE_URLS", "json://localhost")
    monkeypatch.setenv("YNAB_ACCOUNT_ID_CC", "a")
    return monkeypatch


def refusals(capsys):
    """Load the settings, expecting a refusal; returns the error lines printed."""
    with pytest.raises(SystemExit) as exit_info:
        monitor.load_settings()
    assert exit_info.value.code == 1
    return capsys.readouterr().err.splitlines()


def test_new_format_sets_cc_per_variable(env):
    env.setenv("YNAB_ACCOUNT_ID_CC", "a:100")
    env.setenv("YNAB_ACCOUNT_ID_NO_CC", "b")
    accounts = monitor.load_settings().accounts
    assert [(a.account_id, a.min_balance, a.include_cc) for a in accounts] == [
        ("a", 100, True),
        ("b", 0, False),
    ]


def test_legacy_format_uses_min_balance(env):
    env.delenv("YNAB_ACCOUNT_ID_CC")
    env.setenv("YNAB_ACCOUNT_ID", "a")
    env.setenv("MIN_BALANCE", "250")
    [account] = monitor.load_settings().accounts
    assert (account.account_id, account.min_balance, account.include_cc) == (
        "a",
        250,
        True,
    )


def test_legacy_and_new_format_together_is_refused(env, capsys):
    env.setenv("YNAB_ACCOUNT_ID", "a")
    assert refusals(capsys) == [
        "Error: Set YNAB_ACCOUNT_ID_CC/YNAB_ACCOUNT_ID_NO_CC or the older YNAB_ACCOUNT_ID, not both"
    ]


def test_legacy_takes_one_account(env, capsys):
    env.delenv("YNAB_ACCOUNT_ID_CC")
    env.setenv("YNAB_ACCOUNT_ID", "a,b")
    [line] = refusals(capsys)
    assert line.startswith("Error: YNAB_ACCOUNT_ID takes one account ID")


@pytest.mark.parametrize("cc", ["", " , "])
def test_no_accounts_is_refused(env, capsys, cc):
    env.setenv("YNAB_ACCOUNT_ID_CC", cc)
    [line] = refusals(capsys)
    assert line.startswith("Error: No accounts configured")


def test_blank_optional_variables_get_their_defaults(env):
    # Compose passes every optional variable as ${VAR:-}, so unset arrives as "".
    for name in SETTINGS_VARIABLES[2:]:
        if name != "YNAB_ACCOUNT_ID_CC":
            env.setenv(name, "")
    settings = monitor.load_settings()
    assert settings.ynab_budget_id == "last-used"
    assert settings.monitor_days is None
    assert settings.update_apprise_urls is None
    assert (settings.schedule, settings.update_schedule) == ("", "")
    assert settings.accounts[0].min_balance == 0


def test_missing_required_variables_are_named(env, capsys):
    env.delenv("YNAB_API_TOKEN")
    env.setenv("APPRISE_URLS", " , ")
    assert refusals(capsys) == [
        "Error: YNAB_API_TOKEN is required",
        "Error: APPRISE_URLS: must hold at least one URL",
    ]


def test_every_malformed_value_is_reported_without_secrets(env, capsys):
    env.setenv("YNAB_API_TOKEN", "  ")
    env.setenv("UPDATE_APPRISE_URLS", "json://user:hunter2@localhost")
    env.setenv("MONITOR_DAYS", "ten")
    env.setenv("MIN_BALANCE", "-1")
    env.setenv("SCHEDULE", "25:00")
    env.setenv("UPDATE_SCHEDULE", "0h")
    lines = refusals(capsys)
    assert [line.split(":")[1].strip() for line in lines] == [
        "YNAB_API_TOKEN",
        "MONITOR_DAYS",
        "SCHEDULE",
        "UPDATE_SCHEDULE",
    ]
    assert "hunter2" not in "\n".join(lines)


def test_min_balance_is_ignored_with_the_new_format(env):
    # As before Phase 2: MIN_BALANCE belongs to YNAB_ACCOUNT_ID only.
    env.setenv("MIN_BALANCE", "100.50")
    [account] = monitor.load_settings().accounts
    assert account.min_balance == 0


def test_bad_min_balance_with_the_legacy_format_is_refused(env, capsys):
    env.delenv("YNAB_ACCOUNT_ID_CC")
    env.setenv("YNAB_ACCOUNT_ID", "a")
    env.setenv("MIN_BALANCE", "100.50")
    assert refusals(capsys) == ["Error: MIN_BALANCE must be a whole number of dollars"]


def test_account_problems_are_reported_with_field_problems(env, capsys):
    # pydantic skips the model validator when a field fails; load_settings doesn't.
    env.setenv("MONITOR_DAYS", "ten")
    env.setenv("YNAB_ACCOUNT_ID", "a")
    lines = refusals(capsys)
    assert len(lines) == 2
    assert lines[0].startswith("Error: MONITOR_DAYS:")
    assert lines[1].startswith("Error: Set YNAB_ACCOUNT_ID_CC/")


def test_settings_reach_the_cycle(env):
    env.setenv("UPDATE_APPRISE_URLS", "json://updates")
    env.setenv("MONITOR_DAYS", "0")
    env.setattr(monitor, "YNAB_API_TOKEN", "")
    env.setattr(monitor, "MONITOR_DAYS", None)
    env.setattr(monitor, "APPRISE_URLS", "")
    env.setattr(monitor, "UPDATE_APPRISE_URLS", "")
    monitor._apply(monitor.load_settings())
    assert monitor.YNAB_API_TOKEN == "test-token"
    assert monitor.MONITOR_DAYS == 0
    assert monitor.APPRISE_URLS == "json://localhost"
    assert monitor.UPDATE_APPRISE_URLS == "json://updates"


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", None),
        ("  ", None),
        ("6h", ("interval", 6 * 3600)),
        ("0.5h", ("interval", 1800)),
        ("08:00", ("daily", 8, 0)),
        (" 18:30 ", ("daily", 18, 30)),
    ],
)
def test_parse_schedule(value, expected):
    assert monitor._parse_schedule(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "daily",
        "xh",
        "8:00:00",
        "8",
        "0h",
        "-1h",
        "0.01h",  # cycles back to back
        "100000000h",  # the next run's date overflows
        "nanh",
        "infh",
        "24:00",
        "08:60",
    ],
)
def test_parse_schedule_refuses_bad_formats(value):
    with pytest.raises(ValueError, match="invalid schedule"):
        monitor._parse_schedule(value)
