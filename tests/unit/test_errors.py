import pytest
from playwright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeout,
)

from homing.engine.classify import classify
from homing.errors import (
    EXIT_FINAL,
    EXIT_INVALID,
    EXIT_RETRIES_EXHAUSTED,
    ErrorCode,
    FinalError,
    RetryableError,
    exit_code_for,
)
from homing.redact import REDACTOR

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "exc",
    [
        PlaywrightTimeout("Timeout 15000ms exceeded."),
        PlaywrightError("net::ERR_CONNECTION_RESET at https://x"),
        PlaywrightError(
            "Execution context was destroyed, "
            "most likely because of a navigation"
        ),
        PlaywrightError("Element is not attached to the DOM (detached)"),
        TimeoutError(),
        ConnectionResetError(),
        RetryableError("HTTP 502"),
    ],
)
def test_transient_errors_are_retryable(exc):
    assert isinstance(classify(exc), RetryableError)


@pytest.mark.parametrize(
    "exc",
    [
        PlaywrightError("Unexpected token in selector"),
        ValueError("boom"),
        FinalError("nope", code=ErrorCode.LOCATOR_NOT_FOUND),
    ],
)
def test_everything_else_is_final(exc):
    assert isinstance(classify(exc), FinalError)


def test_classified_messages_are_redacted():
    REDACTOR.register("p4ssw0rd!", "PASSWORD")
    err = classify(ValueError("bad p4ssw0rd! here"))
    assert "p4ssw0rd!" not in err.message


def test_exit_codes():
    assert exit_code_for(None) == 0
    assert exit_code_for(ErrorCode.PORTAL_UNAVAILABLE) == EXIT_RETRIES_EXHAUSTED
    assert exit_code_for(ErrorCode.INPUT_INVALID) == EXIT_INVALID
    assert exit_code_for(ErrorCode.RECIPE_INVALID) == EXIT_INVALID
    assert exit_code_for(ErrorCode.AUTH_CODE_REJECTED) == EXIT_FINAL
    assert exit_code_for(ErrorCode.WRITE_BLOCKED) == EXIT_FINAL
