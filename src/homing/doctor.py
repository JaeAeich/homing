"""`homing doctor`: preflight checks to run before the first real login."""

import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from homing.engine.runtime import build_secrets, build_session_store
from homing.errors import HomingError
from homing.recipe.loader import validate_recipe_file
from homing.session import SessionLoad
from homing.settings import get_settings

MAX_DRIFT_S = 15
"""Codes live 30 s and portals accept a step either side: 15 s is safe."""
USABLE_SESSION = frozenset({SessionLoad.LOADED, SessionLoad.MISSING})


@dataclass(frozen=True)
class Check:
    """One preflight result. `message` never contains a secret value."""

    ok: bool
    message: str


async def run_doctor(recipe_path: Path, base_url: str | None) -> list[Check]:
    """Run every preflight check."""
    results: list[Check] = []
    recipe = validate_recipe_file(recipe_path)
    secrets = build_secrets()
    auth = recipe.auth
    keys = [
        auth.secrets.username,
        auth.secrets.password,
        *auth.sensitive_values,
    ]
    keys.extend(auth.second_factor.secret_names)
    for key in dict.fromkeys(keys):  # once each, in order
        try:
            secrets.get(key)
            results.append(Check(ok=True, message=f"{key} is set"))
        except HomingError as exc:
            results.append(Check(ok=False, message=exc.message))

    url = base_url or recipe.base_url
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.head(url)
        server = parsedate_to_datetime(response.headers["date"]).timestamp()
        drift = abs(server - time.time())
        results.append(
            Check(
                ok=drift < MAX_DRIFT_S,
                message=(
                    f"clock drift vs {url}: {drift:.1f}s "
                    f"(TOTP needs < {MAX_DRIFT_S}s)"
                ),
            )
        )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        results.append(
            Check(
                ok=False,
                message=(
                    f"could not read server time from {url}: "
                    f"{type(exc).__name__}"
                ),
            )
        )

    try:
        store = build_session_store(get_settings(), secrets)
        loaded = store.load(recipe.portal, recipe.credential_id)
        good = loaded.reason in USABLE_SESSION
        results.append(
            Check(ok=good, message=f"stored session: {loaded.reason}")
        )
    except HomingError as exc:
        results.append(Check(ok=False, message=exc.message))

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            results.append(
                Check(ok=True, message=f"chromium {browser.version} launches")
            )
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - report any launch failure
        results.append(
            Check(
                ok=False,
                message=f"chromium failed to launch: {exc}; run `make setup`",
            )
        )
    return results
