"""The `CodeProvider` protocol and a factory picking an implementation."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from homing.errors import ErrorCode, FinalError
from homing.recipe.models import (
    EmailFactor,
    HumanFactor,
    NoSecondFactor,
    SecondFactor,
    TotpFactor,
)
from homing.secrets import SecretProvider

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "mock-portal"})


@dataclass(frozen=True)
class Challenge:
    """What the portal is asking for. Never contains the code itself."""

    portal: str
    credential_id: str
    kind: str
    attempt: int
    not_before: datetime


class CodeProvider(Protocol):
    """Obtains a second-factor code without a human.

    Implementations must register every code with `REDACTOR` the moment it
    exists, and must never put the code in log lines or exception messages.
    """

    async def get_code(self, challenge: Challenge) -> str:
        """Return the code for `challenge`."""
        ...


def build_code_provider(
    second_factor: SecondFactor,
    secrets: SecretProvider,
    base_url: str,
    *,
    state_dir: Path | None = None,
) -> CodeProvider | None:
    """Instantiate the recipe's provider (`None` for `type: none`)."""
    # Avoid cycle.
    from homing.auth.codes.email_imap import EmailImapProvider  # noqa: PLC0415
    from homing.auth.codes.human import HumanProvider  # noqa: PLC0415
    from homing.auth.codes.totp import (  # noqa: PLC0415
        BadFirstTotpProvider,
        TotpProvider,
    )

    match second_factor:
        case NoSecondFactor():
            return None
        case HumanFactor():
            return HumanProvider(second_factor.timeout_s)
        case EmailFactor():
            return EmailImapProvider(secrets, second_factor)
        case TotpFactor(type="totp_bad_first"):
            if urlsplit(base_url).hostname not in LOCAL_HOSTS:
                msg = (
                    "totp_bad_first is a mock-portal debug provider; "
                    "refusing a real site"
                )
                raise FinalError(msg, code=ErrorCode.RECIPE_INVALID)
            provider = BadFirstTotpProvider
        case TotpFactor():
            provider = TotpProvider
    return provider(
        secrets,
        second_factor.secret_key,
        second_factor.interval,
        state_dir=state_dir,
    )
