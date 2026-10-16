"""The dashboard queues a real run and shows it.

serve → POST /runs → manifest → detail page.
"""

import subprocess
import sys
import time

import httpx
import pytest

from homing.manifest import Manifest, RunStatus
from homing.paths import RunPaths
from support import ROOT, _free_port, _port_open

pytestmark = pytest.mark.mock


@pytest.fixture
def dashboard(cli_env):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "homing.cli", "serve", "--port", str(port)],
        cwd=ROOT,
        env=cli_env,
    )
    deadline = time.monotonic() + 30
    while not _port_open(port):
        assert time.monotonic() < deadline, "dashboard did not start"
        time.sleep(0.3)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=15)


def test_new_run_from_dashboard(dashboard, portal, home):
    response = httpx.post(
        f"{dashboard}/runs",
        data={
            "recipe": "acme",
            "inputs": "inputs/acme-member-1.json",
            "base_url": portal.base_url,
        },
        headers={"HX-Request": "true"},
    )
    target = response.headers["HX-Redirect"]
    run_id = target.rsplit("/", 1)[1]

    manifest_path = RunPaths(home / "runs" / run_id).manifest
    deadline = time.monotonic() + 120
    while not manifest_path.exists():
        assert time.monotonic() < deadline, "queued run never finished"
        time.sleep(1)

    manifest = Manifest.model_validate_json(manifest_path.read_text())
    assert manifest.status is RunStatus.SUCCEEDED
    page = httpx.get(f"{dashboard}{target}").text
    assert "Succeeded" in page
    assert "capture_statement" in page
