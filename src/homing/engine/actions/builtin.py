"""Built-in actions.

goto, click, fill, select, wait_for, download, print_pdf, switch_frame,
switch_tab, assert_text, custom.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog
from playwright.async_api import (
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeout,
)

from homing.engine import guard
from homing.engine.actions.base import (
    ActionContext,
    action,
    safe_file_name as _safe_name,
)
from homing.errors import ErrorCode, FinalError
from homing.manifest import FileKind
from homing.recipe.models import StepParams
from homing.resolver import LocatorNotFoundError

NETWORK_IDLE_MS = 5_000
POPUP_OPEN_S = 2.0
POPUP_CLICKS = 4
POPUPS = "[role=dialog], [role=menu], [role=listbox], dialog[open]"
RELOAD_TRIES = 4
RELOAD_WAIT_MS = 5_000
ATTR_MS = 1_000

log = structlog.get_logger(__name__)

HOOKS: dict[str, Callable[[ActionContext, StepParams], Awaitable[None]]] = {}
"""Named Python hooks for `custom: {hook: name}`.

Register with `@hook("name")`.
"""


def hook(
    name: str,
) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
    """Register a custom hook."""

    def decorator(
        func: Callable[..., Awaitable[None]],
    ) -> Callable[..., Awaitable[None]]:
        HOOKS[name] = func
        return func

    return decorator


def _require(value: str | None, what: str, params: StepParams) -> str:
    if value is None:
        msg = f"step for '{params.display}' needs `{what}`"
        raise FinalError(msg, code=ErrorCode.RECIPE_INVALID)
    return value


async def _popups(page: Page) -> int:
    return await page.locator(POPUPS).filter(visible=True).count()


async def _opened(page: Page, target: Locator, before: int) -> bool:
    """Whether a popup button opened something, within a short wait."""
    deadline = time.monotonic() + POPUP_OPEN_S
    while time.monotonic() < deadline:
        expanded = await target.get_attribute("aria-expanded", timeout=ATTR_MS)
        if expanded == "true" or await _popups(page) > before:
            return True
        await asyncio.sleep(0.1)
    return False


async def _guarded_click(ctx: ActionContext, target: Locator) -> None:
    element = await guard.describe(target)
    verdict = guard.check(ctx.runtime.loaded.recipe.write_guard, element)
    if verdict == "allow_list" and ctx.side_effect_stage:
        ctx.runtime.side_effect_committed = True
    popup = await target.get_attribute("aria-haspopup", timeout=ATTR_MS)
    page = ctx.runtime.current_page()
    before = await _popups(page) if popup not in {None, "false"} else 0
    await target.click()
    if popup in {None, "false"}:
        return
    # A popup button clicked before the page's script is ready does nothing
    # (server-rendered React). Click again until a dialog/menu shows up.
    with contextlib.suppress(PlaywrightError):
        for _ in range(POPUP_CLICKS - 1):
            if await _opened(page, target, before):
                return
            log.info("popup_click_repeated")
            await target.click()


@action("goto")
async def goto(ctx: ActionContext, params: StepParams) -> None:
    """Navigate the active tab to `url`."""
    await ctx.runtime.goto(_require(params.url, "url", params))


@action("click")
async def click(ctx: ActionContext, params: StepParams) -> None:
    """Click an element (write-guarded).

    With `popup: true`, switch to the opened tab.
    """
    if params.optional:
        found = await ctx.resolve(params, timeout_ms=params.timeout_ms or 1500)
    else:
        found = await ctx.resolve(params)
    page = ctx.runtime.current_page()
    if params.popup:
        async with page.context.expect_page() as popup_info:
            await _guarded_click(ctx, found.locator)
        popup = await popup_info.value
        await popup.wait_for_load_state("domcontentloaded")
        ctx.runtime.page = ctx.runtime.scope = popup
        return
    await _guarded_click(ctx, found.locator)
    await page.wait_for_load_state("domcontentloaded")
    ctx.runtime.raise_if_server_error()


@action("fill")
async def fill(ctx: ActionContext, params: StepParams) -> None:
    """Fill a text control and remember the value for `read_back`."""
    value = _require(params.value, "value", params)
    found = await ctx.resolve(params)
    await found.locator.fill(value)
    ctx.runtime.filled[params.display] = value


@action("select")
async def select(ctx: ActionContext, params: StepParams) -> None:
    """Pick an option from a native `<select>` or a custom menu.

    Native: by label or value. Custom menus: open, then pick the option.
    """
    value = _require(params.value, "value", params)
    found = await ctx.resolve(params)
    tag = await found.locator.evaluate("el => el.tagName.toLowerCase()")
    if tag == "select":
        try:
            await found.locator.select_option(label=value, timeout=2000)
        # Fall back to matching the option value.
        except Exception:  # noqa: BLE001
            await found.locator.select_option(value=value)
    else:
        await _guarded_click(ctx, found.locator)
        scope = ctx.runtime.current_scope()
        option = scope.get_by_role("menuitemradio", name=value).or_(
            scope.get_by_role("option", name=value),
        )
        await _guarded_click(ctx, option.filter(visible=True).first)
    ctx.runtime.filled[params.display] = value


@action("wait_for")
async def wait_for(ctx: ActionContext, params: StepParams) -> None:
    """Wait until the locator resolves (e.g. a field depending on another).

    With `reload: true` the page is reloaded between tries, for lists that
    show a new item only seconds after the write that made it.
    """
    if not params.reload:
        await ctx.resolve(params)
        return
    page = ctx.runtime.current_page()
    for _ in range(RELOAD_TRIES - 1):
        with contextlib.suppress(LocatorNotFoundError):
            await ctx.resolve(params, timeout_ms=RELOAD_WAIT_MS)
            return
        log.info("reloading_until_present", field=params.display)
        await page.reload(wait_until="domcontentloaded")
    await ctx.resolve(params, timeout_ms=RELOAD_WAIT_MS)


@action("download")
async def download(ctx: ActionContext, params: StepParams) -> None:
    """Click a link and save the resulting download under `files/`."""
    save_as = _safe_name(_require(params.save_as, "save_as", params))
    found = await ctx.resolve(params)
    page = ctx.runtime.current_page()
    async with page.expect_download() as info:
        await _guarded_click(ctx, found.locator)
    dl = await info.value
    failure = await dl.failure()
    if failure:
        msg = f"download failed: {failure}"
        raise FinalError(
            msg, code=ErrorCode.POSTCONDITION_FAILED, field=params.display
        )
    target = ctx.runtime.files_dir / save_as
    await dl.save_as(target)
    ctx.record_file(target, kind=FileKind.DOWNLOAD, compare=params.compare)


async def _quiet(page: Page) -> None:
    """Best effort "network idle"; live sites keep polling, so never forever."""
    with contextlib.suppress(PlaywrightTimeout):
        await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)


async def _print(page: Page, target: Path) -> None:
    await page.emulate_media(media="print")
    await page.pdf(path=target, format="A4", print_background=True)


@action("print_pdf")
async def print_pdf(ctx: ActionContext, params: StepParams) -> None:
    """Print the current page to PDF.

    Headed Chromium can't, so use a headless twin.
    """
    save_as = _safe_name(_require(params.save_as, "save_as", params))
    target = ctx.runtime.files_dir / save_as
    page = ctx.runtime.current_page()
    await _quiet(page)
    rt = ctx.runtime
    if not rt.spec.headed:
        await _print(page, target)
    else:
        if rt.playwright is None:
            msg = "playwright not started"
            raise RuntimeError(msg)
        twin = await rt.playwright.chromium.launch(headless=True)
        try:
            context = await twin.new_context(
                storage_state=await rt.storage_state()
            )
            twin_page = await context.new_page()
            await twin_page.goto(page.url, wait_until="load")
            await _quiet(twin_page)
            await _print(twin_page, target)
        finally:
            await twin.close()
    ctx.record_file(target, kind=FileKind.PRINT_PDF, compare=params.compare)


@action("switch_frame")
async def switch_frame(ctx: ActionContext, params: StepParams) -> None:
    """Scope later steps to an iframe (resolved like any element).

    No locator → main frame.
    """
    if not params.strategies:
        ctx.runtime.scope = ctx.runtime.current_page()
        return
    found = await ctx.resolve(params)
    handle = await found.locator.element_handle()
    frame = await handle.content_frame() if handle else None
    if frame is None:
        msg = f"'{params.display}' is not an iframe"
        raise FinalError(
            msg, code=ErrorCode.LOCATOR_NOT_FOUND, field=params.display
        )
    ctx.runtime.scope = frame


@action("switch_tab")
async def switch_tab(ctx: ActionContext, params: StepParams) -> None:
    """Switch to tab `value`: an index, `first` or `last`."""
    pages = ctx.runtime.current_page().context.pages
    which = params.value or "last"
    index = {"first": 0, "last": -1}.get(which)
    page = pages[index if index is not None else int(which)]
    await page.bring_to_front()
    ctx.runtime.page = ctx.runtime.scope = page


@action("assert_text")
async def assert_text(ctx: ActionContext, params: StepParams) -> None:
    """Fail unless `text` is visible in the current scope."""
    text = _require(params.text or params.value, "text", params)
    scope = ctx.runtime.current_scope()
    if (
        not await scope.get_by_text(text, exact=False)
        .filter(visible=True)
        .count()
    ):
        msg = f"expected text not found: {text!r}"
        raise FinalError(msg, code=ErrorCode.POSTCONDITION_FAILED)


@action("custom")
async def custom(ctx: ActionContext, params: StepParams) -> None:
    """Run a registered Python hook."""
    name = _require(params.hook, "hook", params)
    func = HOOKS.get(name)
    if func is None:
        msg = f"unknown hook '{name}'"
        raise FinalError(msg, code=ErrorCode.RECIPE_INVALID)
    await func(ctx, params)
