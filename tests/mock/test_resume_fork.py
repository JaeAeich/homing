"""resume / fork / doctor: the durability commands, against the mock portal."""

import json
import shutil

import pytest

from homing.session import SessionMode
from support import ROOT

pytestmark = pytest.mark.mock


def test_fork_from_stage_reuses_earlier_steps(homing, portal):
    first = homing()
    assert first.code == 0, first.output
    logins = portal.stats().logins

    forked = homing("fork", first.run_id, "--from-stage", "fill_eligibility")
    assert forked.code == 0, forked.output
    assert forked.run_id.startswith(f"{first.run_id}-fork-")
    assert forked.output.count("stage_ok") == 3, (
        "only fill, submit and capture run again"
    )
    assert portal.stats().logins == logins
    assert forked.statement_sha == first.statement_sha
    assert forked.manifest.provenance.forked_from == first.run_id


def test_resume_after_final_failure_continues_from_failed_step(homing, portal):
    portal.faults("wrong_code_always")
    failed = homing()
    assert failed.code == 1
    assert "homing resume" in failed.output

    portal.faults("")
    resumed = homing("resume", failed.run_id)
    assert resumed.code == 0, resumed.output
    assert resumed.run_id.startswith(f"{failed.run_id}-fork-")
    assert resumed.manifest.session.mode is SessionMode.FRESH_LOGIN


def test_resume_refuses_changed_inputs(homing, portal, tmp_path):
    inputs = tmp_path / "inputs.json"
    shutil.copy(ROOT / "inputs/acme-member-1.json", inputs)
    portal.faults("wrong_code_always")
    failed = homing(inputs=str(inputs))
    assert failed.code == 1

    data = json.loads(inputs.read_text()) | {"plan": "HMO Silver"}
    inputs.write_text(json.dumps(data))
    portal.faults("")
    resumed = homing("resume", failed.run_id)
    assert resumed.code == 3, resumed.output
    assert "changed since this run started" in resumed.output


def test_unknown_run_id(homing):
    result = homing("resume", "no-such-run")
    assert result.code != 0
    assert "unknown run" in result.output


def test_doctor_on_mock(homing, portal):
    result = homing("doctor", "-r", "acme", "--base-url", portal.base_url)
    assert result.code == 0, result.output
    assert "ACME_PASSWORD is set" in result.output
    assert "chromium" in result.output
    assert "hunter" not in result.output
