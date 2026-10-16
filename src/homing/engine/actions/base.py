"""Action protocol, registry, and the per-stage journal actions write into."""

import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

from homing.engine.runtime import Runtime
from homing.errors import ErrorCode, FinalError
from homing.manifest import FileKind, FileRecord
from homing.recipe.models import CompareMode, Step, StepParams
from homing.resolver import Attempt, LocatorNotFoundError, Resolution, resolve

PDF_MAGIC = b"%PDF"


@dataclass
class StageJournal:
    """What one stage attempt did."""

    resolutions: list[Resolution] = field(default_factory=list)
    files: list[FileRecord] = field(default_factory=list)
    last_attempts: list[Attempt] = field(default_factory=list)


@dataclass
class ActionContext:
    """Passed to every action."""

    runtime: Runtime
    journal: StageJournal
    side_effect_stage: bool = False

    async def resolve(
        self, params: StepParams, *, timeout_ms: int | None = None
    ) -> Resolution:
        """Resolve a locator in the current scope and journal the result."""
        timeout = (
            timeout_ms
            or params.timeout_ms
            or self.runtime.settings.action_timeout_ms
        )
        try:
            resolution = await resolve(
                self.runtime.current_scope(), params, timeout_ms=timeout
            )
        except LocatorNotFoundError as exc:
            self.journal.last_attempts = exc.attempts
            raise
        self.journal.resolutions.append(resolution)
        self.journal.last_attempts = resolution.attempts
        return resolution

    def record_file(
        self,
        path: Path,
        kind: FileKind,
        compare: CompareMode = CompareMode.BYTES,
    ) -> FileRecord:
        """Hash a captured file and add it to the journal."""
        data = path.read_bytes()
        record = FileRecord(
            path=str(path.relative_to(self.runtime.run_dir)),
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            kind=kind,
            compare=compare,
            text_sha256=pdf_text_sha256(path)
            if data.startswith(PDF_MAGIC)
            else None,
        )
        self.journal.files = [
            f for f in self.journal.files if f.path != record.path
        ]
        self.journal.files.append(record)
        return record


def pdf_text_sha256(path: Path) -> str:
    """Hash of a PDF's extracted text, whitespace collapsed.

    Stable across runs, timestamps aside.
    """
    reader = PdfReader(path)
    text = " ".join((page.extract_text() or "") for page in reader.pages)
    return hashlib.sha256(
        re.sub(r"\s+", " ", text).strip().encode()
    ).hexdigest()


def safe_file_name(name: str) -> str:
    """File names come from inputs; keep them a plain name inside `files/`."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name)
    return cleaned.lstrip(".") or "file"


Action = Callable[[ActionContext, StepParams], Awaitable[None]]
ACTIONS: dict[str, Action] = {}


def action(name: str) -> Callable[[Action], Action]:
    """Register an action under `name`."""

    def decorator(func: Action) -> Action:
        ACTIONS[name] = func
        return func

    return decorator


async def run_step(ctx: ActionContext, step: Step) -> None:
    """Dispatch one recipe step to its action."""
    handler = ACTIONS.get(step.action)
    if handler is None:
        msg = f"unknown action '{step.action}'"
        raise FinalError(msg, code=ErrorCode.RECIPE_INVALID)
    await handler(ctx, step.params)
