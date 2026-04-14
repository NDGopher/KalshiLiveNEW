"""
Regression tests: main-page tabs (Dashboard <-> Odds board).

Fast tests read the repo files / use Flask test_client.
E2E (optional): set RUN_DASHBOARD_E2E=1 to spawn dashboard.py and drive Chromium.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_template_contains_tab_markup():
    html = (REPO_ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="tab-btn-dashboard"' in html
    assert 'id="tab-btn-odds"' in html
    assert 'id="panel-alerts"' in html
    assert 'id="panel-odds"' in html
    assert "app-view-tabs" in html
    assert "app-top-sticky-wrap" in html
    assert "app-main-view-switcher" in html
    assert "kalshi-dashboard-ui-fingerprint" in html
    assert "hidden" in html  # panel-odds starts hidden until JS / ?tab=odds


def test_script_wires_header_tab_button_ids():
    js = (REPO_ROOT / "static" / "script.js").read_text(encoding="utf-8")
    assert "getElementById('tab-btn-dashboard')" in js
    assert "getElementById('tab-btn-odds')" in js
    assert "getElementById('panel-alerts')" in js
    assert "getElementById('panel-odds')" in js
    assert "[data-dashboard-tab" not in js


def test_flask_index_returns_tabbed_shell(monkeypatch):
    """Importing dashboard is heavy but validates the served page matches the template."""
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    try:
        from dashboard import app
    except Exception as e:  # pragma: no cover
        pytest.skip(f"dashboard import failed: {e}")

    with app.test_client() as c:
        r = c.get("/")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert 'id="tab-btn-dashboard"' in body
        assert 'id="tab-btn-odds"' in body
        assert 'id="panel-odds"' in body
        assert "kalshi-dashboard-ui-fingerprint" in body
        assert len(body) > 8000
        assert "script.js?v=6" in body or "script.js" in body


@pytest.mark.skipif(
    os.environ.get("RUN_DASHBOARD_E2E") != "1",
    reason="Set RUN_DASHBOARD_E2E=1 to run Playwright against a spawned dashboard (Chromium).",
)
def test_playwright_tabs_toggle_visibility():
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    def free_port() -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        _, p = s.getsockname()
        s.close()
        return int(p)

    port = free_port()
    env = os.environ.copy()
    env["DASHBOARD_PORT"] = str(port)
    env.pop("DASHBOARD_PASSWORD", None)

    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "dashboard.py")],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 120
    try:
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.3)
                if proc.poll() is not None:
                    pytest.fail(f"dashboard exited early code={proc.returncode}")
        else:
            pytest.fail("dashboard did not accept connections in time")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("#tab-btn-dashboard", timeout=30000)

            dash_hidden = page.eval_on_selector("#panel-alerts", "el => el.hidden")
            odds_hidden = page.eval_on_selector("#panel-odds", "el => el.hidden")
            assert dash_hidden is False
            assert odds_hidden is True

            page.click("#tab-btn-odds")
            page.wait_for_function(
                """() => !document.getElementById('panel-odds').hidden"""
            )
            assert page.eval_on_selector("#panel-odds", "el => el.hidden") is False
            assert page.eval_on_selector("#panel-alerts", "el => el.hidden") is True

            page.click("#tab-btn-dashboard")
            page.wait_for_function(
                """() => !document.getElementById('panel-alerts').hidden"""
            )
            assert page.eval_on_selector("#panel-alerts", "el => el.hidden") is False
            assert page.eval_on_selector("#panel-odds", "el => el.hidden") is True

            params = page.evaluate("() => new URLSearchParams(location.search).get('tab')")
            assert params is None

            page.goto(f"http://127.0.0.1:{port}/?tab=odds", wait_until="domcontentloaded")
            page.wait_for_function(
                """() => !document.getElementById('panel-odds').hidden""",
                timeout=15000,
            )
            assert page.eval_on_selector("#panel-odds", "el => el.hidden") is False

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
