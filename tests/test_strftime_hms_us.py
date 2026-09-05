"""Reproduce Invalid format string from time.strftime('%H:%M:%S.%f').

time.strftime does not support %f (microseconds). datetime.strftime does.
Live crash sites: dashboard.create_task_safely and kalshi_client.fetch_orderbook.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from execution_guard import format_hms_us

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PY = (
    ROOT / "dashboard.py",
    ROOT / "kalshi_client.py",
)
_TIME_STRFTIME_US = re.compile(r"time\.strftime\([^)]*%f")


def test_time_strftime_percent_f_is_invalid_or_not_microseconds():
    """Live desk (Windows) raises ValueError: Invalid format string.

    glibc copies unknown ``%f`` as a literal, so Linux does not raise — but
    the timestamp is still wrong. Either outcome must not be used in auto-bet.
    """
    try:
        out = time.strftime("%H:%M:%S.%f", time.localtime())
    except ValueError as exc:
        assert "format" in str(exc).lower()
        return
    assert "%f" in out


def test_datetime_strftime_percent_f_works():
    stamped = datetime.fromtimestamp(1_757_090_000.123456).strftime("%H:%M:%S.%f")
    assert re.match(r"^\d{2}:\d{2}:\d{2}\.\d{6}$", stamped)


def test_format_hms_us_does_not_raise_on_create_task_safely_inputs():
    """Same call shape as the live create_task_safely / fetch_orderbook logs."""
    lock_acquired_time = time.time()
    assert re.match(r"^\d{2}:\d{2}:\d{2}\.\d{6}$", format_hms_us(lock_acquired_time))
    assert re.match(r"^\d{2}:\d{2}:\d{2}\.\d{6}$", format_hms_us())
    # None / current time must not throw the way time.strftime('%f') did.
    format_hms_us(None)


def test_runtime_paths_do_not_use_time_strftime_percent_f():
    for path in RUNTIME_PY:
        src = path.read_text(encoding="utf-8")
        assert "format_hms_us" in src, path.name
        assert _TIME_STRFTIME_US.search(src) is None, (
            f"{path.name} still calls time.strftime with %f"
        )
