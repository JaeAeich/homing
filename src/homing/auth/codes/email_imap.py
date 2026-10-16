"""Codes delivered by email, fetched over IMAP with `imap-tools`."""

import asyncio
import re
import time
from datetime import UTC, datetime

from imap_tools import AND, BaseMailBox, MailBox, MailBoxUnencrypted

from homing.auth.codes.base import Challenge
from homing.errors import ErrorCode, FinalError, RetryableError
from homing.recipe.models import EmailFactor
from homing.redact import REDACTOR
from homing.secrets import SecretProvider


class EmailImapProvider:
    """Polls a mailbox for a fresh message and extracts the code.

    Only messages newer than the challenge count.
    """

    def __init__(self, secrets: SecretProvider, config: EmailFactor) -> None:
        """`config` is the recipe's `second_factor` block."""
        self._secrets = secrets
        self._config = config
        self._regex = re.compile(config.code_regex)
        self._used: set[str] = set()

    def _mailbox(self) -> BaseMailBox:
        cls = MailBox if self._config.ssl else MailBoxUnencrypted
        return cls(self._config.host, port=self._config.port)

    def _fetch_once(self, not_before: datetime) -> str | None:
        config = self._config
        user = self._secrets.get(config.user_key).get_secret_value()
        password = self._secrets.get(config.password_key).get_secret_value()
        since = not_before.date()
        criteria = (
            AND(date_gte=since, from_=config.from_)
            if config.from_
            else AND(date_gte=since)
        )
        login = self._mailbox().login(
            user, password, initial_folder=config.folder
        )
        with login as box:
            for msg in box.fetch(
                criteria, reverse=True, mark_seen=False, bulk=True
            ):
                sent = (
                    msg.date
                    if msg.date.tzinfo
                    else msg.date.replace(tzinfo=UTC)
                )
                if sent < not_before.replace(microsecond=0):
                    continue
                wanted = config.subject
                if wanted and wanted.lower() not in msg.subject.lower():
                    continue
                match = self._regex.search(msg.text or msg.html or "")
                code = (
                    (match.group(1) if match.groups() else match.group(0))
                    if match
                    else None
                )
                if code and code not in self._used:
                    return code
        return None

    async def get_code(self, challenge: Challenge) -> str:
        """Poll until a matching message arrives or `timeout_s` elapses."""
        deadline = time.monotonic() + self._config.timeout_s
        while time.monotonic() < deadline:
            try:
                code = await asyncio.to_thread(
                    self._fetch_once, challenge.not_before
                )
            except OSError as exc:
                msg = f"IMAP unavailable: {type(exc).__name__}"
                raise RetryableError(msg) from None
            if code:
                REDACTOR.register(code, label="CODE")
                self._used.add(code)
                return code
            await asyncio.sleep(self._config.poll_s)
        msg = f"no code email arrived within {self._config.timeout_s:.0f}s"
        raise FinalError(msg, code=ErrorCode.AUTH_CODE_REJECTED)
