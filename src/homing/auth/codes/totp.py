"""RFC 6238 TOTP codes via `pyotp`."""

import asyncio
import contextlib
import time
from pathlib import Path

import pyotp

from homing.auth.codes.base import Challenge
from homing.redact import REDACTOR
from homing.secrets import SecretProvider

WINDOW_MARGIN_S = 0.5


class TotpProvider:
    """Generates the current TOTP code from a base32 secret.

    The secret is held by the `SecretProvider`.

    Portals (GitHub included) reject a code already used in its 30 s window.
    The last used *time step* (a counter, not the code) is kept in `state/` so
    even two back-to-back logins from separate processes never submit the same
    code.
    """

    def __init__(
        self,
        secrets: SecretProvider,
        secret_key: str,
        interval: int = 30,
        state_dir: Path | None = None,
    ) -> None:
        """`secret_key` is the secret's *name*, e.g. `GITHUB_TOTP_SECRET`."""
        self._secrets = secrets
        self._secret_key = secret_key
        self._interval = interval
        self._state_dir = state_dir

    def _totp(self) -> pyotp.TOTP:
        secret = self._secrets.get(self._secret_key).get_secret_value()
        return pyotp.TOTP(
            secret.replace(" ", "").upper(), interval=self._interval
        )

    def _step_file(self, challenge: Challenge) -> Path | None:
        if self._state_dir is None:
            return None
        return (
            self._state_dir
            / f"{challenge.portal}__{challenge.credential_id}.totp-step"
        )

    def _last_step(self, path: Path | None) -> int:
        if path is None or not path.exists():
            return -1
        with contextlib.suppress(ValueError):
            return int(path.read_text().strip())
        return -1

    async def _wait_for_step_after(self, last: int) -> None:
        """Sleep until the current time step is greater than `last`."""
        next_window_starts = (last + 1) * self._interval + WINDOW_MARGIN_S
        wait = next_window_starts - time.time()
        if wait > 0:
            await asyncio.sleep(wait)

    async def get_code(self, challenge: Challenge) -> str:
        """A code from a time step never used before.

        A retry always gets a new window.
        """
        totp = self._totp()
        step_file = self._step_file(challenge)
        last = self._last_step(step_file)
        if challenge.attempt > 1:
            last = max(last, int(time.time() // self._interval))
        await self._wait_for_step_after(last)
        now = time.time()
        code = totp.at(int(now))
        REDACTOR.register(code, label="CODE")
        if step_file is not None:
            step_file.parent.mkdir(parents=True, exist_ok=True)
            step_file.write_text(str(int(now // self._interval)))
        return code


class BadFirstTotpProvider(TotpProvider):
    """Mock-portal-only: returns a wrong code on attempt 1.

    Demonstrates the retry.
    """

    async def get_code(self, challenge: Challenge) -> str:
        """A deliberately wrong code first, the real one after."""
        code = await super().get_code(challenge)
        if challenge.attempt == 1:
            wrong = f"{(int(code) + 1) % 10 ** len(code):0{len(code)}d}"
            REDACTOR.register(wrong, label="CODE")
            return wrong
        return code
