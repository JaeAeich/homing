import json
from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from homing.recipe.loader import load_recipe

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture(scope="session")
def recipes():
    acme = json.loads(Path("inputs/acme-member-1.json").read_text())
    github = json.loads(Path("inputs/github-v0.1.0.json").read_text()) | {
        "repo": "someone/homing-scratch"
    }
    return {
        "acme": load_recipe(Path("recipes/acme.yaml"), acme).recipe,
        "github": load_recipe(Path("recipes/github.yaml"), github).recipe,
    }


@pytest.fixture(scope="session")
def acme_recipe(recipes):
    return recipes["acme"]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def browser():
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        yield b
        await b.close()


@pytest_asyncio.fixture(loop_scope="module")
async def open_html(browser):
    """Open a local HTML file with every network request blocked."""
    contexts = []

    async def _open(path: Path):
        context = await browser.new_context(offline=True)
        contexts.append(context)

        async def block(route):
            url = route.request.url
            await (
                route.continue_()
                if url.startswith(("file:", "data:", "about:"))
                else route.abort()
            )

        await context.route("**/*", block)
        page = await context.new_page()
        await page.goto(path.resolve().as_uri(), wait_until="load")
        return page

    yield _open
    for context in contexts:
        await context.close()
