"""Turn arbitrary exceptions into `RetryableError` / `FinalError`.

Messages are always redacted.
"""

import re

from playwright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeout,
)

from homing.errors import ErrorCode, FinalError, HomingError, RetryableError
from homing.redact import REDACTOR

_TRANSIENT = re.compile(
    r"net::ERR_|Navigation|navigating|"
    r"Target (page, context or browser )?(has been )?closed|"
    r"detached|Execution context was destroyed|frame was detached|"
    r"ECONNRESET|ECONNREFUSED",
    re.IGNORECASE,
)
_FIRST_LINE = 300


def classify(exc: BaseException) -> HomingError:
    """Map `exc` to our taxonomy. The returned message is always redacted."""
    if isinstance(exc, HomingError):
        exc.message = REDACTOR.text(exc.message)
        exc.args = (exc.message,)
        return exc
    if isinstance(exc, PlaywrightTimeout | TimeoutError):
        detail = (
            str(exc).splitlines()[0][:_FIRST_LINE] if str(exc) else "timeout"
        )
        return RetryableError(REDACTOR.text(f"timeout: {detail}"))
    if isinstance(exc, PlaywrightError):
        detail = (exc.message or str(exc)).splitlines()[0][:_FIRST_LINE]
        if _TRANSIENT.search(exc.message or ""):
            return RetryableError(REDACTOR.text(f"browser: {detail}"))
        return FinalError(
            REDACTOR.text(f"browser: {detail}"), code=ErrorCode.INTERNAL
        )
    if isinstance(exc, ConnectionError):
        return RetryableError(REDACTOR.text(f"network: {type(exc).__name__}"))
    return FinalError(
        REDACTOR.text(f"{type(exc).__name__}: {str(exc)[:_FIRST_LINE]}"),
        code=ErrorCode.INTERNAL,
    )
