"""One run = one DBOS workflow. Stages are steps; crashes resume after last.

The workflow function is deterministic: time, randomness and I/O happen only
inside steps. Steps exchange JSON with DBOS (`to_step`); the workflow parses
it straight back into the typed results in `homing.manifest`.
"""

import contextlib
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from dbos import DBOS

from homing import __version__
from homing.engine import stages
from homing.engine.runtime import RunSpec, close_runtime, peek_runtime
from homing.manifest import (
    Manifest,
    Provenance,
    RunStatus,
    SessionInfo,
    SessionResult,
    StageResult,
    StepName,
    StepResult,
    ValidateResult,
    to_step,
)
from homing.paths import RunPaths

RUN_QUEUE = "homing-runs"
"""Queue for dashboard-started runs.

Registered in `dbos_app.register_queues`.
"""
RUN_QUEUE_WORKERS = 2
"""Two at a time per process.

Sessions are locked per portal+credential, so runs against the same portal
still go one by one.
"""

StepJson = dict[str, Any]
"""What DBOS stores for a step: a `homing.manifest` result as JSON."""


def _spec(data: StepJson) -> RunSpec:
    """Rebuild the spec; after `fork`, the run id is the *new* workflow id."""
    spec = RunSpec.model_validate(data)
    workflow_id = DBOS.workflow_id
    return (
        spec.model_copy(update={"run_id": workflow_id}) if workflow_id else spec
    )


@DBOS.step(name=StepName.VALIDATE)
async def validate_step(spec: StepJson) -> StepJson:
    """Validate recipe and inputs; records the start time."""
    return to_step(stages.validate(_spec(spec)))


@DBOS.step(name=StepName.SESSION)
async def session_step(spec: StepJson) -> StepJson:
    """Reuse the stored session if the positive alive check passes."""
    return to_step(await stages.check_session(_spec(spec)))


@DBOS.step(name=StepName.LOGIN)
async def login_step(spec: StepJson, session: StepJson) -> StepJson:
    """Full login (only when the session is dead or missing)."""
    probe = SessionResult.model_validate(session)
    return to_step(await stages.login(_spec(spec), probe))


@DBOS.step(name=StepName.STAGE)
async def stage_step(spec: StepJson, key: str) -> StepJson:
    """Run one recipe stage."""
    return to_step(await stages.run_stage(_spec(spec), key))


def _git_sha() -> str | None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        out = subprocess.run(
            # Git on PATH is expected.
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return out.stdout.strip() or None
    return None


def build_manifest(
    run: RunSpec,
    forked_from: str | None,
    validated: ValidateResult,
    session: SessionResult | None,
    results: list[StageResult],
    chromium: str | None,
) -> Manifest:
    """Assemble the manifest from typed step results."""
    steps: list[StepResult] = [
        validated,
        *([session] if session else []),
        *results,
    ]
    failure = next((s.error for s in steps if s.error is not None), None)
    finished = datetime.now(UTC)
    return Manifest(
        run_id=run.run_id,
        portal=validated.portal,
        credential_id=validated.credential_id,
        status=RunStatus.FAILED if failure else RunStatus.SUCCEEDED,
        started_at=validated.started_at,
        finished_at=finished,
        duration_ms=int(
            (finished - validated.started_at).total_seconds() * 1000
        ),
        inputs=validated.inputs,
        session=SessionInfo(
            mode=session.mode if session else None,
            reason=session.reason if session else None,
        ),
        pages_visited=[
            *(session.pages if session else []),
            *(p for r in results for p in r.pages),
        ],
        plan=validated.stages,
        stages=[r.record() for r in results],
        files=[f for r in results for f in r.files],
        error=failure,
        provenance=Provenance(
            homing=__version__,
            git_sha=_git_sha(),
            recipe=Path(run.recipe_path).name,
            recipe_sha256=validated.recipe_sha256,
            inputs=Path(run.inputs_path).name,
            inputs_sha256=validated.inputs_sha256,
            playwright=version("playwright"),
            chromium=chromium,
            forked_from=forked_from,
        ),
    )


@DBOS.step(name=StepName.FINALIZE)
async def finalize_step(
    spec: StepJson,
    validated: StepJson,
    session: StepJson | None,
    results: list[StepJson],
) -> StepJson:
    """Save the session on success, write `manifest.json`, close the browser."""
    run = _spec(spec)
    original = RunSpec.model_validate(spec).run_id
    runtime = peek_runtime(run.run_id)
    chromium = runtime.browser.version if runtime and runtime.browser else None
    manifest = build_manifest(
        run,
        forked_from=original if original != run.run_id else None,
        validated=ValidateResult.model_validate(validated),
        session=SessionResult.model_validate(session) if session else None,
        results=[StageResult.model_validate(r) for r in results],
        chromium=chromium,
    )
    if runtime is not None and manifest.status is RunStatus.SUCCEEDED:
        with contextlib.suppress(Exception):
            await runtime.save_session()
    await close_runtime(run.run_id)

    paths = RunPaths.of(run.run_id)
    paths.root.mkdir(parents=True, exist_ok=True)
    data = to_step(manifest)
    paths.manifest.write_text(
        Manifest.model_validate(data).model_dump_json(indent=2) + "\n"
    )
    return data


@DBOS.workflow(name="run_recipe")
async def run_recipe(spec: StepJson) -> StepJson:
    """Validate → session → login (skipped if alive) → stages… → finalize."""
    validated = await validate_step(spec)
    if ValidateResult.model_validate(validated).failed:
        return await finalize_step(spec, validated, None, [])

    session = await session_step(spec)
    probe = SessionResult.model_validate(session)
    if not probe.failed and not probe.alive:
        session = await login_step(spec, session)

    results: list[StepJson] = []
    if not SessionResult.model_validate(session).failed:
        for key in ValidateResult.model_validate(validated).stages:
            result = await stage_step(spec, key)
            results.append(result)
            if StageResult.model_validate(result).failed:
                break
    return await finalize_step(spec, validated, session, results)
