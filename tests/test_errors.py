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
    notifier = monitor._build_notifier(f"json://localhost, ,{BAD_URL}", variable)
    assert len(notifier) == 1
    err = capsys.readouterr().err
    # Entry 3: the blank entry still counts, so the number matches the variable.
    assert f"Apprise: {variable} entry 3 failed to load" in err
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
    assert len(notifications) == 1
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
