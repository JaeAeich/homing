"""The resolver: try strategies in order across all frames.

Exactly one visible match wins.
"""

import asyncio
import time
from dataclasses import dataclass, field as dc_field, replace

import structlog
from playwright.async_api import Error as PlaywrightError, Frame, Locator, Page

from homing.errors import ErrorCode, FinalError
from homing.recipe.models import LocatorSpec
from homing.resolver.strategies import STRATEGIES, Query, Strategy

log = structlog.get_logger(__name__)

POLL_INTERVAL_S = 0.25


@dataclass(frozen=True)
class Attempt:
    """One counted query. Kept for the manifest and the evidence bundle."""

    strategy: str
    query: str
    frame: str
    matches: int
    rejected_reason: str | None


@dataclass
class Resolution:
    """The winning element plus how it was found."""

    field: str
    strategy: str
    frame: str
    drift: bool
    query: str
    locator: Locator = dc_field(repr=False)
    attempts: list[Attempt] = dc_field(default_factory=list)


class LocatorNotFoundError(FinalError):
    """No strategy found exactly one visible element. Carries every attempt."""

    def __init__(self, field: str, attempts: list[Attempt]) -> None:
        """Build the error for `field` with what the resolver tried."""
        super().__init__(
            f"could not resolve '{field}' to exactly one visible element",
            code=ErrorCode.LOCATOR_NOT_FOUND,
            field=field,
        )
        self.attempts = attempts


@dataclass
class _Candidate:
    attempt: Attempt
    locator: Locator


def frame_label(frame: Frame) -> str:
    """Short, stable name for a frame: `main`, its name, or its URL."""
    if frame.parent_frame is None:
        return "main"
    return frame.name or frame.url[:80] or "iframe"


def frames_of(scope: Page | Frame) -> list[Frame]:
    """The scope's own frame first, then every descendant (depth-first)."""
    root = scope.main_frame if isinstance(scope, Page) else scope
    ordered: list[Frame] = []
    stack = [root]
    while stack:
        frame = stack.pop(0)
        ordered.append(frame)
        stack = list(frame.child_frames) + stack
    return [f for f in ordered if not f.is_detached()]


async def _count(locator: Locator) -> int:
    try:
        return await locator.filter(visible=True).count()
    except PlaywrightError:
        return 0


async def _collect(
    strategy: Strategy,
    frames: list[Frame],
    spec: LocatorSpec,
) -> list[_Candidate]:
    """Count every query of one strategy in every frame."""
    counted: list[_Candidate] = []
    for frame in frames:
        label = frame_label(frame)
        try:
            queries = await strategy.queries(frame, spec)
        except PlaywrightError as exc:
            error = Attempt(
                strategy.name, "<error>", label, 0, f"error: {exc.message}"
            )
            counted.append(_Candidate(error, frame.locator(":root")))
            continue
        for query in queries:
            # what was counted is what we act on
            visible = query.locator.filter(visible=True)
            matches = await _count(visible)
            query = Query(query.description, visible)  # noqa: PLW2901
            counted.append(
                _Candidate(
                    Attempt(
                        strategy.name, query.description, label, matches, None
                    ),
                    query.locator,
                ),
            )
    return counted


def _judge(
    counted: list[_Candidate],
) -> tuple[list[Attempt], _Candidate | None]:
    """Pick the winner and explain every other attempt.

    The main frame is searched first: a unique match there wins outright.
    Otherwise a query must match exactly one element across all child frames
    together.
    """
    child_hits: dict[str, int] = {}
    for c in counted:
        if c.attempt.matches == 1 and c.attempt.frame != "main":
            child_hits[c.attempt.query] = child_hits.get(c.attempt.query, 0) + 1

    unique = [c for c in counted if c.attempt.matches == 1]
    winner = next(
        (c for c in unique if c.attempt.frame == "main"), None
    ) or next(
        (c for c in unique if child_hits.get(c.attempt.query) == 1),
        None,
    )

    def reason(c: _Candidate) -> str | None:
        if c is winner:
            return None
        if c.attempt.rejected_reason:
            return c.attempt.rejected_reason
        if c.attempt.matches == 0:
            return "no_match"
        if c.attempt.matches > 1:
            return "ambiguous"
        if c.attempt.frame != "main" and child_hits.get(c.attempt.query, 0) > 1:
            return "ambiguous_across_frames"
        return "lower_priority"

    attempts = [replace(c.attempt, rejected_reason=reason(c)) for c in counted]
    if winner is not None:
        winner = _Candidate(attempts[counted.index(winner)], winner.locator)
    return attempts, winner


async def resolve_once(
    scope: Page | Frame, spec: LocatorSpec
) -> tuple[Resolution | None, list[Attempt]]:
    """A single pass over every strategy.

    Returns the winner (if any) and all attempts.
    """
    frames = frames_of(scope)
    preferred = spec.strategies[0] if spec.strategies else None
    attempts: list[Attempt] = []
    for strategy in STRATEGIES:
        if strategy.name not in spec.strategies:
            continue
        tried, winner = _judge(await _collect(strategy, frames, spec))
        attempts.extend(tried)
        if winner is not None:
            return (
                Resolution(
                    field=spec.display,
                    strategy=strategy.name,
                    frame=winner.attempt.frame,
                    drift=strategy.name != preferred,
                    query=winner.attempt.query,
                    locator=winner.locator,
                    attempts=attempts,
                ),
                attempts,
            )
    return None, attempts


async def resolve(
    scope: Page | Frame,
    spec: LocatorSpec,
    *,
    timeout_ms: int = 10_000,
) -> Resolution:
    """Resolve `spec` to exactly one visible element, polling to `timeout_ms`.

    Raises:
        LocatorNotFoundError: with every attempt in `.attempts`.
    """
    if not spec.strategies:
        msg = f"locator for '{spec.display}' has no strategies"
        raise FinalError(msg, code=ErrorCode.RECIPE_INVALID, field=spec.display)

    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        resolution, attempts = await resolve_once(scope, spec)
        if resolution is not None:
            log.info(
                "resolved",
                field=resolution.field,
                strategy=resolution.strategy,
                frame=resolution.frame,
                drift=resolution.drift,
            )
            return resolution
        if time.monotonic() >= deadline:
            raise LocatorNotFoundError(spec.display, attempts)
        await asyncio.sleep(POLL_INTERVAL_S)
