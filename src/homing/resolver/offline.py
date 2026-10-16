"""Run the resolver against a saved HTML file with all network blocked."""

from playwright.async_api import Route, async_playwright

from homing.recipe.models import LocatorSpec
from homing.resolver.resolver import Attempt, Resolution, resolve_once


async def _block(route: Route) -> None:
    if route.request.url.startswith(("file:", "data:", "about:")):
        await route.continue_()
    else:
        await route.abort()


async def resolve_offline(
    html_uri: str, spec: LocatorSpec
) -> tuple[Resolution | None, list[Attempt]]:
    """Load `html_uri` (a `file://` URI, no network) and resolve `spec` once."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            context = await browser.new_context(offline=True)
            await context.route("**/*", _block)
            page = await context.new_page()
            await page.goto(html_uri, wait_until="load")
            return await resolve_once(page, spec)
        finally:
            await browser.close()
