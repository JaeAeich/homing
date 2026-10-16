"""Locator strategies.

Each turns one part of a `LocatorSpec` into Playwright queries. A strategy
never decides the winner; it only proposes `(query, locator)` pairs. The
resolver counts visible matches and accepts a query only when exactly one
element matches.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from playwright.async_api import Frame, Locator

from homing.recipe.models import LocatorSpec, StrategyName

NEAR_TEXT_MAX_DEPTH = 4
NEAR_TEXT_MAX_ANCHORS = 5
DEFAULT_CONTROLS = (
    "input:not([type=hidden]), textarea, select, button, a[href], summary, "
    "[role=button], [role=textbox], [role=combobox], [role=link], "
    "[contenteditable=true]"
)


@dataclass(frozen=True)
class Query:
    """One concrete lookup a strategy wants counted."""

    description: str
    locator: Locator


class Strategy(Protocol):
    """Turns a spec into candidate queries inside one frame."""

    name: StrategyName

    async def queries(self, frame: Frame, spec: LocatorSpec) -> Sequence[Query]:
        """Return candidate queries; empty if the spec has nothing for this."""
        ...


def _accessible_name(spec: LocatorSpec) -> str | re.Pattern[str] | None:
    if spec.name_regex:
        return re.compile(spec.name_regex)
    return spec.name


def _control_query(scope: Locator, spec: LocatorSpec) -> tuple[str, Locator]:
    """Controls of the spec's kind in `scope` (role > tag > any interactive)."""
    if spec.role:
        return f"role={spec.role}", scope.get_by_role(spec.role)
    if spec.tag:
        css = spec.tag + (f"[type={spec.type}]" if spec.type else "")
        return css, scope.locator(css)
    return "control", scope.locator(DEFAULT_CONTROLS)


def _container(frame: Frame, spec: LocatorSpec) -> tuple[str, Frame | Locator]:
    """The spec's `within` container, or the whole frame."""
    if spec.within and spec.within.role:
        where = f"{spec.within.role}:{spec.within.name!r}"
        return where, frame.get_by_role(spec.within.role, name=spec.within.name)
    if spec.within and spec.within.css:
        return spec.within.css, frame.locator(spec.within.css)
    return "", frame


class RoleStrategy:
    """`get_by_role(role, name=...)`: what users and assistive tech see.

    With `within`, the search stays inside that container: the same link
    can appear in navigation and in the content.
    """

    name: StrategyName = "role"

    async def queries(self, frame: Frame, spec: LocatorSpec) -> Sequence[Query]:
        """Query by ARIA role and accessible name."""
        if not spec.role:
            return []
        name = _accessible_name(spec)
        where, scope = _container(frame, spec)
        loc = scope.get_by_role(spec.role, name=name, exact=spec.exact)
        shown = (spec.name_regex and f"/{spec.name_regex}/") or spec.name
        inside = f" within={where}" if where else ""
        exact = " exact" if spec.exact else ""
        return [Query(f"role={spec.role} name={shown!r}{exact}{inside}", loc)]


class LabelStrategy:
    """`get_by_label(text)`: the visible label tied to a form control."""

    name: StrategyName = "label"

    async def queries(self, frame: Frame, spec: LocatorSpec) -> Sequence[Query]:
        """Query by associated `<label>` / aria-label text."""
        if not spec.label:
            return []
        return [
            Query(
                f"label={spec.label!r}",
                frame.get_by_label(spec.label, exact=False),
            )
        ]


class NearTextStrategy:
    """Find text, walk up ≤4 ancestors, take the lone control in the nearest."""

    name: StrategyName = "near_text"

    async def queries(self, frame: Frame, spec: LocatorSpec) -> Sequence[Query]:
        """One query per ancestor holding exactly one matching control."""
        if not spec.near_text:
            return []
        anchors = frame.get_by_text(spec.near_text, exact=False)
        anchor_count = min(await anchors.count(), NEAR_TEXT_MAX_ANCHORS)
        found: list[Query] = []
        for i in range(anchor_count):
            anchor = anchors.nth(i)
            # ancestor-or-self::*[1] is the anchor itself, [2] its parent, etc.
            for depth in range(1, NEAR_TEXT_MAX_DEPTH + 2):
                container = anchor.locator(
                    f"xpath=ancestor-or-self::*[{depth}]"
                )
                kind, controls = _control_query(container, spec)
                visible = controls.filter(visible=True)
                if await visible.count() == 1:
                    desc = (
                        f"near_text={spec.near_text!r} anchor={i} "
                        f"depth={depth} {kind}"
                    )
                    found.append(Query(desc, visible))
                    break
        return found


class StructureStrategy:
    """`within` a named container, by tag/type, optionally the `nth` one."""

    name: StrategyName = "structure"

    async def queries(self, frame: Frame, spec: LocatorSpec) -> Sequence[Query]:
        """Query by container + tag + position."""
        if not (spec.within or spec.tag):
            return []
        scope: Locator = frame.locator(":root")
        where = "page"
        if spec.within and spec.within.role:
            scope = frame.get_by_role(spec.within.role, name=spec.within.name)
            where = f"{spec.within.role}:{spec.within.name!r}"
        elif spec.within and spec.within.css:
            scope = frame.locator(spec.within.css)
            where = spec.within.css
        kind, controls = _control_query(scope, spec)
        controls = controls.filter(visible=True)
        desc = f"within={where} {kind}"
        if spec.nth is not None:
            controls = controls.nth(spec.nth)
            desc += f" nth={spec.nth}"
        return [Query(desc, controls)]


class CssStrategy:
    """Each id/class/attribute selector in order. Most brittle, so last."""

    name: StrategyName = "css"

    async def queries(self, frame: Frame, spec: LocatorSpec) -> Sequence[Query]:
        """One query per CSS selector."""
        return [Query(f"css={sel}", frame.locator(sel)) for sel in spec.css]


STRATEGIES: tuple[Strategy, ...] = (
    RoleStrategy(),
    LabelStrategy(),
    NearTextStrategy(),
    StructureStrategy(),
    CssStrategy(),
)
"""Fixed order: most semantic and stable → most implementation-specific."""
