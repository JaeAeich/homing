"""Human-in-the-loop code entry.

Off by default and never part of the graded path.

Deliberately *not* `DBOS.recv`: a durable message would persist the code in
the system database, and the code is typed inside the login step where `recv`
is unavailable. A hidden terminal prompt keeps the code in memory only.
"""

import asyncio
import getpass
import os

from homing.auth.codes.base import Challenge
from homing.errors import ErrorCode, FinalError
from homing.redact import REDACTOR

ENABLE_ENV = "HOMING_ALLOW_HUMAN_CODE"


class HumanProvider:
    """Asks the operator for the code on the terminal (input hidden)."""

    def __init__(self, timeout_s: float = 300.0) -> None:
        """`timeout_s` bounds the wait."""
        self._timeout_s = timeout_s

    async def get_code(self, challenge: Challenge) -> str:
        """Prompt for the code; refuse unless explicitly enabled."""
        if os.environ.get(ENABLE_ENV) != "1":
            msg = (
                "human code provider is disabled; "
                f"set {ENABLE_ENV}=1 to allow it"
            )
            raise FinalError(msg, code=ErrorCode.CONFIG_MISSING)
        prompt = (
            f"{challenge.portal} {challenge.kind} code "
            f"(attempt {challenge.attempt}): "
        )
        try:
            code = await asyncio.wait_for(
                asyncio.to_thread(getpass.getpass, prompt),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            msg = f"no code entered within {self._timeout_s:.0f}s"
            raise FinalError(msg, code=ErrorCode.AUTH_CODE_REJECTED) from None
        code = code.strip()
        REDACTOR.register(code, label="CODE")
        return code
