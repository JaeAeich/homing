"""Stage execution: position, run steps, verify, retry transient failures.

Each public coroutine here is wrapped as one DBOS step by `workflow.py`.
They never raise: failures come back as typed results with a redacted
`ErrorInfo`, so no exception text lands in the DBOS database.
"""

import asyncio
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import structlog
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from homing.auth.flows import FLOWS, is_alive
from homing.engine.actions import ActionContext, StageJournal, run_step
from homing.engine.actions.base import safe_file_name
from homing.engine.captcha import raise_if_captcha
from homing.engine.classify import classify
from homing.engine.runtime import RunSpec, Runtime, load_for, runtime_for
from homing.errors import ErrorCode, FinalError, HomingError, RetryableError
from homing.evidence import write_evidence
from homing.manifest import (
    AliveReason,
    ErrorInfo,
    PageVisited,
    Phase,
    ResolverWin,
    SessionResult,
    StageResult,
    StageStatus,
    ValidateResult,
)
from homing.recipe.models import FileCheck, Postcondition, Stage
from homing.redact import REDACTOR
from homing.resolver import resolve
from homing.resolver.resolver import Attempt, resolve_once
from homing.session import SessionLoad, SessionMode

log = structlog.get_logger(__name__)

POST_TIMEOUT_MS = 8_000
IDEMPOTENCY_SETTLE_MS = 500
HTTP_SERVER_ERROR = 500
MASKED = "\N{BLACK CIRCLE}" * 3
"""How a sensitive input value is shown in the manifest."""


def _ms(since: float) -> int:
    return int((time.monotonic() - since) * 1000)


def _error_info(
    err: HomingError, stage: str, evidence: Path | None = None
) -> ErrorInfo:
    return ErrorInfo(
        type=type(err).__name__,
        code=err.code,
        message=REDACTOR.text(err.message),
        stage=stage,
        field=err.field,
        evidence=evidence.name if evidence else None,
    )


def _pages(runtime: Runtime | None, start: int) -> list[PageVisited]:
    if runtime is None:
        return []
    return [
        PageVisited(
            url=REDACTOR.url(v.url), title=REDACTOR.text(v.title), stage=v.stage
        )
        for v in runtime.visits[start:]
    ]


def _wins(journal: StageJournal) -> list[ResolverWin]:
    return [
        ResolverWin(
            field=r.field,
            strategy=r.strategy,
            frame=r.frame,
            drift=r.drift,
            query=r.query,
        )
        for r in journal.resolutions
    ]


async def _fail(
    runtime: Runtime | None,
    err: HomingError,
    stage: str,
    attempts: list[Attempt],
) -> ErrorInfo:
    """Write evidence (if a browser exists) and describe the failure."""
    evidence = None
    if runtime is not None:
        runtime.stage = stage
        try:
            evidence = await write_evidence(runtime, err, attempts)
        # A broken bundle must not hide the real error.
        except Exception as exc:  # noqa: BLE001
            log.warning("evidence_failed", error=type(exc).__name__)
    log.error(
        "stage_failed", stage=stage, code=err.code.value, error=err.message
    )
    return _error_info(err, stage, evidence)


def _budget(rt: Runtime) -> float:
    """Seconds a stage (or login) may take.

    The stage timeout, capped by the run deadline.
    """
    if rt.remaining_s() <= 0:
        msg = "run deadline exceeded"
        raise FinalError(msg, code=ErrorCode.TIMEOUT)
    return min(rt.settings.stage_timeout_s, rt.remaining_s())


# ---------------------------------------------------------------- validate


def validate(spec: RunSpec) -> ValidateResult:
    """Load and validate recipe + inputs. No browser."""
    started = datetime.now(UTC)
    try:
        loaded = load_for(spec)
    except Exception as exc:  # noqa: BLE001 - steps must not raise
        error = _error_info(classify(exc), Phase.VALIDATE)
        return ValidateResult(started_at=started, error=error)
    sensitive = loaded.recipe.sensitive_inputs
    for name in sensitive:
        if loaded.context.get(name):
            REDACTOR.register(str(loaded.context[name]), label=name)
    return ValidateResult(
        started_at=started,
        portal=loaded.recipe.portal,
        credential_id=loaded.recipe.credential_id,
        stages=[s.key for s in loaded.recipe.stages],
        inputs={
            k: (MASKED if k in sensitive else v)
            for k, v in loaded.inputs.items()
        },
        recipe_sha256=loaded.sha256,
        inputs_sha256=loaded.inputs_sha256,
    )


# --------------------------------------------------------- session + login


async def _probe_session(runtime: Runtime) -> SessionResult:
    stored = runtime.stored_state_reason or SessionLoad.MISSING
    if stored is not SessionLoad.LOADED:
        return SessionResult(
            reason=stored, had_state=stored is not SessionLoad.MISSING
        )
    reason = await is_alive(runtime, runtime.loaded.recipe.auth.alive_check)
    if reason is AliveReason.PASSED:
        return SessionResult(alive=True, mode=SessionMode.REUSED, reason=reason)
    return SessionResult(reason=reason, had_state=True)


async def check_session(spec: RunSpec) -> SessionResult:
    """Load the stored session and run the positive alive check."""
    runtime: Runtime | None = None
    try:
        runtime = await runtime_for(spec)
        runtime.stage = Phase.SESSION
        start = len(runtime.visits)
        budget = _budget(runtime)
        result = await asyncio.wait_for(_probe_session(runtime), timeout=budget)
        return result.model_copy(update={"pages": _pages(runtime, start)})
    # Steps must not raise; see module docstring.
    except Exception as exc:  # noqa: BLE001
        err = classify(exc)
        if isinstance(err, RetryableError):
            # A flaky alive check is not proof of life; log in instead.
            return SessionResult(reason=AliveReason.CHECK_ERROR, had_state=True)
        return SessionResult(error=await _fail(runtime, err, Phase.SESSION, []))


async def login(spec: RunSpec, session: SessionResult) -> SessionResult:
    """Full login on a clean context, then save the session.

    The code never leaves here.
    """
    runtime: Runtime | None = None
    login_start = 0
    mode = (
        SessionMode.RELOGIN_AFTER_DEAD
        if session.had_state
        else SessionMode.FRESH_LOGIN
    )
    try:
        runtime = await runtime_for(spec)
        runtime.stage = Phase.LOGIN
        login_start = len(runtime.visits)
        flow = FLOWS[runtime.loaded.recipe.auth.flow]
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(2),
            wait=wait_exponential_jitter(initial=2, max=10),
            retry=retry_if_exception(
                lambda e: isinstance(classify(e), RetryableError)
            ),
            reraise=True,
        ):
            with attempt:
                await runtime.new_context(None)
                await asyncio.wait_for(
                    flow.login(runtime), timeout=_budget(runtime)
                )
        await runtime.save_session()
    # Steps must not raise (cancellation still does).
    except Exception as exc:  # noqa: BLE001
        err = classify(exc)
        if isinstance(err, RetryableError):  # retries are over: say so
            msg = f"retries exhausted at login: {err.message}"
            err = FinalError(msg, code=ErrorCode.PORTAL_UNAVAILABLE)
        return SessionResult(error=await _fail(runtime, err, Phase.LOGIN, []))
    log.info("logged_in", mode=mode.value)
    return SessionResult(
        alive=True,
        mode=mode,
        reason=session.reason,
        pages=session.pages + _pages(runtime, login_start),
    )


# ---------------------------------------------------------------- stages


async def _check_page(ctx: ActionContext, post: Postcondition) -> None:
    page = ctx.runtime.current_page()
    scope = ctx.runtime.current_scope()
    if post.url_matches:
        try:
            await page.wait_for_url(
                re.compile(post.url_matches),
                timeout=POST_TIMEOUT_MS,
                wait_until="commit",
            )
        except Exception as exc:
            msg = f"url does not match /{post.url_matches}/"
            raise FinalError(msg, code=ErrorCode.POSTCONDITION_FAILED) from exc
    if post.text_present:
        text = scope.get_by_text(post.text_present, exact=False).filter(
            visible=True
        )
        try:
            await text.first.wait_for(timeout=POST_TIMEOUT_MS)
        except Exception as exc:
            msg = f"expected text not present: {post.text_present!r}"
            raise FinalError(msg, code=ErrorCode.POSTCONDITION_FAILED) from exc
    if post.element:
        await resolve(scope, post.element, timeout_ms=POST_TIMEOUT_MS)


async def _check_read_back(ctx: ActionContext, fields: list[str]) -> None:
    """Each filled field must still hold exactly what we typed."""
    for field in fields:
        expected = ctx.runtime.filled.get(field)
        found = next(
            (r for r in reversed(ctx.journal.resolutions) if r.field == field),
            None,
        )
        if expected is None or found is None:
            msg = f"read_back: field '{field}' was never filled"
            raise FinalError(msg, code=ErrorCode.RECIPE_INVALID, field=field)
        if await found.locator.input_value() != expected:
            msg = f"read_back: field '{field}' does not hold the value we typed"
            raise FinalError(
                msg, code=ErrorCode.POSTCONDITION_FAILED, field=field
            )


def _check_files(rt: Runtime, checks: list[FileCheck]) -> None:
    """Captured files exist, have the right magic bytes and a plausible size."""
    for check in checks:
        path = rt.files_dir / safe_file_name(check.path)
        data = path.read_bytes() if path.exists() else b""
        if not data:
            msg = f"expected file missing: {check.path}"
            raise FinalError(msg, code=ErrorCode.POSTCONDITION_FAILED)
        if check.magic and not data.startswith(check.magic.encode()):
            msg = f"{check.path} does not start with {check.magic!r}"
            raise FinalError(msg, code=ErrorCode.POSTCONDITION_FAILED)
        if len(data) < check.min_bytes:
            msg = (
                f"{check.path} is {len(data)} bytes, "
                f"expected ≥ {check.min_bytes}"
            )
            raise FinalError(msg, code=ErrorCode.POSTCONDITION_FAILED)


async def _check_post(ctx: ActionContext, post: Postcondition) -> None:
    """Success is decided by postconditions, not absence of exceptions."""
    await _check_page(ctx, post)
    await _check_read_back(ctx, post.read_back)
    _check_files(ctx.runtime, post.files)


async def _replay_to(rt: Runtime, stage: Stage) -> None:
    """Fresh browser, no `entry_url`: replay the navigation that led here."""
    stages = rt.loaded.recipe.stages
    index = stages.index(stage)
    start = next(
        (i for i in range(index - 1, -1, -1) if stages[i].entry_url), None
    )
    if start is None:
        return
    chain = stages[start:index]
    if any(s.side_effect for s in chain):
        msg = (
            f"cannot replay into '{stage.key}' through a side-effect stage; "
            "give it an entry_url"
        )
        raise FinalError(msg, code=ErrorCode.RECIPE_INVALID)
    log.info(
        "replaying_navigation", stage=stage.key, via=[s.key for s in chain]
    )
    ctx = ActionContext(runtime=rt, journal=StageJournal())
    for previous in chain:
        if previous.entry_url:
            await rt.goto(previous.entry_url)
        for step in previous.steps:
            await run_step(ctx, step)


async def _attempt(
    rt: Runtime, stage: Stage, journal: StageJournal, attempt: int
) -> None:
    if stage.entry_url:
        await rt.goto(stage.entry_url)
    elif attempt > 1 and rt.checkpoint_url:
        await rt.goto(rt.checkpoint_url)
    elif rt.fresh:
        await _replay_to(rt, stage)
    rt.fresh = False
    if rt.checkpoint_url is None:
        rt.checkpoint_url = (
            rt.current_page().url
        )  # where a retry of this stage restarts
    ctx = ActionContext(
        runtime=rt, journal=journal, side_effect_stage=stage.side_effect
    )
    for step in stage.steps:
        await run_step(ctx, step)
    await raise_if_captcha(
        rt.current_page(), rt.loaded.recipe.captcha_selectors
    )
    if stage.post:
        await _check_post(ctx, stage.post)


async def _already_done(rt: Runtime, stage: Stage) -> bool:
    """Look for the side effect's result in a separate tab.

    A separate tab, so a filled form stays filled. "Exists" means at least
    one visible match: two drafts with the same title still mean "do not
    create a third". Transient errors are retried like any stage.
    """
    check = stage.idempotency_check
    if check is None or rt.context is None:
        return False
    attempts: list[Attempt] = []
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(stage.max_attempts),
        wait=wait_exponential_jitter(initial=1, max=8),
        retry=retry_if_exception(
            lambda e: isinstance(classify(e), RetryableError)
        ),
        reraise=True,
    ):
        with attempt:
            tab = await rt.context.new_page()
            try:
                response = await tab.goto(
                    check.goto, wait_until="domcontentloaded"
                )
                if (
                    response is not None
                    and response.status >= HTTP_SERVER_ERROR
                ):
                    msg = f"HTTP {response.status} during idempotency check"
                    raise RetryableError(msg)
                await tab.wait_for_timeout(IDEMPOTENCY_SETTLE_MS)
                _, attempts = await resolve_once(tab, check.exists)
            finally:
                await tab.close()
    return any(a.matches >= 1 for a in attempts)


def _final_from(err: HomingError, rt: Runtime, attempts: int) -> HomingError:
    """Retries are over: turn the last transient error into a final one."""
    if rt.side_effect_committed:
        msg = (
            "stage failed after its write click; not retried, "
            f"check manually ({err.message})"
        )
        return FinalError(
            msg, code=ErrorCode.SIDE_EFFECT_UNCERTAIN, field=err.field
        )
    if isinstance(err, RetryableError):
        msg = f"retries exhausted after {attempts} attempts: {err.message}"
        return FinalError(
            msg, code=ErrorCode.PORTAL_UNAVAILABLE, field=err.field
        )
    return err


async def run_stage(spec: RunSpec, key: str) -> StageResult:
    """Run one stage with bounded, classified retries from its checkpoint."""
    started = time.monotonic()
    runtime: Runtime | None = None
    journal = StageJournal()
    attempts = 0
    retries: list[str] = []
    visits_start = 0
    try:
        runtime = await runtime_for(spec)
        rt = runtime
        stage = rt.loaded.recipe.stage(key)
        rt.stage = key
        rt.side_effect_committed = False
        visits_start = len(rt.visits)
        rt.checkpoint_url = None if rt.fresh else rt.current_page().url

        if await _already_done(rt, stage):
            log.info("side_effect_already_done", stage=key)
            return StageResult(
                key=key,
                status=StageStatus.SKIPPED,
                duration_ms=_ms(started),
                attempts=0,
                note="idempotency_check: result already exists, reused",
                pages=_pages(rt, visits_start),
            )

        def should_retry(exc: BaseException) -> bool:
            transient = isinstance(classify(exc), RetryableError)
            return transient and not rt.side_effect_committed

        def before_sleep(state: RetryCallState) -> None:
            exc = state.outcome.exception() if state.outcome else None
            reason = classify(exc).message if exc else "unknown"
            retries.append(reason)
            log.warning(
                "stage_retry",
                stage=key,
                attempt=state.attempt_number,
                reason=reason,
            )

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(stage.max_attempts),
            wait=wait_exponential_jitter(initial=1, max=8),
            retry=retry_if_exception(should_retry),
            before_sleep=before_sleep,
            reraise=True,
        ):
            with attempt:
                attempts = attempt.retry_state.attempt_number
                journal = StageJournal()
                work = _attempt(rt, stage, journal, attempts)
                await asyncio.wait_for(work, timeout=_budget(rt))
    # Steps must not raise (cancellation still does).
    except Exception as exc:  # noqa: BLE001
        err = classify(exc)
        if runtime is not None:
            err = _final_from(err, runtime, attempts)
        return StageResult(
            key=key,
            status=StageStatus.FAILED,
            duration_ms=_ms(started),
            attempts=attempts,
            retries=retries,
            resolver=_wins(journal),
            files=journal.files,
            pages=_pages(runtime, visits_start),
            error=await _fail(runtime, err, key, journal.last_attempts),
        )

    log.info("stage_ok", stage=key, attempts=attempts, ms=_ms(started))
    return StageResult(
        key=key,
        status=StageStatus.OK,
        duration_ms=_ms(started),
        attempts=attempts,
        retries=retries,
        resolver=_wins(journal),
        files=journal.files,
        pages=_pages(rt, visits_start),
    )
