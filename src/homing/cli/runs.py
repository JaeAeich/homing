"""Run panel: `run`, `resume`, `fork`, `runs list`, `runs show`."""

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from homing import service
from homing.cli.app import (
    PANEL_RUN,
    BaseUrlOpt,
    InputsOpt,
    RecipeOpt,
    RunIdArg,
    app,
    failure,
    hint,
    invalid,
    out,
    start,
)
from homing.errors import ErrorCode, exit_code_for
from homing.manifest import Manifest, RunStatus
from homing.paths import RunPaths
from homing.settings import get_settings

WATCHDOG_MARGIN_S = 120.0
"""Login/session polling sits outside stage budgets; give it room to finish."""

runs_app = typer.Typer(no_args_is_help=True, help="Look at past runs.")
app.add_typer(runs_app, name="runs", rich_help_panel=PANEL_RUN)

Driver = Callable[[], Coroutine[Any, Any, Manifest]]


# ---------------------------------------------------------------- helpers


def _print_manifest(manifest: Manifest) -> None:
    """Summary line, stage table, files, and where to look next."""
    ok = manifest.status is RunStatus.SUCCEEDED
    mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
    session = manifest.session
    out.print(
        f"{mark} run [bold]{manifest.run_id}[/bold]  {manifest.status}  "
        f"{manifest.duration_ms} ms",
    )
    hint("session", f"{session.mode} ({session.reason})")
    table = Table(
        "stage", "status", "ms", "tries", "resolver", box=None, pad_edge=False
    )
    for stage in manifest.stages:
        wins = ", ".join(
            dict.fromkeys(  # a field waited for, then clicked: list it once
                f"{r.field}:{r.strategy}"
                + ("[yellow]*drift[/yellow]" if r.drift else "")
                for r in stage.resolver
            )
        )
        table.add_row(
            stage.key,
            stage.status,
            str(stage.duration_ms),
            str(stage.attempts),
            wins,
        )
    out.print(table)
    for f in manifest.files:  # full hashes are in the manifest
        hint("file", f"{f.path}  {f.bytes} B  {f.sha256[:12]}")
    paths = RunPaths.of(manifest.run_id)
    if manifest.error:
        e = manifest.error
        failure(e.code, f"at {e.stage}: {escape(e.message)}")
        if e.evidence:
            hint("evidence", paths.evidence)
        hint("retry", f"homing resume {manifest.run_id}")
    hint("manifest", paths.manifest)


def _finish(manifest: Manifest) -> None:
    _print_manifest(manifest)
    raise typer.Exit(
        exit_code_for(manifest.error.code) if manifest.error else 0
    )


def _drive(executor_id: str, driver: Driver) -> Manifest:
    """Launch DBOS, await the run, kill the browser if the deadline is blown.

    The watchdog leaves the workflow PENDING in DBOS, so `homing resume` can
    pick it up.
    """
    from homing import dbos_app  # noqa: PLC0415 - heavy import, only for runs
    from homing.engine.runtime import close_all  # noqa: PLC0415

    deadline = get_settings().run_timeout_s + WATCHDOG_MARGIN_S

    async def guarded() -> Manifest:
        try:
            return await asyncio.wait_for(driver(), timeout=deadline)
        except TimeoutError:
            await close_all()
            failure(
                ErrorCode.TIMEOUT,
                f"run exceeded {deadline:.0f}s; browser killed",
            )
            hint("retry", f"homing resume {executor_id}")
            raise typer.Exit(exit_code_for(ErrorCode.TIMEOUT)) from None

    dbos_app.launch(executor_id)
    try:
        return asyncio.run(guarded())
    finally:
        dbos_app.shutdown()


# ---------------------------------------------------------------- commands


@app.command(
    rich_help_panel=PANEL_RUN,
    epilog=(
        "Example: homing run -r github "
        "-i inputs/local/github-v0.1.0.json --headed"
    ),
)
def run(
    recipe: RecipeOpt,
    inputs: InputsOpt,
    base_url: BaseUrlOpt = None,
    headed: Annotated[
        bool, typer.Option("--headed", help="Show the browser.")
    ] = False,
    trace: Annotated[
        bool,
        typer.Option(
            "--trace", help="Write a local trace.zip (unredacted, never share)."
        ),
    ] = False,
    run_id: Annotated[
        str | None,
        typer.Option(
            "--run-id",
            metavar="RUN_ID",
            help="Choose the run id (default: generated).",
        ),
    ] = None,
) -> None:
    """Run a recipe with an inputs file, as one durable workflow."""
    spec = service.prepare_run(
        recipe,
        inputs,
        base_url=base_url,
        headed=headed,
        trace=trace,
        run_id=run_id,
    )
    start(
        f"run {spec.run_id}  recipe={Path(spec.recipe_path).name}  "
        f"inputs={inputs.name}"
    )

    async def go() -> Manifest:
        handle = await service.start(spec)
        return Manifest.model_validate(await handle.get_result())

    _finish(_drive(spec.run_id, go))


@app.command(
    rich_help_panel=PANEL_RUN,
    epilog="Example: homing resume github-20261016T060823-30d74f",
)
def resume(run_id: RunIdArg) -> None:
    """Continue a run: recover a crash in place, or redo a failed run.

    A failed run is redone from its failed step.
    """
    # A crashed run is recovered under its own executor id; a
    # finished-but-failed run continues as a fork with a fresh id.
    executor_id = (
        run_id if service.is_pending(run_id) else service.fork_id(run_id)
    )

    async def go() -> Manifest:
        result_id, handle = await service.resume(run_id, executor_id)
        start(
            f"resume {run_id}"
            + (f" → {result_id}" if result_id != run_id else "")
        )
        return Manifest.model_validate(await handle.get_result())

    _finish(_drive(executor_id, go))


@app.command(
    rich_help_panel=PANEL_RUN,
    epilog=(
        "Example: homing fork github-20261016T060823-30d74f "
        "--from-stage fill_form"
    ),
)
def fork(
    run_id: RunIdArg,
    from_stage: Annotated[
        str,
        typer.Option(
            "--from-stage", metavar="STAGE", help="Stage key to restart at."
        ),
    ],
) -> None:
    """Start a new run reusing every step before --from-stage (no new login)."""
    new_id = service.fork_id(run_id)

    async def go() -> Manifest:
        handle = await service.fork(run_id, new_id, from_stage)
        start(f"fork {run_id} at {from_stage} → {new_id}")
        return Manifest.model_validate(await handle.get_result())

    _finish(_drive(new_id, go))


@runs_app.command("list", epilog="Example: homing runs list")
def runs_list() -> None:
    """List runs under runs/, newest first."""
    runs_dir = get_settings().runs_dir
    run_dirs = (
        sorted(runs_dir.glob("*/"), reverse=True) if runs_dir.exists() else []
    )
    if not run_dirs:
        out.print(f"no runs yet under {runs_dir}")
        return
    table = Table("run_id", "status", "session", "ms", "error", box=None)
    for run_dir in run_dirs:
        paths = RunPaths(run_dir)
        if not paths.manifest.exists():
            table.add_row(
                run_dir.name,
                "[yellow]incomplete[/yellow]",
                "",
                "",
                "homing resume",
            )
            continue
        m = Manifest.model_validate_json(paths.manifest.read_text())
        table.add_row(
            m.run_id,
            m.status,
            str(m.session.mode or ""),
            str(m.duration_ms),
            m.error.code if m.error else "",
        )
    out.print(table)


@runs_app.command(
    "show", epilog="Example: homing runs show github-20261016T060823-30d74f"
)
def runs_show(run_id: RunIdArg) -> None:
    """Print one run's manifest summary."""
    paths = RunPaths.of(run_id)
    if not paths.manifest.exists():
        msg = (
            f"no manifest for {run_id} "
            f"(still running or crashed? try `homing resume {run_id}`)"
        )
        raise invalid(msg)
    _print_manifest(Manifest.model_validate_json(paths.manifest.read_text()))
