"""Error taxonomy. The retry classifier only looks at the exception *type*."""

from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable codes surfaced in the manifest, evidence and exit status."""

    TRANSIENT = "TRANSIENT"
    AUTH_BAD_CREDENTIALS = "AUTH_BAD_CREDENTIALS"
    AUTH_CODE_REJECTED = "AUTH_CODE_REJECTED"
    LOCATOR_NOT_FOUND = "LOCATOR_NOT_FOUND"
    POSTCONDITION_FAILED = "POSTCONDITION_FAILED"
    CAPTCHA_ENCOUNTERED = "CAPTCHA_ENCOUNTERED"
    WRITE_BLOCKED = "WRITE_BLOCKED"
    SIDE_EFFECT_UNCERTAIN = "SIDE_EFFECT_UNCERTAIN"
    PORTAL_UNAVAILABLE = "PORTAL_UNAVAILABLE"
    INPUT_INVALID = "INPUT_INVALID"
    RECIPE_INVALID = "RECIPE_INVALID"
    CONFIG_MISSING = "CONFIG_MISSING"
    TIMEOUT = "TIMEOUT"
    SESSION_BUSY = "SESSION_BUSY"
    INTERNAL = "INTERNAL"


class HomingError(Exception):
    """Base error. `message` must never hold secrets; redacted anyway."""

    code: ErrorCode = ErrorCode.TRANSIENT

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        field: str | None = None,
    ) -> None:
        """Create an error with optional code override and related field."""
        super().__init__(message)
        self.message = message
        self.field = field
        if code is not None:
            self.code = code


class RetryableError(HomingError):
    """Transient: timeout, nav/network error, HTTP 5xx, detached element."""

    code = ErrorCode.TRANSIENT


class FinalError(HomingError):
    """Not worth retrying. Always carries a specific `ErrorCode`."""

    code = ErrorCode.POSTCONDITION_FAILED


EXIT_SUCCESS = 0
EXIT_FINAL = 1
EXIT_RETRIES_EXHAUSTED = 2
EXIT_INVALID = 3


def exit_code_for(code: ErrorCode | None) -> int:
    """Map an error code to the process exit status."""
    if code is None:
        return EXIT_SUCCESS
    if code in {
        ErrorCode.PORTAL_UNAVAILABLE,
        ErrorCode.TRANSIENT,
        ErrorCode.TIMEOUT,
        ErrorCode.SESSION_BUSY,
    }:
        return EXIT_RETRIES_EXHAUSTED
    if code in {
        ErrorCode.INPUT_INVALID,
        ErrorCode.RECIPE_INVALID,
        ErrorCode.CONFIG_MISSING,
    }:
        return EXIT_INVALID
    return EXIT_FINAL
