"""Run lifecycle operations shared by the CLI and the dashboard.

Everything here assumes DBOS is already launched in this process
(`dbos_app.launch`). The CLI waits on the returned handles; the dashboard
enqueues and returns immediately.
"""

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dbos import StepInfo, WorkflowHandleAsync

from homing.engine.runtime import RunSpec
from homing.errors import ErrorCode, FinalError
from homing.manifest import (
    STEP_RESULTS,
    Manifest,
    RunStatus,
    StageResult,
    StepResult,
)
from homing.paths import RunPaths
from homing.recipe.loader import load_inputs, load_recipe, resolve_recipe_path
from homing.settings import get_settings

FAILED_DBOS_STATES = frozenset(
    {"ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"}
)


def invalid(message: str) -> FinalError:
    """A user error (bad argument, unknown run...)."""
    return FinalError(message, code=ErrorCode.INPUT_INVALID)


def new_run_id(portal: str) -> str:
    """`<portal>-<UTC timestamp>-<random>`: sortable and readable."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{portal}-{stamp}-{uuid.uuid4().hex[:6]}"


def fork_id(run_id: str) -> str:
    """`<original run id>-fork-<random>`.

    Forks of forks keep the original prefix.
    """
    return (
        f"{run_id.split('-fork-', maxsplit=1)[0]}-fork-{uuid.uuid4().hex[:4]}"
    )


def prepare_run(
    recipe: str,
    inputs: Path,
    *,
    base_url: str | None = None,
    headed: bool = False,
    trace: bool = False,
    run_id: str | None = None,
) -> RunSpec:
    """Validate recipe + inputs, build workflow arg. Raises `HomingError`."""
    settings = get_settings()
    path = resolve_recipe_path(recipe, settings.recipes_dir)
    data, sha = load_inputs(inputs)
    loaded = load_recipe(path, data, inputs_sha256=sha, base_url=base_url)
    return RunSpec(
        run_id=run_id or new_run_id(loaded.recipe.portal),
        recipe_path=str(path.resolve()),
        inputs_path=str(inputs.resolve()),
        base_url=base_url,
        headed=headed or settings.headed,
        trace=trace,
        recipe_sha256=loaded.sha256,
        inputs_sha256=loaded.inputs_sha256,
    )


async def start(
    spec: RunSpec, *, queued: bool = False
) -> "WorkflowHandleAsync[dict[str, Any]]":
    """Start the workflow now, or put it on the run queue (dashboard)."""
    # Heavy import, only for runs.
    from dbos import DBOS, SetWorkflowID  # noqa: PLC0415

    from homing.engine.workflow import RUN_QUEUE, run_recipe  # noqa: PLC0415

    with SetWorkflowID(spec.run_id):
        if queued:
            queue = await DBOS.retrieve_queue_async(RUN_QUEUE)
            if queue is None:
                msg = "run queue is not registered (is DBOS launched?)"
                raise RuntimeError(msg)
            return await queue.enqueue_async(run_recipe, spec.model_dump())
        return await DBOS.start_workflow_async(run_recipe, spec.model_dump())


def _step_result(step: "StepInfo") -> StepResult | None:
    """Parse a DBOS step's stored JSON into its typed result.

    Returns None for other steps.
    """
    model = STEP_RESULTS.get(step["function_name"])
    return (
        model.model_validate(step["output"])
        if model and step["output"]
        else None
    )


async def _restart_step(run_id: str, stage_key: str | None) -> int:
    """DBOS step id to fork from.

    The named stage, or else the first failed step of any kind.
    """
    from dbos import DBOS  # noqa: PLC0415

    for step in await DBOS.list_workflow_steps_async(run_id):
        result = _step_result(step)
        if result is None:
            continue
        named = isinstance(result, StageResult) and result.key == stage_key
        if (stage_key and named) or (not stage_key and result.failed):
            return int(step["function_id"])
    if stage_key:
        msg = f"stage '{stage_key}' never ran in {run_id}"
        raise invalid(msg)
    msg = f"{run_id} has no failed step to continue from"
    raise invalid(msg)


async def fork(
    run_id: str, new_id: str, stage_key: str | None
) -> "WorkflowHandleAsync[dict[str, Any]]":
    """New run reusing every step before `stage_key` (or the failed step).

    Earlier stages' documents carry over.
    """
    from dbos import DBOS, SetWorkflowID  # noqa: PLC0415

    if await DBOS.get_workflow_status_async(run_id) is None:
        msg = f"unknown run {run_id}"
        raise invalid(msg)
    step_id = await _restart_step(run_id, stage_key)
    files = RunPaths.of(run_id).files
    if files.exists():
        shutil.copytree(files, RunPaths.of(new_id).files, dirs_exist_ok=True)
    with SetWorkflowID(new_id):
        return await DBOS.fork_workflow_async(run_id, step_id)


async def resume(
    run_id: str, new_id: str
) -> tuple[str, "WorkflowHandleAsync[dict[str, Any]]"]:
    """Continue a run. Returns `(run id that will hold the result, handle)`.

    Crashed (PENDING) runs continue in place; failed ones continue as a fork
    from the failed step.
    """
    from dbos import DBOS  # noqa: PLC0415

    status = await DBOS.get_workflow_status_async(run_id)
    if status is None:
        msg = f"unknown run {run_id}"
        raise invalid(msg)
    if status.status in FAILED_DBOS_STATES:
        return run_id, await DBOS.resume_workflow_async(run_id)
    handle: WorkflowHandleAsync[
        dict[str, Any]
    ] = await DBOS.retrieve_workflow_async(run_id)
    if status.status == "SUCCESS":
        result = Manifest.model_validate(await handle.get_result())
        if result.status is RunStatus.FAILED:
            return new_id, await fork(run_id, new_id, None)
    return run_id, handle


def is_pending(run_id: str) -> bool:
    """Peek at a run's status without launching DBOS.

    Launching would start recovery.
    """
    from dbos import DBOSClient, DBOSPortableJSONSerializer  # noqa: PLC0415

    get_settings().state_dir.mkdir(parents=True, exist_ok=True)
    client = DBOSClient(
        system_database_url=get_settings().database_url,
        serializer=DBOSPortableJSONSerializer(),
    )
    try:
        status = client.retrieve_workflow(run_id).get_status()
    # Unknown id: the caller reports it properly.
    except Exception:  # noqa: BLE001
        return True
    finally:
        client.destroy()
    return str(status.status) == "PENDING"
