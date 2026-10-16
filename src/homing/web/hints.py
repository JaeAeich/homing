"""What to do next, per error code: the "How to fix" list in a stage panel."""

from dataclasses import dataclass

from homing.errors import ErrorCode


@dataclass(frozen=True)
class HintStep:
    """One step; `command` is shown as a copyable line under the text."""

    text: str
    command: str | None = None


UPDATE_AND_RESUME = HintStep(
    "Update the recipe, then Resume. Earlier stages and the login are reused."
)
RESUME = HintStep("Resume once it's fixed. Finished stages are not redone.")

HINTS: dict[ErrorCode, tuple[HintStep, ...]] = {
    ErrorCode.POSTCONDITION_FAILED: (
        HintStep("The stage ran, but its post checks did not hold."),
        HintStep("Compare the screenshot with the stage's `post` block."),
        UPDATE_AND_RESUME,
    ),
    ErrorCode.AUTH_BAD_CREDENTIALS: (
        HintStep("The portal rejected the username or password."),
        HintStep("Fix the values in .env, then check them:", "homing doctor"),
        RESUME,
    ),
    ErrorCode.AUTH_CODE_REJECTED: (
        HintStep("The second-factor code was rejected twice."),
        HintStep("Check the clock and the TOTP secret:", "homing doctor"),
        RESUME,
    ),
    ErrorCode.CAPTCHA_ENCOUNTERED: (
        HintStep("The portal showed a captcha. homing never solves them."),
        HintStep("Sign in once by hand in the browser, then Resume."),
    ),
    ErrorCode.WRITE_BLOCKED: (
        HintStep("The click would write and is not on the allow list."),
        HintStep("If the write is intended, add it to `write_guard.allow`."),
        UPDATE_AND_RESUME,
    ),
    ErrorCode.SIDE_EFFECT_UNCERTAIN: (
        HintStep("The failure came after a write click; it may have landed."),
        HintStep("Check the portal by hand before you Resume."),
    ),
    ErrorCode.PORTAL_UNAVAILABLE: (
        HintStep("The portal kept failing (timeouts or HTTP 5xx)."),
        RESUME,
    ),
    ErrorCode.TIMEOUT: (
        HintStep("The run hit its time limit; the browser was stopped."),
        RESUME,
    ),
    ErrorCode.SESSION_BUSY: (
        HintStep("Another run was using this portal's session."),
        RESUME,
    ),
}
FALLBACK = (HintStep("See the error and the evidence below."), RESUME)


def hints_for(
    code: ErrorCode, *, portal: str, field: str | None, page: str | None
) -> tuple[HintStep, ...]:
    """Steps for `code`; locator failures get the exact `resolve` command."""
    if code is ErrorCode.LOCATOR_NOT_FOUND and field and page:
        return (
            HintStep(
                "The page changed. Try the recipe on the saved page:",
                f"homing resolve --html {page} -r {portal} -f {field}",
            ),
            UPDATE_AND_RESUME,
        )
    return HINTS.get(code, FALLBACK)
