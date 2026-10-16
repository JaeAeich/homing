"""Per-run, in-memory browser runtime. Never checkpointed; rebuilt lazily.

`runtime_for(spec)` is the only way steps reach the browser. On a fresh
process (DBOS recovery, `homing resume`, `homing fork`) it launches Chromium,
loads the stored session and lets the stage navigate to its `entry_url`.
"""

import asyncio
import contextlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import cast

import structlog
from filelock import FileLock, Timeout as LockTimeout
from playwright.async_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Frame,
    Page,
    Playwright,
    Response,
    StorageState,
    async_playwright,
)
from pydantic import BaseModel, ConfigDict

from homing.errors import ErrorCode, FinalError, RetryableError
from homing.paths import RunPaths
from homing.recipe.loader import LoadedRecipe, load_inputs, load_recipe
from homing.redact import REDACTOR
from homing.secrets import EnvSecrets, SecretProvider
from homing.session import EncryptedFileSessionStore, SessionLoad, SessionStore
from homing.settings import Settings, get_settings

log = structlog.get_logger(__name__)

LOCK_WAIT_S = 30.0
HTTP_SERVER_ERROR = 500
CONSOLE_TEXT_LIMIT = 2000


class RunSpec(BaseModel):
    """Workflow argument: references only, never credentials."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    recipe_path: str
    inputs_path: str
    base_url: str | None = None
    headed: bool = False
    trace: bool = False
    recipe_sha256: str = ""
    inputs_sha256: str = ""


@dataclass(frozen=True)
class ConsoleLine:
    """One browser console message (text already redacted)."""

    stage: str
    type: str
    text: str


@dataclass
class PageVisit:
    """One main-frame navigation (URL redacted on the way out)."""

    url: str
    stage: str
    title: str = ""


@dataclass
class Runtime:
    """Everything a run needs in memory: browser, page, journal, secrets."""

    spec: RunSpec
    loaded: LoadedRecipe
    settings: Settings
    secrets: SecretProvider
    sessions: SessionStore
    run_dir: Path
    deadline: float
    playwright: Playwright | None = None
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    scope: Page | Frame | None = None
    stage: str = "startup"
    fresh: bool = True
    lock: FileLock | None = None
    visits: list[PageVisit] = field(default_factory=list)
    console: list[ConsoleLine] = field(default_factory=list)
    last_document_status: int | None = None
    filled: dict[str, str] = field(default_factory=dict)
    checkpoint_url: str | None = None
    side_effect_committed: bool = False
    stored_state_reason: SessionLoad | None = None

    # ------------------------------------------------------------------ paths

    @property
    def paths(self) -> RunPaths:
        """Every file and folder of this run."""
        return RunPaths(self.run_dir)

    @property
    def files_dir(self) -> Path:
        """Captured documents live here."""
        path = self.paths.files
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def portal(self) -> str:
        """Recipe portal name."""
        return self.loaded.recipe.portal

    @property
    def credential_id(self) -> str:
        """Credential the session belongs to."""
        return self.loaded.recipe.credential_id

    def remaining_s(self) -> float:
        """Seconds left before the run deadline."""
        return self.deadline - time.monotonic()

    # ------------------------------------------------------------------ browser

    async def launch(self) -> None:
        """Start Chromium; open a context with the stored session, if any."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=not self.spec.headed
        )
        loaded = self.sessions.load(self.portal, self.credential_id)
        self.stored_state_reason = loaded.reason
        await self.new_context(cast("StorageState | None", loaded.state))

    async def new_context(self, storage_state: StorageState | None) -> None:
        """Replace the browser context.

        E.g. to drop a dead session before logging in.
        """
        if self.browser is None:
            msg = "browser not launched"
            raise RuntimeError(msg)
        if self.context is not None:
            await self._stop_trace()
            await self.context.close()
        self.context = await self.browser.new_context(
            storage_state=storage_state,
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="UTC",
        )
        self.context.set_default_timeout(self.settings.action_timeout_ms)
        self.context.set_default_navigation_timeout(
            self.settings.action_timeout_ms * 2
        )
        self.context.on("page", self._watch)
        if self.spec.trace:
            await self.context.tracing.start(
                screenshots=True, snapshots=True, sources=False
            )
        self.page = await self.context.new_page()
        self.scope = self.page
        self.fresh = True

    def _watch(self, page: Page) -> None:
        """Collect console messages, navigations and document statuses."""

        def on_console(msg: ConsoleMessage) -> None:
            text = REDACTOR.text(msg.text)[:CONSOLE_TEXT_LIMIT]
            self.console.append(
                ConsoleLine(stage=self.stage, type=msg.type, text=text)
            )

        def on_nav(frame: Frame) -> None:
            if frame.parent_frame is None and frame.url not in {
                "about:blank",
                "",
            }:
                self.visits.append(PageVisit(url=frame.url, stage=self.stage))
                self.fresh = False

        def on_response(response: Response) -> None:
            if (
                response.request.is_navigation_request()
                and response.frame.parent_frame is None
            ):
                self.last_document_status = response.status

        async def on_load(loaded_page: Page) -> None:
            with contextlib.suppress(Exception):
                title = await loaded_page.title()
                for visit in reversed(self.visits):
                    if visit.url == loaded_page.url:
                        visit.title = title
                        break

        page.on("console", on_console)
        page.on("framenavigated", on_nav)
        page.on("response", on_response)
        page.on("load", on_load)

    def current_page(self) -> Page:
        """The active tab."""
        if self.page is None:
            msg = "no page open"
            raise RuntimeError(msg)
        return self.page

    def current_scope(self) -> Page | Frame:
        """The active tab or frame for locator resolution."""
        return self.scope or self.current_page()

    async def goto(self, url: str) -> None:
        """Navigate the active tab; 5xx responses are transient."""
        page = self.current_page()
        self.scope = page
        response = await page.goto(url, wait_until="domcontentloaded")
        if response is not None and response.status >= HTTP_SERVER_ERROR:
            msg = f"HTTP {response.status} from {REDACTOR.url(url)}"
            raise RetryableError(msg)

    def raise_if_server_error(self) -> None:
        """After click-triggered navigations, treat 5xx docs as transient."""
        status = self.last_document_status
        if status is not None and status >= HTTP_SERVER_ERROR:
            self.last_document_status = None
            msg = f"HTTP {status} after navigation"
            raise RetryableError(msg)

    async def storage_state(self) -> StorageState:
        """Current cookies + localStorage."""
        if self.context is None:
            msg = "no context"
            raise RuntimeError(msg)
        return await self.context.storage_state()

    async def save_session(self) -> None:
        """Persist the session.

        Cookies rotate, so save after login *and* after success.
        """
        self.sessions.save(
            self.portal, self.credential_id, dict(await self.storage_state())
        )

    async def _stop_trace(self) -> None:
        if self.spec.trace and self.context is not None:
            with contextlib.suppress(Exception):
                path = self.paths.trace(self.spec.run_id)
                await self.context.tracing.stop(path=path)

    async def close(self) -> None:
        """Close everything; safe to call twice."""
        await self._stop_trace()
        for closer in (self.context, self.browser):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(closer.close(), timeout=10)
        if self.playwright is not None:
            with contextlib.suppress(Exception):
                await self.playwright.stop()
        self.context = self.browser = self.playwright = None
        self.page = self.scope = None
        if self.lock is not None and self.lock.is_locked:
            self.lock.release()


_RUNTIMES: dict[str, Runtime] = {}


def load_for(spec: RunSpec) -> LoadedRecipe:
    """Load recipe + inputs, refusing files that changed since the run started.

    A resumed or forked run must replay *the same* recipe and inputs;
    otherwise the stages already recorded and the ones still to run would
    disagree.
    """
    inputs, inputs_sha = load_inputs(Path(spec.inputs_path))
    loaded = load_recipe(
        Path(spec.recipe_path),
        inputs,
        inputs_sha256=inputs_sha,
        base_url=spec.base_url,
    )
    if spec.recipe_sha256 and spec.recipe_sha256 != loaded.sha256:
        msg = (
            f"recipe {Path(spec.recipe_path).name} "
            "changed since this run started"
        )
        raise FinalError(msg, code=ErrorCode.RECIPE_INVALID)
    if spec.inputs_sha256 and spec.inputs_sha256 != loaded.inputs_sha256:
        msg = (
            f"inputs {Path(spec.inputs_path).name} "
            "changed since this run started"
        )
        raise FinalError(msg, code=ErrorCode.INPUT_INVALID)
    return loaded


def build_secrets() -> SecretProvider:
    """Default secret provider."""
    return EnvSecrets()


def build_session_store(
    settings: Settings, secrets: SecretProvider
) -> SessionStore:
    """Default session store, keyed by `SESSION_KEY`."""
    key = (
        settings.session_key.get_secret_value()
        if settings.session_key
        else None
    )
    if key is None:
        key = secrets.get("SESSION_KEY").get_secret_value()
    REDACTOR.register(key, label="SESSION_KEY")
    return EncryptedFileSessionStore(settings.state_dir, key)


def register_known_values(
    loaded: LoadedRecipe, secrets: SecretProvider
) -> None:
    """Pre-register all recipe-sensitive values so no output can leak them."""
    auth = loaded.recipe.auth
    for key in (
        auth.secrets.username,
        auth.secrets.password,
        *auth.sensitive_values,
    ):
        with contextlib.suppress(FinalError):
            secrets.get(key)
    for name in loaded.recipe.sensitive_inputs:
        value = loaded.context.get(name)
        if value:
            REDACTOR.register(str(value), label=name)
    for pattern in loaded.recipe.redaction.patterns:
        REDACTOR.register_pattern(pattern)


async def runtime_for(spec: RunSpec) -> Runtime:
    """Return the run's runtime, launching it on first use (`browser_for`)."""
    existing = _RUNTIMES.get(spec.run_id)
    if existing is not None and existing.browser is not None:
        return existing

    settings = get_settings()
    loaded = load_for(spec)
    secrets = build_secrets()
    register_known_values(loaded, secrets)
    run_dir = RunPaths.of(spec.run_id).root
    run_dir.mkdir(parents=True, exist_ok=True)

    sessions = build_session_store(
        settings, secrets
    )  # may fail on a bad key: before locking

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(
        settings.state_dir
        / f"{loaded.recipe.portal}__{loaded.recipe.credential_id}.lock"
    )
    try:
        lock.acquire(timeout=LOCK_WAIT_S)
    except LockTimeout:
        msg = (
            "another run is using this portal+credential session; "
            "try again shortly"
        )
        raise FinalError(msg, code=ErrorCode.SESSION_BUSY) from None

    runtime = Runtime(
        spec=spec,
        loaded=loaded,
        settings=settings,
        secrets=secrets,
        sessions=sessions,
        run_dir=run_dir,
        deadline=time.monotonic() + settings.run_timeout_s,
        lock=lock,
    )
    try:
        await runtime.launch()
    except BaseException:
        await runtime.close()
        raise
    _RUNTIMES[spec.run_id] = runtime
    log.info(
        "browser_launched",
        run_id=spec.run_id,
        session_file=runtime.stored_state_reason,
    )
    return runtime


def peek_runtime(run_id: str) -> Runtime | None:
    """The runtime if it exists in this process."""
    return _RUNTIMES.get(run_id)


async def close_all() -> None:
    """Kill every browser this process owns (run-deadline watchdog)."""
    for run_id in list(_RUNTIMES):
        await close_runtime(run_id)


async def close_runtime(run_id: str) -> None:
    """Close and forget the run's browser."""
    runtime = _RUNTIMES.pop(run_id, None)
    if runtime is not None:
        await runtime.close()


def write_console(path: Path, lines: list[ConsoleLine]) -> None:
    """Write console lines as JSON lines (text already redacted)."""
    path.write_text(
        "".join(json.dumps(asdict(line)) + "\n" for line in lines),
        encoding="utf-8",
    )
