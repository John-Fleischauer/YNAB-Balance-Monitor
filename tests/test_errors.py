"""Errors at the boundaries: YNAB and network failures, notification URLs, secrets in logs.

Nothing here calls YNAB or a notification service: urlopen and the senders are replaced.
"""

import io
import json
import logging
import signal
from datetime import date
from urllib.error import HTTPError, URLError

import pytest

import monitor

# A Telegram URL Apprise can't parse: the part after the scheme is the bot token.
BAD_URL = "tgram://SECRET-BOT-TOKEN/"


@pytest.fixture
def notifications(monkeypatch):
    """Record what would be sent, instead of sending it."""
    sent = []

    def send(urls_str, variable, title, message, notify_type):
        sent.append(
            {"urls": urls_str, "variable": variable, "title": title, "body": message}
        )
        return True

    monkeypatch.setattr(monitor, "_send_notification", send)
    return sent


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """Each test starts with no failures counted, no retries left and a plain sleep;
    monkeypatch puts back whatever a test (or main()) changes."""
    monkeypatch.setattr(monitor, "_failures_in_a_row", 0)
    monkeypatch.setattr(monitor, "_notified_kind", None)
    monkeypatch.setattr(monitor, "_stopping", bool)
    monkeypatch.setattr(monitor, "_retries_left", [])
    monkeypatch.setattr(monitor, "_wait_budget", 0)
    monkeypatch.setattr(monitor, "_retry_wait", monitor._sleep)


@pytest.fixture
def signal_handlers():
    """Put SIGTERM and SIGINT back as they were: run-once mode replaces them."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


# ---------------------------------------------------------------------------
# Apprise URLs never reach the logs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variable", ["APPRISE_URLS", "UPDATE_APPRISE_URLS"])
def test_load_failure_names_the_entry_not_the_url(capsys, variable):
    notifier = monitor._build_notifier(f"json://localhost, ;{BAD_URL}", variable)
    assert len(notifier) == 1
    err = capsys.readouterr().err
    # Entry 2: blank entries between separators don't count, as at boot.
    assert f"Apprise: {variable} entry 2 failed to load" in err
    assert "SECRET" not in err
    assert "tgram" not in err


def test_apprise_own_log_lines_are_redacted():
    # Apprise itself logs a URL it can't load; the filter masks it.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        monitor._install_redaction(
            ["the-ynab-token", *monitor._secret_parts(f"json://localhost,{BAD_URL}")]
        )
        monitor._build_notifier(BAD_URL, "APPRISE_URLS")
        logging.getLogger("apprise").error("token the-ynab-token")
    finally:
        root.removeHandler(handler)
        monitor._install_redaction([])  # leave no secrets on the root handlers
    logged = stream.getvalue()
    assert "Unparseable" in logged  # Apprise did log, so the check means something
    assert "SECRET" not in logged
    assert "the-ynab-token" not in logged


def test_secret_parts_split_like_apprise():
    # Apprise accepts URLs separated by spaces or ';' as well as ','.
    parts = monitor._secret_parts(
        "discord://webhookid/TOKEN-ONE tgram://BOTTOKEN/x;json://hostname"
    )
    for secret in ("TOKEN-ONE", "BOTTOKEN", "hostname"):
        assert secret in parts


def test_redaction_survives_a_bad_log_call_and_masks_tracebacks():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    log = logging.getLogger("apprise")
    try:
        monitor._install_redaction(["SECRET-TOKEN"])
        log.error("bad %s %s", "SECRET-TOKEN")  # noqa: PLE1206 -- the bad call under test
        try:
            raise RuntimeError("https://hooks.example/SECRET-TOKEN")
        except RuntimeError:
            log.exception("send failed")
    finally:
        root.removeHandler(handler)
        monitor._install_redaction([])
    logged = stream.getvalue()
    assert "log arguments didn't match" in logged
    assert "RuntimeError" in logged
    assert "SECRET-TOKEN" not in logged


def test_secret_parts_cover_every_credential_position():
    parts = monitor._secret_parts(
        "mailto://someuser:hunter2%40x@mail.example, discord://webhookid/webhooktoken,"
        " ntfy://topicname?token=tk_abcdef"
    )
    for secret in (
        "someuser",
        "hunter2@x",  # decoded, as Apprise may log it
        "hunter2%40x",
        "webhookid",
        "webhooktoken",
        "tk_abcdef",
    ):
        assert secret in parts


def test_update_notification_names_its_variable(monkeypatch, notifications):
    today = date(2026, 3, 1)
    monkeypatch.setattr(monitor, "APPRISE_URLS", "json://alerts")
    monkeypatch.setattr(monitor, "UPDATE_APPRISE_URLS", "json://updates")
    monitor.send_update_notification(1.0, today, today, "Checking", 0)
    monkeypatch.setattr(monitor, "UPDATE_APPRISE_URLS", "")
    monitor.send_update_notification(1.0, today, today, "Checking", 0)
    assert [(n["urls"], n["variable"]) for n in notifications] == [
        ("json://updates", "UPDATE_APPRISE_URLS"),
        ("json://alerts", "APPRISE_URLS"),
    ]


# ---------------------------------------------------------------------------
# ynab_get: a timeout on every request, and errors raised, not exited
# ---------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_every_request_has_a_timeout(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(timeout)
        return FakeResponse(json.dumps({"data": {"ok": 1}}).encode())

    monkeypatch.setattr(monitor, "urlopen", fake_urlopen)
    assert monitor.ynab_get("/budgets/b/accounts", "accounts") == {"ok": 1}
    assert calls == [monitor.YNAB_TIMEOUT]
    assert 0 < monitor.YNAB_TIMEOUT <= 60


def http_error(code, body):
    return HTTPError(
        "https://api.ynab.com/v1/budgets/budget-id-123/accounts/acct-id-456",
        code,
        "Error",
        {},
        io.BytesIO(body),
    )


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (
            http_error(
                401,
                b'{"error": {"id": "401", "name": "unauthorized",'
                b' "detail": "acct-id-456"}}',
            ),
            "YNAB API error fetching an account: HTTP 401 (unauthorized)",
        ),
        (
            http_error(500, b"<html>acct-id-456</html>"),
            "YNAB API error fetching an account: HTTP 500",
        ),
        (
            URLError(OSError("Name or service not known")),
            "Network error fetching an account: Name or service not known",
        ),
        (
            TimeoutError("timed out"),
            f"Network error fetching an account: no reply within {monitor.YNAB_TIMEOUT} s",
        ),
        (
            ConnectionResetError("reset"),
            "Network error fetching an account: ConnectionResetError",
        ),
    ],
)
def test_request_errors_raise_without_ids(monkeypatch, raised, expected):
    def fake_urlopen(request, timeout=None):
        raise raised

    monkeypatch.setattr(monitor, "urlopen", fake_urlopen)
    monkeypatch.setattr(monitor, "YNAB_API_TOKEN", "the-ynab-token")
    with pytest.raises(monitor.YNABError) as error:
        monitor.ynab_get("/budgets/budget-id-123/accounts/acct-id-456", "an account")
    assert str(error.value) == expected
    for secret in ("acct-id-456", "budget-id-123", "the-ynab-token"):
        assert secret not in str(error.value)


@pytest.mark.parametrize("body", [b"not json", b'{"nodata": 1}'])
def test_a_reply_without_data_raises(monkeypatch, body):
    monkeypatch.setattr(monitor, "urlopen", lambda r, timeout=None: FakeResponse(body))
    with pytest.raises(monitor.YNABError, match="Unexpected reply from YNAB"):
        monitor.ynab_get("/budgets/b/categories", "categories")


# ---------------------------------------------------------------------------
# The cycle handler
# ---------------------------------------------------------------------------


def failing_cycle(monkeypatch, error):
    def run_cycle(accounts, send_update=False):
        raise error

    monkeypatch.setattr(monitor, "run_cycle", run_cycle)


def test_a_ynab_error_is_logged_and_notified(monkeypatch, capsys, notifications):
    monkeypatch.setattr(monitor, "APPRISE_URLS", "json://alerts")
    failing_cycle(monkeypatch, monitor.YNABError("Network error fetching accounts: x"))
    assert monitor.run_guarded_cycle([]) is False
    assert "Check failed: Network error fetching accounts: x" in capsys.readouterr().err
    [sent] = notifications
    assert sent["urls"] == "json://alerts"
    assert sent["variable"] == "APPRISE_URLS"
    assert sent["title"] == "YNAB Balance Monitor failed"
    assert "Network error fetching accounts: x" in sent["body"]
    assert "runs again at the next scheduled time" in sent["body"]


def test_an_unexpected_error_gives_only_its_type(monkeypatch, capsys, notifications):
    failing_cycle(monkeypatch, KeyError("json://user:hunter2@host"))
    assert monitor.run_guarded_cycle([]) is False
    err = capsys.readouterr().err
    assert "Check failed: unexpected KeyError in run_cycle, line " in err
    assert "hunter2" not in err
    assert "hunter2" not in notifications[0]["body"]


def test_a_finished_cycle_sends_nothing(monkeypatch, notifications):
    monkeypatch.setattr(monitor, "run_cycle", lambda accounts, send_update=False: None)
    assert monitor.run_guarded_cycle([]) is True
    assert notifications == []


def test_a_failed_failure_notification_is_logged(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "_send_notification", lambda *args: False)
    failing_cycle(monkeypatch, monitor.YNABError("boom"))
    assert monitor.run_guarded_cycle([]) is False
    assert "Could not send the failure notification" in capsys.readouterr().err


def test_a_raising_failure_notification_is_logged_without_its_message(
    monkeypatch, capsys
):
    def send(*args):
        raise RuntimeError("json://user:hunter2@host")

    monkeypatch.setattr(monitor, "_send_notification", send)
    failing_cycle(monkeypatch, monitor.YNABError("boom"))
    assert monitor.run_guarded_cycle([]) is False
    err = capsys.readouterr().err
    assert "Could not send the failure notification: RuntimeError" in err
    assert "hunter2" not in err


# ---------------------------------------------------------------------------
# main(): the loop survives a failed cycle; run-once exits non-zero
# ---------------------------------------------------------------------------


@pytest.fixture
def main_env(monkeypatch, tmp_path):
    for name in (
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
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("YNAB_API_TOKEN", "test-token")
    monkeypatch.setenv("APPRISE_URLS", "json://localhost")
    monkeypatch.setenv("YNAB_ACCOUNT_ID_CC", "a")
    monkeypatch.setattr(monitor, "HEARTBEAT_PATH", str(tmp_path / "heartbeat"))
    return monkeypatch


def test_the_scheduled_loop_survives_a_failed_cycle(main_env, notifications):
    main_env.setenv("SCHEDULE", "1h")

    class FakeStop:
        requested = False

        def wait(self, seconds):
            return self.requested

    stop = FakeStop()
    main_env.setattr(monitor, "_Stop", lambda: stop)
    # Every run is due at once, so the loop runs cycle after cycle without waiting.
    main_env.setattr(
        monitor, "_next_occurrence", lambda schedule: monitor.datetime.now()
    )

    cycles = []

    def run_cycle(accounts, send_update=False):
        cycles.append(send_update)
        if len(cycles) == 1:
            raise monitor.YNABError("Network error fetching accounts: down")
        stop.requested = True  # the second cycle ran: the loop survived the first

    main_env.setattr(monitor, "run_cycle", run_cycle)
    monitor.main()
    assert len(cycles) == 2
    assert [n["title"] for n in notifications] == [
        "YNAB Balance Monitor failed",
        "YNAB Balance Monitor working again",
    ]
    assert "Network error fetching accounts: down" in notifications[0]["body"]


def test_run_once_exits_1_after_a_failed_cycle(
    main_env, notifications, signal_handlers
):
    failing_cycle(main_env, monitor.YNABError("Network error fetching accounts: down"))
    with pytest.raises(SystemExit) as exit_info:
        monitor.main()
    assert exit_info.value.code == 1
    [sent] = notifications
    assert "runs again" not in sent["body"]


def test_run_once_exits_0_after_a_finished_cycle(
    main_env, notifications, signal_handlers
):
    main_env.setattr(monitor, "run_cycle", lambda accounts, send_update=False: None)
    with pytest.raises(SystemExit) as exit_info:
        monitor.main()
    assert exit_info.value.code == 0
    assert notifications == []


def test_run_once_stop_skips_the_failure_notification(
    main_env, capsys, notifications, signal_handlers
):
    failing_cycle(main_env, monitor._Stopped(signal.SIGTERM))
    with pytest.raises(SystemExit) as exit_info:
        monitor.main()
    assert exit_info.value.code == 128 + signal.SIGTERM
    assert notifications == []
    assert "Stopped before the check finished" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Retries inside a cycle
# ---------------------------------------------------------------------------


def flaky_urlopen(monkeypatch, *errors):
    """urlopen raising each of `errors` in turn (None answers), then answering."""
    queue = list(errors)
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(timeout)
        error = queue.pop(0) if queue else None
        if error:
            raise error
        return FakeResponse(json.dumps({"data": {"ok": 1}}).encode())

    monkeypatch.setattr(monitor, "urlopen", fake_urlopen)
    return calls


@pytest.fixture
def waits(monkeypatch):
    """Record each retry wait instead of sleeping; no stop is requested."""
    seen = []
    monkeypatch.setattr(monitor, "_retry_wait", lambda s: seen.append(s) or False)
    monkeypatch.setattr(monitor, "_retries_left", list(monitor.RETRY_WAITS))
    monkeypatch.setattr(monitor, "_wait_budget", monitor.RETRY_BUDGET)
    return seen


@pytest.mark.parametrize(
    "error",
    [
        URLError(OSError("Name or service not known")),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
        http_error(503, b""),
        http_error(429, b'{"error": {"name": "too_many_requests"}}'),
    ],
)
def test_network_errors_5xx_and_429_are_retried(monkeypatch, capsys, waits, error):
    calls = flaky_urlopen(monkeypatch, error, error)
    assert monitor.ynab_get("/budgets/b/accounts", "accounts") == {"ok": 1}
    assert len(calls) == 3
    assert waits == [60, 300]
    err = capsys.readouterr().err
    assert "; retrying in 60 s" in err
    assert "; retrying in 300 s" in err
    assert "/budgets/b" not in err


def test_retries_give_up_after_the_last_wait(monkeypatch, waits):
    error = http_error(502, b"")
    calls = flaky_urlopen(monkeypatch, error, error, error)
    with pytest.raises(monitor.YNABError, match="HTTP 502"):
        monitor.ynab_get("/budgets/b/accounts", "accounts")
    assert len(calls) == 3
    assert waits == [60, 300]


@pytest.mark.parametrize(
    "error",
    [http_error(401, b'{"error": {"name": "unauthorized"}}'), http_error(404, b"")],
)
def test_other_4xx_fail_at_once(monkeypatch, waits, error):
    calls = flaky_urlopen(monkeypatch, error)
    with pytest.raises(monitor.YNABError):
        monitor.ynab_get("/budgets/b/accounts", "accounts")
    assert len(calls) == 1
    assert waits == []


def test_an_unexpected_reply_fails_at_once(monkeypatch, waits):
    monkeypatch.setattr(
        monitor, "urlopen", lambda r, timeout=None: FakeResponse(b"not json")
    )
    with pytest.raises(monitor.YNABError, match="Unexpected reply"):
        monitor.ynab_get("/budgets/b/accounts", "accounts")
    assert waits == []


def test_the_retry_budget_is_shared_by_the_cycle(monkeypatch, notifications, waits):
    # The first request uses the 60 s wait; the second gets only the 300 s one.
    error = URLError(OSError("down"))
    calls = flaky_urlopen(monkeypatch, error, None, error, error)

    def run_cycle(accounts, send_update=False):
        monitor.ynab_get("/budgets/b/accounts", "accounts")
        monitor.ynab_get("/budgets/b/categories", "categories")

    monkeypatch.setattr(monitor, "run_cycle", run_cycle)
    assert monitor.run_guarded_cycle([]) is False
    assert waits == [60, 300]
    assert len(calls) == 4
    assert "Network error fetching categories: down" in notifications[0]["body"]


def test_a_stop_during_a_retry_wait_ends_the_cycle_unreported(
    monkeypatch, capsys, notifications
):
    monkeypatch.setattr(monitor, "_retry_wait", lambda seconds: True)  # stop requested
    flaky_urlopen(monkeypatch, URLError(OSError("down")))
    monkeypatch.setattr(
        monitor,
        "run_cycle",
        lambda accounts, send_update=False: monitor.ynab_get("/b", "accounts"),
    )
    with pytest.raises(monitor._Stopped):
        monitor.run_guarded_cycle([])
    assert notifications == []
    assert monitor._failures_in_a_row == 0
    assert "Check failed" not in capsys.readouterr().err


def test_a_stop_during_a_retry_wait_ends_the_scheduled_loop(
    main_env, capsys, notifications
):
    main_env.setenv("SCHEDULE", "1h")

    class FakeStop:
        requested = False

        def wait(self, seconds):
            # The retry wait: docker stop arrives during it.
            self.requested = True
            return True

    main_env.setattr(monitor, "_Stop", FakeStop)
    flaky_urlopen(main_env, URLError(OSError("down")))
    main_env.setattr(
        monitor,
        "run_cycle",
        lambda accounts, send_update=False: monitor.ynab_get("/b", "accounts"),
    )
    monitor.main()  # returns: no exception, no exit code
    out = capsys.readouterr()
    assert "Shutting down..." in out.out
    assert "retrying in 60 s" in out.err
    assert notifications == []


# ---------------------------------------------------------------------------
# Only the first of a run of failures is notified; recovery is
# ---------------------------------------------------------------------------


def test_only_the_first_failure_in_a_row_is_notified(
    monkeypatch, capsys, notifications
):
    outcomes = [monitor.YNABError("down"), monitor.YNABError("down"), None, None]

    def run_cycle(accounts, send_update=False):
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome

    monkeypatch.setattr(monitor, "run_cycle", run_cycle)
    assert [monitor.run_guarded_cycle([]) for _ in range(4)] == [
        False,
        False,
        True,
        True,
    ]
    assert [n["title"] for n in notifications] == [
        "YNAB Balance Monitor failed",
        "YNAB Balance Monitor working again",
    ]
    assert notifications[1]["urls"] == monitor.APPRISE_URLS
    assert notifications[1]["body"] == (
        "The balance check finished again after 2 failed checks."
    )
    assert "Check still failing (2 in a row); not notified again" in (
        capsys.readouterr().err
    )


def test_a_failed_recovery_notification_is_logged(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "_failures_in_a_row", 3)
    monkeypatch.setattr(monitor, "_notified_kind", "network")
    monkeypatch.setattr(monitor, "_send_notification", lambda *args: False)
    monkeypatch.setattr(monitor, "run_cycle", lambda accounts, send_update=False: None)
    assert monitor.run_guarded_cycle([]) is True
    assert "Could not send the recovery notification" in capsys.readouterr().err
    assert monitor._failures_in_a_row == 0


# ---------------------------------------------------------------------------
# Apprise URLs are checked at boot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variable", ["APPRISE_URLS", "UPDATE_APPRISE_URLS"])
def test_an_unloadable_url_fails_the_boot(main_env, capsys, variable):
    main_env.setenv(variable, f"json://localhost {BAD_URL};discord://SECRET-ID/")
    with pytest.raises(SystemExit) as exit_info:
        monitor.load_settings()
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        f"Error: {variable}: entries 2, 3 don't load in Apprise; check their format"
    ]
    assert "SECRET" not in captured.out + captured.err


def test_one_unloadable_url_is_named_alone(main_env, capsys):
    main_env.setenv("APPRISE_URLS", f"{BAD_URL},json://localhost")
    with pytest.raises(SystemExit):
        monitor.load_settings()
    assert capsys.readouterr().err.splitlines() == [
        "Error: APPRISE_URLS: entry 1 doesn't load in Apprise; check its format"
    ]


def test_apprise_logs_nothing_while_urls_are_checked_at_boot(main_env, caplog):
    main_env.setenv("APPRISE_URLS", BAD_URL)
    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit):
        monitor.load_settings()
    assert "SECRET" not in caplog.text
    assert not logging.root.manager.disable  # logging is back on afterwards


def test_entry_numbers_agree_everywhere():
    urls = "json://a ,; json://b\njson://c"
    assert monitor._split_urls(urls) == ["json://a", "json://b", "json://c"]
    assert monitor._split_urls(" , ;\t") == []


def test_a_new_kind_of_failure_is_notified_again(monkeypatch, capsys, notifications):
    errors = [
        monitor.YNABError("Network error fetching accounts: down", kind="network"),
        monitor.YNABError("Network error fetching accounts: timed out", kind="network"),
        monitor.YNABError(
            "YNAB API error fetching accounts: HTTP 401", kind="HTTP 401"
        ),
        monitor.YNABError(
            "YNAB API error fetching accounts: HTTP 401", kind="HTTP 401"
        ),
    ]
    failing = iter(errors)

    def run_cycle(accounts, send_update=False):
        raise next(failing)

    monkeypatch.setattr(monitor, "run_cycle", run_cycle)
    for _ in errors:
        monitor.run_guarded_cycle([])
    bodies = [n["body"] for n in notifications]
    assert len(bodies) == 2
    assert "down" in bodies[0]
    assert "HTTP 401" in bodies[1]


def test_an_unsent_failure_notification_is_tried_again(monkeypatch):
    results = [False, True]
    sent = []

    def send(urls_str, variable, title, message, notify_type):
        sent.append(title)
        return results.pop(0) if results else True

    monkeypatch.setattr(monitor, "_send_notification", send)
    failing_cycle(monkeypatch, monitor.YNABError("down", kind="network"))
    monitor.run_guarded_cycle([])  # notification fails
    monitor.run_guarded_cycle([])  # so this one is notified
    monitor.run_guarded_cycle([])  # and this one isn't
    assert sent == ["YNAB Balance Monitor failed"] * 2


def test_no_recovery_notice_when_no_failure_was_notified(monkeypatch, notifications):
    monkeypatch.setattr(monitor, "_failures_in_a_row", 2)  # never notified
    monkeypatch.setattr(monitor, "run_cycle", lambda accounts, send_update=False: None)
    assert monitor.run_guarded_cycle([]) is True
    assert notifications == []
    assert monitor._failures_in_a_row == 0


def test_a_stop_means_no_further_requests(monkeypatch):
    monkeypatch.setattr(monitor, "_stopping", lambda: True)
    calls = flaky_urlopen(monkeypatch)
    with pytest.raises(monitor._Stopped):
        monitor.ynab_get("/b", "accounts")
    assert calls == []


def test_an_error_after_a_stop_is_a_stop_not_a_failure(monkeypatch, notifications):
    # Retries used up; the stop cuts the request off: still no failure is reported.
    stopping = []
    monkeypatch.setattr(monitor, "_stopping", lambda: bool(stopping))

    def fake_urlopen(request, timeout=None):
        stopping.append(True)
        raise URLError(OSError("reset by the stop"))

    monkeypatch.setattr(monitor, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        monitor,
        "run_cycle",
        lambda accounts, send_update=False: monitor.ynab_get("/b", "accounts"),
    )
    with pytest.raises(monitor._Stopped):
        monitor.run_guarded_cycle([])
    assert notifications == []


def test_a_comma_inside_a_url_stays_in_it(main_env):
    url = "mailto://user:pass@example.com?to=a@example.com,b@example.com"
    main_env.setenv("APPRISE_URLS", f"{url};json://localhost")
    assert monitor._split_urls(
        monitor.load_settings().apprise_urls.get_secret_value()
    ) == [
        url,
        "json://localhost",
    ]


# ---------------------------------------------------------------------------
# Retry-After on a 429
# ---------------------------------------------------------------------------


def too_many_requests(retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return HTTPError(
        "https://api.ynab.com/v1/x", 429, "Too Many", headers, io.BytesIO()
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120", 120),
        (" 0 ", 0),
        ("Sun, 15 Nov 2026 12:02:00 GMT", 120),
        ("Sun, 15 Nov 2026 11:59:00 GMT", None),  # in the past: clock skew
        ("99999999999", 99999999999),  # huge: fails the check at once
        ("Mon, 1 Jan 99999999999999999999 00:00:00 GMT", None),
        (None, None),
        ("", None),
        ("soon", None),
        ("-5", None),
        ("1.5", None),
    ],
)
def test_retry_after_parsing(value, expected):
    now = monitor.datetime(2026, 11, 15, 12, 0, tzinfo=monitor.UTC)
    assert monitor._retry_after_seconds(value, now=now) == expected


def test_a_429_waits_for_retry_after(monkeypatch, capsys, waits):
    calls = flaky_urlopen(monkeypatch, too_many_requests("42"))
    assert monitor.ynab_get("/b", "accounts") == {"ok": 1}
    assert waits == [42]
    assert len(calls) == 2
    assert "retrying in 42 s (YNAB's Retry-After)" in capsys.readouterr().err
    assert monitor._wait_budget == monitor.RETRY_BUDGET - 42


def test_a_429_without_a_usable_header_uses_the_normal_waits(monkeypatch, waits):
    flaky_urlopen(monkeypatch, too_many_requests("soon"), too_many_requests())
    monitor.ynab_get("/b", "accounts")
    assert waits == [60, 300]


@pytest.mark.parametrize("value", ["301", "Sun, 15 Nov 2099 12:00:00 GMT"])
def test_a_retry_after_over_the_cap_fails_at_once(monkeypatch, capsys, waits, value):
    calls = flaky_urlopen(monkeypatch, too_many_requests(value))
    with pytest.raises(monitor.YNABError, match="more than this check can wait"):
        monitor.ynab_get("/b", "accounts")
    assert waits == []
    assert len(calls) == 1
    assert "2099" not in capsys.readouterr().err


def test_a_retry_after_over_the_budget_left_fails_at_once(monkeypatch, waits):
    # 200 s used on the first 429 leaves 160 s: a second asking 200 s gives up.
    calls = flaky_urlopen(
        monkeypatch, too_many_requests("200"), too_many_requests("200")
    )
    with pytest.raises(monitor.YNABError, match="a 200 s wait"):
        monitor.ynab_get("/b", "accounts")
    assert waits == [200]
    assert len(calls) == 2


def test_a_normal_wait_is_cut_to_the_budget_left(monkeypatch, waits):
    flaky_urlopen(monkeypatch, too_many_requests("300"), URLError(OSError("down")))
    monitor.ynab_get("/b", "accounts")
    assert waits == [300, 60]
    assert monitor._wait_budget == 0


def test_a_retry_after_failure_is_notified_without_the_header(
    monkeypatch, notifications, waits
):
    flaky_urlopen(monkeypatch, too_many_requests("Sun, 15 Nov 2099 12:00:00 GMT"))
    monkeypatch.setattr(
        monitor,
        "run_cycle",
        lambda accounts, send_update=False: monitor.ynab_get("/b", "accounts"),
    )
    assert monitor.run_guarded_cycle([]) is False
    [sent] = notifications
    assert "HTTP 429" in sent["body"]
    assert "more than this check can wait" in sent["body"]
    assert "2099" not in sent["body"]


# ---------------------------------------------------------------------------
# The heartbeat during retry waits (scheduled mode)
# ---------------------------------------------------------------------------


class ClockStop:
    """A _Stop whose wait advances a fake clock instead of sleeping."""

    def __init__(self, clock, stop_after=None):
        self.clock = clock
        self.requested = False
        self.waits = []
        self.stop_after = stop_after

    def wait(self, seconds):
        self.waits.append(seconds)
        self.clock[0] += seconds
        if self.stop_after is not None and len(self.waits) >= self.stop_after:
            self.requested = True
        return self.requested


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(monitor.time, "monotonic", lambda: now[0])
    return now


def test_a_retry_wait_writes_the_heartbeat_every_minute(monkeypatch, clock):
    beats = []
    monkeypatch.setattr(monitor, "touch_heartbeat", lambda: beats.append(clock[0]))
    stop = ClockStop(clock)
    assert monitor._heartbeat_wait(stop)(300) is False
    assert stop.waits == [60, 60, 60, 60, 60]
    # At the start, then after each minute.
    assert beats == [1000, 1060, 1120, 1180, 1240, 1300]


def test_a_stop_ends_a_heartbeat_retry_wait_at_once(monkeypatch, clock):
    monkeypatch.setattr(monitor, "touch_heartbeat", lambda: None)
    stop = ClockStop(clock, stop_after=2)
    assert monitor._heartbeat_wait(stop)(300) is True
    assert stop.waits == [60, 60]


def test_the_scheduled_loop_uses_the_heartbeat_wait_for_retries(
    main_env, notifications
):
    main_env.setenv("SCHEDULE", "1h")
    beats = []
    main_env.setattr(monitor, "touch_heartbeat", lambda: beats.append(True))

    class FakeStop:
        requested = False

        def wait(self, seconds):
            self.requested = True  # docker stop during the first retry wait
            return True

    main_env.setattr(monitor, "_Stop", FakeStop)
    flaky_urlopen(main_env, URLError(OSError("down")))
    main_env.setattr(
        monitor,
        "run_cycle",
        lambda accounts, send_update=False: monitor.ynab_get("/b", "accounts"),
    )
    monitor.main()
    # One at startup, one at the start of the retry wait.
    assert len(beats) == 2
    assert notifications == []
