"""Read side of the dashboard.

Finished runs come from manifests, live runs from DBOS. Every view here has
a fixed shape: a run page always has the same rows, in the same places, and
state only changes what fills them.
"""

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from homing.engine.runtime import RunSpec
from homing.errors import ErrorCode
from homing.evidence import ATTEMPTS
from homing.manifest import (
    SIGN_IN_STEPS,
    FileRecord,
    Manifest,
    Phase,
    StageRecord,
    StageResult,
    StageStatus,
    StepName,
)
from homing.paths import EvidenceFile, RunPaths
from homing.recipe.loader import validate_recipe_file
from homing.resolver import Attempt
from homing.session import SessionMode
from homing.settings import get_settings
from homing.web.hints import HintStep, hints_for

WINDOW = timedelta(hours=24)
SIGN_IN = "sign-in"
"""Key of the fixed first row: validate, session check and login."""
SIGN_IN_PHASES = frozenset({Phase.VALIDATE, Phase.SESSION, Phase.LOGIN})
SHOWN_EVIDENCE = (
    EvidenceFile.SCREENSHOT,
    EvidenceFile.PAGE,
    EvidenceFile.URL,
    EvidenceFile.CONSOLE,
    EvidenceFile.ERROR,
)
SESSION_LABELS = {
    SessionMode.REUSED: "Reused",
    SessionMode.FRESH_LOGIN: "Fresh login",
    SessionMode.RELOGIN_AFTER_DEAD: "Re-login",
}
NONE = "\N{EM DASH}"


class Tone(StrEnum):
    """State of a run or a stage: picks the dot colour and the word."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRIED = "retried"
    SKIPPED = "skipped"
    FAILED = "failed"
    NOT_RUN = "not-run"

    @property
    def label(self) -> str:
        """The word shown next to the dot."""
        return TONE_LABELS[self]


TONE_LABELS = {
    Tone.QUEUED: "Queued",
    Tone.RUNNING: "Running",
    Tone.SUCCEEDED: "Succeeded",
    Tone.RETRIED: "Retried",
    Tone.SKIPPED: "Reused",
    Tone.FAILED: "Failed",
    Tone.NOT_RUN: "Not run",
}
FINISHED = frozenset({Tone.SUCCEEDED, Tone.RETRIED, Tone.FAILED})
LIVE = frozenset({Tone.QUEUED, Tone.RUNNING})


@dataclass
class RunRow:
    """One line in the runs table."""

    run_id: str
    title: str
    portal: str
    status: Tone
    result: str
    segments: list[Tone]
    duration_ms: int | None
    started_at: datetime | None
    finished_at: datetime | None = None
    forked_from: str | None = None


@dataclass(frozen=True)
class Stats:
    """The line above the table: the last 24 hours."""

    total: int
    success_rate: float | None
    median_ms: float | None


@dataclass(frozen=True)
class StageView:
    """One row of the stage list (the sign-in row included)."""

    key: str
    label: str
    tone: Tone
    offset_ms: int | None
    duration_ms: int | None
    note: str = ""


@dataclass(frozen=True)
class RunView:
    """Everything the run page shows; the same slots for every run."""

    run_id: str
    title: str
    status: Tone
    duration_ms: int | None
    session: str
    started_at: datetime | None
    stages: list[StageView]
    total_ms: int
    recipe: str
    inputs: str
    forked_from: str | None
    files: list[FileRecord]
    failed_stage: str | None
    live: bool

    @property
    def resumable(self) -> bool:
        """Whether Resume has something to do."""
        return self.status is Tone.FAILED


@dataclass(frozen=True)
class AttemptLine:
    """One try of a stage."""

    number: int
    text: str
    outcome: str


@dataclass(frozen=True)
class LocatorLine:
    """One locator query and what it found."""

    strategy: str
    query: str
    result: str


@dataclass(frozen=True)
class StagePanel:
    """The side panel for one stage; same sections for every stage."""

    key: str
    label: str
    tone: Tone
    outcome: str
    code: ErrorCode | None
    hints: tuple[HintStep, ...]
    attempts: list[AttemptLine]
    locators: list[LocatorLine]
    evidence: list[EvidenceFile]


def _title(portal: str, inputs_file: str | None) -> str:
    stem = Path(inputs_file or "").stem
    label = stem.removeprefix(f"{portal}-") if stem else "run"
    return f"{portal} · {label}"


def _files_label(count: int) -> str:
    if count == 0:
        return "No files"
    return f"{count} file" + ("s" if count != 1 else "")


def load_manifest(run_id: str) -> Manifest | None:
    """The run's manifest, or `None` while it is running (or crashed)."""
    path = RunPaths.of(run_id).manifest
    if not path.is_file():
        return None
    return Manifest.model_validate_json(path.read_text(encoding="utf-8"))


def _plan(m: Manifest) -> list[str]:
    """Every recipe stage; older manifests only know the ones that ran."""
    return m.plan or [s.key for s in m.stages]


def _stage_tone(stage: StageRecord) -> Tone:
    if stage.status is StageStatus.FAILED:
        return Tone.FAILED
    if stage.status is StageStatus.SKIPPED:
        return Tone.SKIPPED
    return Tone.RETRIED if stage.attempts > 1 else Tone.SUCCEEDED


def _records(m: Manifest) -> dict[str, StageRecord]:
    return {s.key: s for s in m.stages}


def _segments(m: Manifest) -> list[Tone]:
    records = _records(m)
    return [
        _stage_tone(records[k]) if k in records else Tone.NOT_RUN
        for k in _plan(m)
    ]


def _run_tone(m: Manifest) -> Tone:
    if m.error:
        return Tone.FAILED
    if any(s.attempts > 1 for s in m.stages):
        return Tone.RETRIED
    return Tone.SUCCEEDED


def row_from_manifest(m: Manifest) -> RunRow:
    """Turn a manifest into a table row."""
    retried = next((s for s in m.stages if s.attempts > 1), None)
    if m.error:
        result = f"Stopped at {m.error.stage}"
    elif retried:
        result = f"{retried.attempts - 1} retry at {retried.key}"
    else:
        result = _files_label(len(m.files))
    return RunRow(
        run_id=m.run_id,
        title=_title(m.portal, m.provenance.inputs),
        portal=m.portal,
        status=_run_tone(m),
        result=result,
        segments=_segments(m),
        duration_ms=m.duration_ms,
        started_at=m.started_at,
        finished_at=m.finished_at,
        forked_from=m.provenance.forked_from,
    )


def _sign_in_view(m: Manifest, offset_end: int) -> StageView:
    failed = m.error is not None and m.error.stage in SIGN_IN_PHASES
    mode = m.session.mode
    if failed:
        tone = Tone.FAILED
    elif mode is None:
        tone = Tone.NOT_RUN
    else:
        tone = Tone.SUCCEEDED
    return StageView(
        key=SIGN_IN,
        label="Sign in",
        tone=tone,
        offset_ms=0 if tone is not Tone.NOT_RUN else None,
        duration_ms=offset_end if tone is not Tone.NOT_RUN else None,
        note=SESSION_LABELS[mode] if mode else "",
    )


def run_view(m: Manifest) -> RunView:
    """The run page for a finished run."""
    records = _records(m)
    ran = sum(s.duration_ms for s in m.stages)
    setup = max(0, m.duration_ms - ran)
    views = [_sign_in_view(m, setup)]
    offset = setup
    for key in _plan(m):
        record = records.get(key)
        if record is None:
            views.append(StageView(key, key, Tone.NOT_RUN, None, None))
            continue
        tone = _stage_tone(record)
        note = f"{record.attempts} tries" if record.attempts > 1 else ""
        views.append(
            StageView(key, key, tone, offset, record.duration_ms, note)
        )
        offset += record.duration_ms
    mode = m.session.mode
    return RunView(
        run_id=m.run_id,
        title=_title(m.portal, m.provenance.inputs),
        status=_run_tone(m),
        duration_ms=m.duration_ms,
        session=SESSION_LABELS[mode] if mode else NONE,
        started_at=m.started_at,
        stages=views,
        total_ms=max(1, m.duration_ms),
        recipe=m.provenance.recipe,
        inputs=m.provenance.inputs,
        forked_from=m.provenance.forked_from,
        files=m.files,
        failed_stage=m.error.stage if m.error else None,
        live=False,
    )


def _spec_of(workflow_input: object) -> RunSpec | None:
    """The `RunSpec` a `run_recipe` workflow was started with."""
    if not isinstance(workflow_input, dict):
        return None
    args = workflow_input.get("args") or [None]
    try:
        return RunSpec.model_validate(args[0])
    except ValidationError:
        return None


def _recipe_plan(spec: RunSpec | None) -> list[str]:
    if spec is None:
        return []
    try:
        recipe = validate_recipe_file(Path(spec.recipe_path))
    except (OSError, ValueError):
        return []
    return [s.key for s in recipe.stages]


async def _progress(run_id: str) -> tuple[bool, dict[str, StageResult]]:
    """Whether sign-in finished, and the stages that did."""
    from dbos import DBOS  # noqa: PLC0415 - only when DBOS is launched

    signed_in, done = False, {}
    for step in await DBOS.list_workflow_steps_async(run_id):
        name, output = step["function_name"], step["output"]
        if name in SIGN_IN_STEPS and output:
            signed_in = True
        if name == StepName.STAGE and output:
            result = StageResult.model_validate(output)
            done[result.key] = result
    return signed_in, done


async def live_view(run_id: str) -> RunView | None:
    """The run page for a queued or running run (no manifest yet)."""
    from dbos import DBOS  # noqa: PLC0415

    status = await DBOS.get_workflow_status_async(run_id)
    if status is None:
        return None
    spec = _spec_of(status.input)
    portal = Path(spec.recipe_path).stem if spec else "run"
    started = datetime.fromtimestamp((status.created_at or 0) / 1000, tz=UTC)
    signed_in, done = await _progress(run_id)
    queued = status.status == "ENQUEUED"
    views = [
        StageView(
            SIGN_IN,
            "Sign in",
            Tone.SUCCEEDED if signed_in else Tone.RUNNING,
            None,
            None,
        )
    ]
    if queued:
        views[0] = StageView(SIGN_IN, "Sign in", Tone.NOT_RUN, None, None)
    current_found = not signed_in or queued
    for key in _recipe_plan(spec):
        if key in done:
            record = done[key]
            tone = _stage_tone(record.record())
            views.append(StageView(key, key, tone, None, record.duration_ms))
        elif not current_found:
            views.append(StageView(key, key, Tone.RUNNING, None, None))
            current_found = True
        else:
            views.append(StageView(key, key, Tone.NOT_RUN, None, None))
    elapsed = int((datetime.now(UTC) - started).total_seconds() * 1000)
    return RunView(
        run_id=run_id,
        title=_title(portal, spec.inputs_path if spec else None),
        status=Tone.QUEUED if queued else Tone.RUNNING,
        duration_ms=None if queued else elapsed,
        session=NONE,
        started_at=started,
        stages=views,
        total_ms=max(1, elapsed),
        recipe=Path(spec.recipe_path).name if spec else NONE,
        inputs=Path(spec.inputs_path).name if spec else NONE,
        forked_from=None,
        files=[f for r in done.values() for f in r.files],
        failed_stage=None,
        live=True,
    )


async def live_rows(known: set[str]) -> list[RunRow]:
    """Queued and running workflows that have no manifest yet."""
    from dbos import DBOS  # noqa: PLC0415

    rows: list[RunRow] = []
    workflows = await DBOS.list_workflows_async(
        status=["PENDING", "ENQUEUED"], load_output=False
    )
    for wf in workflows:
        if wf.workflow_id in known:
            continue
        view = await live_view(wf.workflow_id)
        if view is None:
            continue
        current = next(
            (s.label for s in view.stages if s.tone is Tone.RUNNING), ""
        )
        rows.append(
            RunRow(
                run_id=view.run_id,
                title=view.title,
                portal=view.recipe.removesuffix(".yaml"),
                status=view.status,
                result=current or "Waiting to start",
                segments=[s.tone for s in view.stages[1:]],
                duration_ms=view.duration_ms,
                started_at=view.started_at,
            ),
        )
    return rows


def finished_rows() -> list[RunRow]:
    """Every run with a readable manifest, newest first."""
    runs_dir = get_settings().runs_dir
    rows = []
    paths = runs_dir.glob("*/manifest.json") if runs_dir.exists() else []
    for path in paths:
        try:
            manifest = Manifest.model_validate_json(path.read_bytes())
        except ValidationError:
            continue
        rows.append(row_from_manifest(manifest))
    fixed = resolved_ids(rows)
    for row in rows:
        if row.status is Tone.FAILED and row.run_id in fixed:
            row.result = "Fixed by a later run"
    oldest = datetime.min.replace(tzinfo=UTC)
    rows.sort(key=lambda r: r.started_at or oldest, reverse=True)
    return rows


def resolved_ids(rows: list[RunRow]) -> set[str]:
    """Failed runs that a later resume/fork already fixed."""
    return {
        r.forked_from
        for r in rows
        if r.forked_from and r.status is not Tone.FAILED
    }


def unresolved(rows: list[RunRow]) -> list[RunRow]:
    """Failed runs from the last 24 h that nothing has fixed yet."""
    now = datetime.now(UTC)
    done = resolved_ids(rows)
    return [
        r
        for r in rows
        if r.status is Tone.FAILED
        and r.run_id not in done
        and (r.started_at is None or now - r.started_at <= WINDOW)
    ]


def stats(rows: list[RunRow]) -> Stats:
    """Success rate and median duration over the last 24 h."""
    now = datetime.now(UTC)
    recent = [r for r in rows if r.started_at and now - r.started_at <= WINDOW]
    finished = [r for r in recent if r.status in FINISHED]
    ok = [r for r in finished if r.status is not Tone.FAILED]
    durations = [r.duration_ms for r in finished if r.duration_ms is not None]
    return Stats(
        total=len(recent),
        success_rate=(len(ok) / len(finished)) if finished else None,
        median_ms=statistics.median(durations) if durations else None,
    )


def evidence(run_id: str) -> list[EvidenceFile]:
    """Evidence files that exist for a run."""
    paths = RunPaths.of(run_id)
    return [f for f in SHOWN_EVIDENCE if paths.evidence_file(f).is_file()]


def resolver_attempts(run_id: str) -> list[Attempt]:
    """What the resolver tried at the failing step."""
    path = RunPaths.of(run_id).evidence_file(EvidenceFile.RESOLVER_ATTEMPTS)
    if not path.is_file():
        return []
    return ATTEMPTS.validate_json(path.read_bytes())


def _attempts(record: StageRecord, failure: str | None) -> list[AttemptLine]:
    lines = [
        AttemptLine(i, text, "retried")
        for i, text in enumerate(record.retries, 1)
    ]
    last = failure or ("Reused" if record.status is StageStatus.SKIPPED else "")
    lines.append(AttemptLine(len(lines) + 1, last or "Succeeded", "final"))
    return lines


def _outcome(record: StageRecord | None, tone: Tone) -> str:
    if record is None:
        return "Did not run: an earlier step failed."
    if tone is Tone.SKIPPED:
        return record.note or "Already done; reused, so nothing was written."
    if record.attempts > 1:
        return f"Succeeded after {record.attempts} tries."
    return "Succeeded on the first try."


def stage_panel(m: Manifest, key: str) -> StagePanel | None:
    """The side panel for stage `key` (or the sign-in row)."""
    if key == SIGN_IN:
        sign_in = _sign_in_view(m, 0)
        record, tone = None, sign_in.tone
        reason = m.session.reason
        outcome = f"{sign_in.note}: {reason}." if reason else sign_in.note
        failed = tone is Tone.FAILED
    elif key in _plan(m):
        record = _records(m).get(key)
        tone = _stage_tone(record) if record else Tone.NOT_RUN
        outcome = _outcome(record, tone)
        failed = m.error is not None and m.error.stage == key
    else:
        return None
    error = m.error if failed else None
    if error is None:
        wins = record.resolver if record else []
        return StagePanel(
            key=key,
            label="Sign in" if key == SIGN_IN else key,
            tone=tone,
            outcome=outcome or NONE,
            code=None,
            hints=(),
            attempts=_attempts(record, None) if record else [],
            locators=[
                LocatorLine(
                    w.strategy,
                    w.query,
                    "found by fallback" if w.drift else "found",
                )
                for w in wins
            ],
            evidence=[],
        )
    page = RunPaths.of(m.run_id).evidence_file(EvidenceFile.PAGE)
    return StagePanel(
        key=key,
        label="Sign in" if key == SIGN_IN else key,
        tone=tone,
        outcome=error.message,
        code=error.code,
        hints=hints_for(
            error.code,
            portal=m.portal,
            field=error.field,
            page=str(page) if page.is_file() else None,
        ),
        attempts=_attempts(record, error.message) if record else [],
        locators=[
            LocatorLine(a.strategy, a.query, f"{a.matches} found")
            for a in resolver_attempts(m.run_id)
        ],
        evidence=evidence(m.run_id),
    )
