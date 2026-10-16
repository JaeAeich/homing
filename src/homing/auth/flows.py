"""Login flows.

The second-factor code is fetched and typed here and never leaves.
"""

import contextlib
import hashlib
import re
import time
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

import structlog
from playwright.async_api import Page

from homing.auth.codes import Challenge, build_code_provider
from homing.engine.captcha import raise_if_captcha
from homing.engine.runtime import Runtime
from homing.errors import ErrorCode, FinalError, RetryableError
from homing.manifest import AliveReason
from homing.recipe.models import AliveCheck, Auth, LocatorSpec, TextMarker
from homing.resolver import resolve
from homing.resolver.resolver import resolve_once

log = structlog.get_logger(__name__)

MAX_CODE_ATTEMPTS = 2
"""First code plus exactly one retry with a fresh code."""
OUTCOME_TIMEOUT_S = 15.0
AUTO_SUBMIT_GRACE_S = 1.5
"""Wait this long for an auto-submitting code form before clicking submit."""
MARKER_GRACE_S = 1.0
"""Portals may show "incorrect password" on a new URL; let the marker load."""
POLL_MS = 250


class AuthFlow(Protocol):
    """Gets a logged-in session into `runtime.context`."""

    async def login(self, runtime: Runtime) -> None:
        """Log in from scratch or raise a `FinalError` with an `AUTH_*` code."""
        ...


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _marker_present(page: Page, marker: TextMarker | None) -> bool:
    if marker is None:
        return False
    if marker.url_matches and not re.search(marker.url_matches, page.url):
        return False
    return bool(
        await page.get_by_text(marker.text, exact=False)
        .filter(visible=True)
        .count()
    )


async def _present(runtime: Runtime, spec: LocatorSpec) -> bool:
    found, _ = await resolve_once(runtime.current_scope(), spec)
    return found is not None


async def is_alive(runtime: Runtime, check: AliveCheck) -> AliveReason:
    """Positive alive check: page must *prove* we're logged in as expected user.

    GitHub serves a logged-out page (no redirect) for garbage cookies, so "not
    redirected to /login" proves nothing. We compare the `user-login` meta to
    the expected username by hash and require an element only a logged-in
    owner sees.
    """
    await runtime.goto(check.url)
    page = runtime.current_page()
    if check.meta is not None:
        expected = runtime.secrets.get(
            check.meta.equals_secret
        ).get_secret_value()
        meta = page.locator(f'meta[name="{check.meta.name}"]')
        actual = (
            await meta.get_attribute("content") if await meta.count() else None
        )
        if not actual or _sha(actual.lower()) != _sha(expected.lower()):
            return AliveReason.META_MISMATCH
    if (
        check.absent_text
        and await page.get_by_text(check.absent_text)
        .filter(visible=True)
        .count()
    ):
        return AliveReason.LOGGED_OUT_MARKER
    if check.element is not None:
        try:
            await resolve(page, check.element, timeout_ms=4000)
        except FinalError:
            return AliveReason.ELEMENT_MISSING
    return AliveReason.PASSED


class PasswordFlow:
    """Username + password, then an optional `CodeProvider` second factor.

    Never sleeps a fixed time: after each submit it polls until the page shows
    an outcome (error marker, code page, or somewhere else), so slow portals
    are not misread.
    """

    async def _fill(
        self, runtime: Runtime, spec: LocatorSpec, value: str
    ) -> None:
        found = await resolve(runtime.current_scope(), spec, timeout_ms=10_000)
        await found.locator.fill(value)

    async def _click(self, runtime: Runtime, spec: LocatorSpec) -> None:
        found = await resolve(runtime.current_scope(), spec, timeout_ms=10_000)
        await found.locator.click()

    async def _loaded(self, page: Page) -> None:
        with contextlib.suppress(Exception):
            await page.wait_for_load_state(
                "domcontentloaded", timeout=POLL_MS * 4
            )

    async def _on_code_page(self, runtime: Runtime, auth: Auth) -> bool:
        page = runtime.current_page()
        if auth.otp_url_matches and re.search(auth.otp_url_matches, page.url):
            return True
        return auth.fields.otp is not None and await _present(
            runtime, auth.fields.otp
        )

    async def _after_password(self, runtime: Runtime, auth: Auth) -> bool:
        """Poll until the password submit has an outcome.

        Returns whether a code is asked for.
        """
        page = runtime.current_page()
        login_path = urlsplit(auth.login_url).path
        captcha = runtime.loaded.recipe.captcha_selectors
        started = time.monotonic()
        while True:
            elapsed = time.monotonic() - started
            await self._loaded(page)
            await raise_if_captcha(page, captcha)
            if await _marker_present(page, auth.bad_credentials):
                msg = "portal rejected the username or password"
                raise FinalError(msg, code=ErrorCode.AUTH_BAD_CREDENTIALS)
            if await self._on_code_page(runtime, auth):
                return True
            moved_on = urlsplit(page.url).path != login_path
            if moved_on and elapsed > MARKER_GRACE_S:
                # left the login page, no code asked, no error shown: logged in
                return False
            if elapsed > OUTCOME_TIMEOUT_S:
                msg = "login page did not react to the password submit"
                raise RetryableError(msg)
            await page.wait_for_timeout(POLL_MS)

    async def _code_outcome(
        self, runtime: Runtime, auth: Auth, *, stale_marker: bool
    ) -> bool:
        """Poll after typing a code: True accepted, False rejected, else raise.

        Raising means no verdict. Many portals (GitHub) auto-submit a complete
        code; the submit button is clicked only if the page is still sitting on
        the code form after a short grace period. A rejection banner left over
        from the previous attempt only counts once a new page has loaded.
        """
        page = runtime.current_page()
        clicked = auth.fields.otp_submit is None
        loads_before = len(runtime.visits)
        started = time.monotonic()
        while time.monotonic() - started < OUTCOME_TIMEOUT_S:
            await self._loaded(page)
            if not await self._on_code_page(runtime, auth):
                return True
            fresh_page = len(runtime.visits) > loads_before
            if (fresh_page or not stale_marker) and await _marker_present(
                page, auth.code_rejected
            ):
                return False
            if not clicked and time.monotonic() - started > AUTO_SUBMIT_GRACE_S:
                clicked = True
                if auth.fields.otp_submit and await _present(
                    runtime, auth.fields.otp_submit
                ):
                    await self._click(runtime, auth.fields.otp_submit)
            await page.wait_for_timeout(POLL_MS)
        # Neither accepted nor rejected: the portal is slow, not saying no. Do
        # not burn the one code retry on that; the login step retries from
        # scratch with a fresh code.
        msg = (
            "no verdict on the second-factor code "
            "(still on the code page, no error shown)"
        )
        raise RetryableError(msg)

    async def _to_code_form(self, runtime: Runtime, auth: Auth) -> None:
        """Go to the code form if the portal opened another method first."""
        if auth.otp_entry_url is None or auth.fields.otp is None:
            return
        if await _present(runtime, auth.fields.otp):
            return
        log.info("second_factor_switched_to_code_form")
        await runtime.goto(auth.otp_entry_url)

    async def _second_factor(
        self, runtime: Runtime, auth: Auth, requested_at: datetime
    ) -> None:
        provider = build_code_provider(
            auth.second_factor,
            runtime.secrets,
            runtime.loaded.context["base_url"],
            state_dir=runtime.settings.state_dir,
        )
        if provider is None or auth.fields.otp is None:
            return
        await self._to_code_form(runtime, auth)
        not_before = requested_at
        for attempt in range(1, MAX_CODE_ATTEMPTS + 1):
            challenge = Challenge(
                portal=runtime.portal,
                credential_id=runtime.credential_id,
                kind=auth.second_factor.type,
                attempt=attempt,
                not_before=not_before,
            )
            stale_marker = await _marker_present(
                runtime.current_page(), auth.code_rejected
            )
            code = await provider.get_code(challenge)
            submitted_at = datetime.now(UTC).replace(microsecond=0)
            await self._fill(runtime, auth.fields.otp, code)
            del code
            if await self._code_outcome(
                runtime, auth, stale_marker=stale_marker
            ):
                log.info(
                    "second_factor_accepted",
                    attempt=attempt,
                    provider=auth.second_factor.type,
                )
                return
            log.warning(
                "second_factor_rejected",
                attempt=attempt,
                provider=auth.second_factor.type,
            )
            not_before = (
                submitted_at  # a resent code is newer than our submission
            )
        msg = f"second-factor code rejected {MAX_CODE_ATTEMPTS} times"
        raise FinalError(msg, code=ErrorCode.AUTH_CODE_REJECTED)

    async def login(self, runtime: Runtime) -> None:
        """Full login.

        Captcha → `CAPTCHA_ENCOUNTERED`; wrong password →
        `AUTH_BAD_CREDENTIALS`.
        """
        auth = runtime.loaded.recipe.auth
        captcha = runtime.loaded.recipe.captcha_selectors
        await runtime.goto(auth.login_url)
        page = runtime.current_page()
        await raise_if_captcha(page, captcha)

        username = runtime.secrets.get(auth.secrets.username).get_secret_value()
        password = runtime.secrets.get(auth.secrets.password).get_secret_value()
        await self._fill(runtime, auth.fields.username, username)
        await self._fill(runtime, auth.fields.password, password)
        requested_at = datetime.now(UTC).replace(microsecond=0)
        await self._click(runtime, auth.fields.submit)

        if await self._after_password(runtime, auth):
            await self._second_factor(runtime, auth, requested_at)

        for interstitial in auth.interstitials:
            await self._loaded(page)
            found, _ = await resolve_once(runtime.current_scope(), interstitial)
            if found is not None:
                log.info("interstitial_dismissed", field=interstitial.display)
                await found.locator.click()

        await self._loaded(page)
        await raise_if_captcha(page, captcha)
        reason = await is_alive(runtime, auth.alive_check)
        if reason is not AliveReason.PASSED:
            msg = f"login finished but the session is not alive ({reason})"
            raise FinalError(msg, code=ErrorCode.AUTH_BAD_CREDENTIALS)


FLOWS: dict[str, AuthFlow] = {"password": PasswordFlow()}
