"""CAPTCHA detection only. We never try to solve one."""

from playwright.async_api import Page

from homing.errors import ErrorCode, FinalError

CAPTCHA_FRAME_HOSTS = (
    "hcaptcha.com",
    "recaptcha.net",
    "google.com/recaptcha",
    "arkoselabs.com",
    "funcaptcha.com",
    "octocaptcha.com",
    "challenges.cloudflare.com",
)
CAPTCHA_SELECTORS = (
    "iframe[src*='captcha']",
    ".g-recaptcha",
    ".h-captcha",
    "#arkose-iframe",
    "[data-captcha]",
)


async def raise_if_captcha(
    page: Page, extra_selectors: list[str] | None = None
) -> None:
    """Raise `CAPTCHA_ENCOUNTERED` if a known challenge frame/widget shows."""
    for frame in page.frames:
        if any(host in frame.url for host in CAPTCHA_FRAME_HOSTS):
            msg = "captcha challenge frame detected"
            raise FinalError(msg, code=ErrorCode.CAPTCHA_ENCOUNTERED)
    for selector in (*CAPTCHA_SELECTORS, *(extra_selectors or [])):
        if await page.locator(selector).filter(visible=True).count():
            msg = f"captcha widget detected ({selector})"
            raise FinalError(msg, code=ErrorCode.CAPTCHA_ENCOUNTERED)
