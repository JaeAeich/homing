import pytest
from pydantic import ValidationError

from homing.manifest import Manifest

pytestmark = pytest.mark.unit

BASE = {
    "run_id": "r",
    "portal": "acme",
    "credential_id": "default",
    "status": "succeeded",
    "started_at": "2026-01-01T00:00:00Z",
    "finished_at": "2026-01-01T00:00:01Z",
    "duration_ms": 1000,
    "inputs": {},
    "session": {"mode": "reused", "reason": "alive_check_passed"},
    "pages_visited": [{"url": "https://x", "stage": "s"}],
    "stages": [
        {
            "key": "s",
            "status": "ok",
            "duration_ms": 1,
            "attempts": 1,
            "resolver": [],
        }
    ],
    "files": [
        {
            "path": "files/a.pdf",
            "bytes": 1,
            "sha256": "0" * 64,
            "kind": "download",
        }
    ],
    "error": None,
    "provenance": {
        "homing": "0",
        "git_sha": None,
        "recipe": "acme.yaml",
        "recipe_sha256": "a",
        "inputs": "i.json",
        "inputs_sha256": "b",
        "playwright": "1",
    },
}


def test_valid_manifest():
    assert Manifest.model_validate(BASE).status == "succeeded"


def test_rejects_unknown_session_mode():
    with pytest.raises(ValidationError):
        Manifest.model_validate(BASE | {"session": {"mode": "magic"}})
