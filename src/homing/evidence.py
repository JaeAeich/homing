"""Failure evidence bundle under `runs/<run_id>/evidence/`, all redacted."""

import contextlib
from pathlib import Path

from playwright.async_api import Locator, Page
from pydantic import TypeAdapter

from homing.engine.runtime import Runtime, write_console
from homing.errors import HomingError
from homing.manifest import ErrorInfo
from homing.paths import EvidenceFile
from homing.recipe.models import LocatorSpec
from homing.redact import REDACTOR
from homing.resolver import Attempt

ATTEMPTS = TypeAdapter(list[Attempt])
"""(De)serializes resolver attempts for `resolver_attempts.json`."""

ALWAYS_MASK_CSS = (
    "header",
    "img.avatar",
    "img[alt^='@']",
    ".AppHeader",
    "[data-login]",
    "input[type=password]",
    "input[autocomplete=one-time-code]",
)


def _mask_locators(page: Page, specs: list[LocatorSpec]) -> list[Locator]:
    """Every element any mask spec could mean (greedy, unlike resolving)."""
    masks = [page.locator(css) for css in ALWAYS_MASK_CSS]
    for spec in specs:
        masks.extend(page.locator(css) for css in spec.css)
        if spec.role:
            masks.append(page.get_by_role(spec.role, name=spec.name))
        if spec.label:
            masks.append(page.get_by_label(spec.label))
    return masks


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


async def write_evidence(
    runtime: Runtime,
    error: HomingError,
    resolver_attempts: list[Attempt],
) -> Path:
    """Write screenshot, HTML, URL, console, stage, attempts and the error."""
    paths = runtime.paths
    paths.evidence.mkdir(parents=True, exist_ok=True)
    page = runtime.page

    if page is not None and not page.is_closed():
        with contextlib.suppress(Exception):
            masks = _mask_locators(
                page, list(runtime.loaded.recipe.redaction.mask)
            )
            await page.screenshot(
                path=paths.evidence_file(EvidenceFile.SCREENSHOT),
                full_page=True,
                mask=masks,
                mask_color="#000000",
                timeout=10_000,
            )
        with contextlib.suppress(Exception):
            html = REDACTOR.html(await page.content())
            _write(paths.evidence_file(EvidenceFile.PAGE), html)
        _write(
            paths.evidence_file(EvidenceFile.URL), REDACTOR.url(page.url) + "\n"
        )

    write_console(paths.evidence_file(EvidenceFile.CONSOLE), runtime.console)
    _write(paths.evidence_file(EvidenceFile.STAGE), runtime.stage + "\n")
    attempts_json = ATTEMPTS.dump_json(resolver_attempts, indent=2).decode()
    _write(
        paths.evidence_file(EvidenceFile.RESOLVER_ATTEMPTS),
        REDACTOR.text(attempts_json),
    )
    info = ErrorInfo(
        type=type(error).__name__,
        code=error.code,
        message=REDACTOR.text(error.message),
        stage=runtime.stage,
        field=error.field,
    )
    _write(
        paths.evidence_file(EvidenceFile.ERROR), info.model_dump_json(indent=2)
    )
    return paths.evidence
