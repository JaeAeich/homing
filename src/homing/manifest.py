"""Typed run records: DBOS step outputs and the manifest built from them.

DBOS stores step outputs as JSON. Steps build these models, `to_step` turns
them into redacted JSON at that boundary, and the workflow parses them
straight back. Nothing else in homing passes run data around as plain dicts.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from homing.errors import ErrorCode
from homing.recipe.models import CompareMode
from homing.redact import REDACTOR
from homing.session import SessionLoad, SessionMode


class RunStatus(StrEnum):
    """Outcome of a whole run."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class StageStatus(StrEnum):
    """Outcome of one recipe stage."""

    OK = "ok"
    SKIPPED = "skipped"  # its side effect already existed; reused
    FAILED = "failed"


class Phase(StrEnum):
    """Steps that are not recipe stages (used as `ErrorInfo.stage`)."""

    VALIDATE = "validate"
    SESSION = "session"
    LOGIN = "login"


class FileKind(StrEnum):
    """How a document was captured."""

    DOWNLOAD = "download"
    PRINT_PDF = "print_pdf"


class AliveReason(StrEnum):
    """Why the positive alive check passed or failed."""

    PASSED = "alive_check_passed"
    META_MISMATCH = "meta_mismatch"
    LOGGED_OUT_MARKER = "logged_out_marker"
    ELEMENT_MISSING = "element_missing"
    CHECK_ERROR = "alive_check_error"


class Record(BaseModel):
    """Base for run records: immutable, tolerant of fields older runs wrote."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class ErrorInfo(Record):
    """The final error of a step, redacted."""

    type: str
    code: ErrorCode
    message: str
    stage: str | None = None
    field: str | None = None
    evidence: str | None = None


class PageVisited(Record):
    """A main-frame navigation or popup."""

    url: str
    title: str = ""
    stage: str


class ResolverWin(Record):
    """Which strategy found a field, and whether that was a fallback (drift)."""

    field: str
    strategy: str
    frame: str
    drift: bool
    query: str


class FileRecord(Record):
    """A captured document."""

    path: str
    bytes: int
    sha256: str
    kind: FileKind
    compare: CompareMode = CompareMode.BYTES
    text_sha256: str | None = None

    @property
    def name(self) -> str:
        """File name inside `files/`."""
        return self.path.rsplit("/", 1)[-1]


class StepResult(Record):
    """Common to every step: `error` is set when the step failed."""

    error: ErrorInfo | None = None

    @property
    def failed(self) -> bool:
        """Whether the step ended in a final error."""
        return self.error is not None


class ValidateResult(StepResult):
    """Recipe + inputs loaded and checked; the run's start time."""

    started_at: datetime
    portal: str = "?"
    credential_id: str = "?"
    stages: list[str] = Field(default_factory=list)
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    recipe_sha256: str = ""
    inputs_sha256: str = ""


class SessionResult(StepResult):
    """How the run got (or failed to get) a logged-in session."""

    alive: bool = False
    mode: SessionMode | None = None
    reason: SessionLoad | AliveReason | None = None
    had_state: bool = False
    pages: list[PageVisited] = Field(default_factory=list)


class StageRecord(Record):
    """One stage as shown in the manifest."""

    key: str
    status: StageStatus
    duration_ms: int
    attempts: int
    retries: list[str] = Field(default_factory=list)
    note: str | None = None
    resolver: list[ResolverWin] = Field(default_factory=list)


class StageResult(StageRecord, StepResult):
    """What a stage step returns: the manifest record plus files and pages."""

    files: list[FileRecord] = Field(default_factory=list)
    pages: list[PageVisited] = Field(default_factory=list)

    def record(self) -> StageRecord:
        """The manifest view of this stage."""
        return StageRecord.model_validate(
            self.model_dump(include=set(StageRecord.model_fields))
        )


class SessionInfo(Record):
    """How the run obtained a logged-in session."""

    mode: SessionMode | None = None
    reason: SessionLoad | AliveReason | None = None


class Provenance(Record):
    """Exactly what produced this run."""

    homing: str
    git_sha: str | None
    recipe: str
    recipe_sha256: str
    inputs: str
    inputs_sha256: str
    playwright: str
    chromium: str | None = None
    forked_from: str | None = None


class Manifest(Record):
    """Everything a reviewer needs to trust (or debug) a run."""

    run_id: str
    portal: str
    credential_id: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    inputs: dict[str, JsonValue]
    session: SessionInfo
    pages_visited: list[PageVisited]
    # Every stage the recipe defines, in order (ran or not).
    plan: list[str] = Field(default_factory=list)
    stages: list[StageRecord]
    files: list[FileRecord]
    error: ErrorInfo | None
    provenance: Provenance


class StepName(StrEnum):
    """DBOS step names of the `run_recipe` workflow."""

    VALIDATE = "validate"
    SESSION = "session"
    LOGIN = "login"
    STAGE = "stage"
    FINALIZE = "finalize"


STEP_RESULTS: dict[str, type[StepResult]] = {
    StepName.VALIDATE: ValidateResult,
    StepName.SESSION: SessionResult,
    StepName.LOGIN: SessionResult,
    StepName.STAGE: StageResult,
}
"""DBOS step name → the result model it stores."""
SIGN_IN_STEPS = frozenset({StepName.SESSION, StepName.LOGIN})


def to_step(record: BaseModel) -> dict[str, Any]:
    """JSON for the DBOS boundary, with every string redacted."""
    return REDACTOR.mapping(record.model_dump(mode="json"))
