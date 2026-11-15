"""SIGTERM ends the scheduled wait promptly (docker stop gives a container 10 s)."""

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_sigterm_stops_the_scheduled_wait():
    # A daily check that last fired two minutes ago: the next one is a day away, so
    # the process starts its wait and never runs a cycle (no YNAB or Apprise call).
    schedule = (datetime.now() - timedelta(minutes=2)).strftime("%H:%M")
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("YNAB_", "APPRISE_", "UPDATE_", "MONITOR_", "MIN_"))
    }
    env.update(
        PYTHONUNBUFFERED="1",
        YNAB_API_TOKEN="test-token",
        APPRISE_URLS="json://localhost",
        YNAB_ACCOUNT_ID_CC="a",
        SCHEDULE=schedule,
        GIT_REVISION="test-revision",
    )
    proc = subprocess.Popen(
        [sys.executable, "monitor.py"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Never hang the suite: kill the process if it doesn't get as far as its wait.
    watchdog = threading.Timer(15, proc.kill)
    watchdog.start()
    try:
        lines = []
        for line in proc.stdout:
            lines.append(line)
            if line.startswith("Next event in"):
                break
        assert "YNAB Balance Monitor starting, revision test-revision\n" in lines

        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
        assert time.monotonic() - started < 3
        assert "Shutting down..." in proc.stdout.read()
    finally:
        watchdog.cancel()
        proc.kill()
        proc.stdout.close()
