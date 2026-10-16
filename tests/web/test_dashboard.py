"""Dashboard routes against hand-made manifests (no DBOS, no browser)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from homing.settings import get_settings
from homing.web.app import create_app

pytestmark = pytest.mark.unit

NOW = datetime.now(UTC)


def manifest(
    run_id,
    *,
    error=None,
    forked_from=None,
    drift=False,
    minutes_ago=5,
    attempts=1,
):
    started = NOW - timedelta(minutes=minutes_ago)
    return {
        "run_id": run_id,
        "portal": "acme",
        "credential_id": "default",
        "status": "failed" if error else "succeeded",
        "started_at": started.isoformat(),
        "finished_at": (started + timedelta(seconds=9)).isoformat(),
        "duration_ms": 9000,
        "inputs": {"plan": "PPO Gold", "member_id": "●●●"},
        "session": {"mode": "reused", "reason": "alive_check_passed"},
        "pages_visited": [],
        "plan": ["search_member", "capture_statement", "never_reached"],
        "stages": [
            {
                "key": "search_member",
                "status": "ok",
                "duration_ms": 1000,
                "attempts": attempts,
                "retries": [],
                "resolver": [
                    {
                        "field": "plan",
                        "strategy": "near_text",
                        "frame": "main",
                        "drift": drift,
                        "query": "q",
                    }
                ],
            },
            {
                "key": "capture_statement",
                "status": "failed" if error else "ok",
                "duration_ms": 8000,
                "attempts": 1,
                "retries": [],
                "resolver": [],
            },
        ],
        "files": []
        if error
        else [
            {
                "path": "files/statement.pdf",
                "bytes": 4,
                "sha256": "ab" * 32,
                "kind": "download",
            }
        ],
        "error": error,
        "provenance": {
            "homing": "0",
            "git_sha": None,
            "recipe": "acme.yaml",
            "recipe_sha256": "f" * 64,
            "inputs": "acme-member-1.json",
            "inputs_sha256": "e" * 64,
            "playwright": "1",
            "forked_from": forked_from,
        },
    }


ERROR = {
    "type": "FinalError",
    "code": "LOCATOR_NOT_FOUND",
    "message": "could not resolve 'statement'",
    "stage": "capture_statement",
    "field": "statement",
    "evidence": "evidence",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMING_HOME", str(tmp_path))
    monkeypatch.delenv("SESSION_KEY", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def write(home, m, files=None):
    run_dir = home / "runs" / m["run_id"]
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps(m))
    for rel, content in (files or {}).items():
        (run_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (run_dir / rel).write_bytes(content)


@pytest.fixture
def client(home):
    assert home.is_dir()  # settings now point at the temp home
    return TestClient(create_app(launch_dbos=False))


def test_runs_page_lists_runs_with_plain_statuses(home, client):
    write(home, manifest("acme-ok"))
    write(home, manifest("acme-bad", error=ERROR))
    page = client.get("/runs").text
    assert "Succeeded" in page
    assert "Failed" in page
    assert "Stopped at capture_statement" in page
    assert "50% succeeded" in page
    # one segment per planned stage, the unreached one included
    assert page.count('<i class="not-run"></i>') == 2


def test_attention_lists_only_unresolved_failures(home, client):
    write(home, manifest("acme-bad-1", error=ERROR))
    write(home, manifest("acme-ok"))
    page = client.get("/runs?tab=attention").text.split('id="runs-body"')[1]
    assert "acme-bad-1" in page
    assert "acme-ok" not in page


def test_failure_fixed_by_a_fork_leaves_attention(home, client):
    write(home, manifest("acme-bad", error=ERROR))
    write(
        home, manifest("acme-bad-fork-1", forked_from="acme-bad", minutes_ago=1)
    )
    assert "Nothing needs attention" in client.get("/runs?tab=attention").text
    assert "Fixed by a later run" in client.get("/runs").text


def test_old_failures_drop_out_of_attention(home, client):
    write(home, manifest("acme-ancient", error=ERROR, minutes_ago=60 * 30))
    page = client.get("/runs?tab=attention").text
    assert "acme-ancient" not in page.split('id="runs-body"')[1]


SECTIONS = ("Status", "Duration", "Session", "Started", "Stages", "Details")


def test_run_page_has_the_same_slots_whatever_the_outcome(home, client):
    write(home, manifest("acme-ok"))
    write(home, manifest("acme-bad", error=ERROR))
    ok = client.get("/runs/acme-ok").text
    bad = client.get("/runs/acme-bad").text
    for section in SECTIONS:
        assert section in ok
        assert section in bad
    for page in (ok, bad):
        assert "Sign in" in page
        assert "never_reached" in page  # planned but not run: still a row
        assert "Fork" in page
        assert "Resume" in page
    assert 'title="Nothing to resume"' in ok
    assert 'title="Nothing to resume"' not in bad


def test_stage_panel_explains_a_failure(home, client):
    attempts = [
        {
            "strategy": "role",
            "query": "role=link name='Download statement'",
            "frame": "main",
            "matches": 0,
            "rejected_reason": "no_match",
        }
    ]
    write(
        home,
        manifest("acme-bad", error=ERROR),
        {
            "evidence/screenshot.png": b"png",
            "evidence/page.html": b"<html></html>",
            "evidence/resolver_attempts.json": json.dumps(attempts).encode(),
        },
    )
    panel = client.get("/runs/acme-bad/stages/capture_statement").text
    assert "could not resolve" in panel
    assert "LOCATOR_NOT_FOUND" in panel
    assert "homing resolve --html" in panel
    assert "-f statement" in panel
    assert "role=link name=" in panel
    assert "/runs/acme-bad/evidence/screenshot.png" in panel


def test_stage_panel_for_ok_and_unreached_stages(home, client):
    write(home, manifest("acme-bad", error=ERROR))
    ok = client.get("/runs/acme-bad/stages/search_member").text
    assert "Succeeded on the first try." in ok
    assert "How to fix" in ok  # section exists, hidden: same layout
    unreached = client.get("/runs/acme-bad/stages/never_reached").text
    assert "Did not run" in unreached
    sign_in = client.get("/runs/acme-bad/stages/sign-in").text
    assert "Reused" in sign_in


def test_partial_poll_returns_only_the_body(home, client):
    write(home, manifest("acme-ok"))
    body = client.get(
        "/runs", headers={"HX-Request": "true", "HX-Target": "runs-body"}
    ).text
    assert body.lstrip().startswith('<div id="runs-body"')
    assert "<aside" not in body


def test_poll_sends_a_toast_for_runs_that_finished_since_last_poll(
    home, client
):
    write(home, manifest("acme-bad", error=ERROR, minutes_ago=0))
    since = (NOW - timedelta(minutes=5)).isoformat()
    body = client.get(
        "/runs",
        params={"since": since},
        headers={"HX-Request": "true", "HX-Target": "runs-body"},
    ).text
    assert "Run failed · acme" in body
    assert 'hx-swap-oob="beforeend:#toaster"' in body


def test_files_are_served_and_traversal_is_blocked(home, client):
    write(home, manifest("acme-ok"), {"files/statement.pdf": b"%PDF"})
    assert client.get("/runs/acme-ok/files/statement.pdf").content == b"%PDF"
    assert (
        client.get("/runs/acme-ok/files/..%2Fmanifest.json").status_code == 404
    )
    assert client.get("/runs/acme-ok/state/x").status_code == 404


def test_unknown_run_is_404(client):
    assert client.get("/runs/nope").status_code == 404


def test_settings_page(client):
    page = client.get("/settings").text
    assert "Storage" in page
    assert "sqlite" in page
