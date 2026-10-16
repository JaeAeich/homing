"""The evaluation table, against the mock portal.

Each test gets a fresh portal + home.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from ruamel.yaml import CommentedMap

from homing.errors import ErrorCode
from homing.manifest import (
    AliveReason,
    ErrorInfo,
    FileKind,
    Manifest,
    RunStatus,
    SessionInfo,
    StageStatus,
)
from homing.paths import EvidenceFile, RunPaths
from homing.session import SessionLoad, SessionMode
from support import ROOT, recipe_variant, stage_of

pytestmark = pytest.mark.mock

EVIDENCE = set(EvidenceFile)


def error_of(result) -> ErrorInfo:
    error = result.manifest.error
    assert error, "run should have failed"
    return error


def code_of(result) -> ErrorCode:
    return error_of(result).code


def evidence_files(result):
    return {p.name for p in result.paths.evidence.iterdir()}


def test_fresh_then_reused_session_same_document(homing, portal):
    first = homing()
    assert first.code == 0, first.output
    assert first.manifest.session == SessionInfo(
        mode=SessionMode.FRESH_LOGIN, reason=SessionLoad.MISSING
    )
    assert portal.stats().logins == 1

    second = homing()
    assert second.code == 0, second.output
    assert second.manifest.session == SessionInfo(
        mode=SessionMode.REUSED, reason=AliveReason.PASSED
    )
    assert portal.stats().logins == 1, "login must be skipped"
    assert second.statement_sha == first.statement_sha
    assert second.manifest.duration_ms < first.manifest.duration_ms
    stages = [s.key for s in first.manifest.stages]
    assert stages == [
        "search_member",
        "open_member",
        "save_note",
        "open_eligibility",
        "fill_eligibility",
        "submit_eligibility",
        "capture_statement",
    ]
    assert len(first.manifest.pages_visited) >= 5


def test_changed_inputs_give_a_different_document(homing):
    a = homing()
    b = homing(inputs="inputs/acme-member-2.json")
    assert a.code == b.code == 0, a.output + b.output
    assert a.statement_sha != b.statement_sha


def test_corrupt_state_relogs_in(homing, home):
    assert homing().code == 0
    (state,) = (home / "state").glob("acme__default.json.enc")
    state.write_bytes(b"not a fernet token")
    result = homing()
    assert result.code == 0, result.output
    assert result.manifest.session == SessionInfo(
        mode=SessionMode.RELOGIN_AFTER_DEAD, reason=SessionLoad.UNDECRYPTABLE
    )


def test_expired_session_without_redirect_is_detected(homing, portal):
    assert homing().code == 0
    # cookies still sent; portal renders a logged-out 200 page
    portal.expire_sessions()
    result = homing()
    assert result.code == 0, result.output
    assert result.manifest.session.mode is SessionMode.RELOGIN_AFTER_DEAD
    assert result.manifest.session.reason is AliveReason.META_MISMATCH
    assert portal.stats().logins == 2


def test_wrong_code_once_retries_with_a_fresh_code(homing, portal):
    portal.faults("wrong_code_once")
    result = homing()
    assert result.code == 0, result.output
    assert portal.stats().codes_rejected == 1
    assert "second_factor_rejected" in result.output
    assert "second_factor_accepted" in result.output


def test_passkey_prompt_first_switches_to_the_code_form(homing, portal):
    portal.faults("passkey_first")
    result = homing()
    assert result.code == 0, result.output
    assert "second_factor_switched_to_code_form" in result.output
    assert portal.stats().logins == 1


def test_wrong_code_always_fails_with_evidence(homing, portal):
    portal.faults("wrong_code_always")
    result = homing()
    assert result.code == 1, result.output
    assert code_of(result) is ErrorCode.AUTH_CODE_REJECTED
    assert portal.stats().codes_rejected == 2, "exactly one retry"
    assert evidence_files(result) == EVIDENCE


def test_bad_password_is_final(homing, cli_env):
    cli_env["ACME_PASSWORD"] = "wrong-password"
    result = homing()
    assert result.code == 1
    assert code_of(result) is ErrorCode.AUTH_BAD_CREDENTIALS


def test_http_500_once_is_retried_from_checkpoint(homing, portal):
    portal.faults("http500_once:/members/M-1001")
    result = homing()
    assert result.code == 0, result.output
    stage = result.stage("open_member")
    assert stage.attempts == 2
    assert "HTTP 500" in stage.retries[0]


def test_layout_v2_resolves_with_drift(homing, portal):
    portal.faults("layout=v2")
    result = homing()
    assert result.code == 0, result.output
    wins = {r.field: r for s in result.manifest.stages for r in s.resolver}
    assert wins["plan"].strategy == "near_text"
    assert wins["plan"].drift
    assert wins["check"].strategy == "structure"
    assert "drift" in result.output


def test_iframe_layout_is_found_in_child_frame(homing, portal):
    portal.faults("layout=iframe")
    result = homing()
    assert result.code == 0, result.output
    wins = {r.field: r for s in result.manifest.stages for r in s.resolver}
    assert wins["plan"].frame != "main"
    assert not wins["plan"].drift


def test_captcha_is_detected_never_solved(homing, portal):
    portal.faults("captcha_on_login")
    result = homing()
    assert result.code == 1
    assert code_of(result) is ErrorCode.CAPTCHA_ENCOUNTERED
    assert evidence_files(result) == EVIDENCE


def test_write_guard_blocks_unlisted_writes(homing, tmp_path):
    def delete_instead_of_save(recipe: CommentedMap) -> None:
        steps = stage_of(recipe, "save_note")["steps"]
        steps[-1] = {"click": {"role": "button", "name": "Delete member"}}

    recipe = recipe_variant(tmp_path, delete_instead_of_save)
    result = homing(recipe=recipe)
    assert result.code == 1
    assert code_of(result) is ErrorCode.WRITE_BLOCKED
    assert error_of(result).stage == "save_note"


def test_side_effect_stage_is_idempotent(homing, portal):
    assert homing().code == 0
    second = homing()
    assert second.code == 0
    assert second.stage("save_note").status is StageStatus.SKIPPED
    assert portal.stats().notes == 1, "no duplicate write"


def test_failure_after_write_click_is_not_retried(homing, portal):
    portal.faults("http500_once:/members/M-1001/notes")
    result = homing()
    assert result.code == 1
    error = error_of(result)
    assert error.code is ErrorCode.SIDE_EFFECT_UNCERTAIN
    assert "check manually" in error.message
    assert result.stage("save_note").attempts == 1


def test_new_tab_statement_is_captured(homing, portal, tmp_path):
    portal.faults("statement=new_tab")

    def view_and_print(recipe: CommentedMap) -> None:
        stage_of(recipe, "capture_statement")["steps"] = [
            {
                "click": {
                    "field": "statement",
                    "role": "link",
                    "name": "View statement",
                    "popup": True,
                }
            },
            {
                "print_pdf": {
                    "save_as": "statement-{{ date_of_service }}.pdf",
                    "compare": "text",
                }
            },
        ]

    recipe = recipe_variant(tmp_path, view_and_print)
    result = homing(recipe=recipe)
    assert result.code == 0, result.output
    (file,) = result.manifest.files
    assert file.kind is FileKind.PRINT_PDF
    assert file.text_sha256
    assert any(
        p.url.endswith(("/view", "dos=2026-11-02"))
        for p in result.manifest.pages_visited
    )


def test_invalid_inputs_exit_3(homing, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"member_query": "x"}))
    result = homing(inputs=str(bad))
    assert result.code == 3
    assert "INPUT_INVALID" in result.output


def test_sigkill_during_capture_then_resume(cli_env, portal, homing):
    portal.faults("slow:/statement/M-1001.pdf")
    run_id = "acme-killed-run"
    cmd = [
        sys.executable,
        "-m",
        "homing.cli",
        "--json-logs",
        "run",
        "-r",
        "acme",
        "-i",
        str(ROOT / "inputs/acme-member-1.json"),
        "--base-url",
        portal.base_url,
        "--run-id",
        run_id,
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=cli_env,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 120
    assert proc.stderr is not None
    for line in proc.stderr:
        if '"stage": "submit_eligibility"' in line and "stage_ok" in line:
            break
        assert time.monotonic() < deadline
    time.sleep(0.5)  # now inside capture_statement (download is slowed by 3 s)
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    proc.stderr.close()  # we stopped reading mid-stream; release the pipe
    logins_before = portal.stats().logins

    resumed = homing("resume", run_id)
    assert resumed.code == 0, resumed.output
    assert "stage_ok" in resumed.output
    assert resumed.output.count("stage_ok") == 1, (
        "only capture_statement runs again"
    )
    assert portal.stats().logins == logins_before, "session step was not re-run"
    paths = RunPaths(Path(cli_env["HOMING_HOME"]) / "runs" / run_id)
    manifest = Manifest.model_validate_json(paths.manifest.read_text())
    assert manifest.status is RunStatus.SUCCEEDED
    assert manifest.stages[-1].key == "capture_statement"


def test_idempotency_check_keeps_a_filled_form(homing, portal, tmp_path):
    """GitHub-shaped: form filled in one stage, saved in next (no entry_url).

    The idempotency check must not navigate the filled form away (audit bug).
    """

    def fill_in_its_own_stage(recipe: CommentedMap) -> None:
        save = stage_of(recipe, "save_note")
        fill = save["steps"].pop(0)
        fill_note = {
            "key": "fill_note",
            "entry_url": save.pop("entry_url"),
            "steps": [fill],
            "post": {"read_back": ["note"]},
        }
        stages = recipe["stages"]
        stages.insert(stages.index(save), fill_note)

    recipe = recipe_variant(tmp_path, fill_in_its_own_stage)
    first = homing(recipe=recipe)
    assert first.code == 0, first.output
    assert first.stage("save_note").status is StageStatus.OK
    second = homing(recipe=recipe)
    assert second.code == 0, second.output
    assert second.stage("save_note").status is StageStatus.SKIPPED
    assert portal.stats().notes == 1
