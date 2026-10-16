"""Fault injection: env var, request header, or the admin endpoint.

Spec: comma-separated `wrong_code_once`, `wrong_code_always`,
`captcha_on_login`, `passkey_first`, `http500_once:<path>`, `slow:<path>`,
`layout=v2`, `layout=iframe`, `statement=new_tab`.
"""

import os
from dataclasses import dataclass, field, replace
from enum import StrEnum

ENV_VAR = "ACME_FAULTS"
HEADER = "x-mock-faults"


class Flag(StrEnum):
    """On/off faults."""

    WRONG_CODE_ONCE = "wrong_code_once"
    WRONG_CODE_ALWAYS = "wrong_code_always"
    CAPTCHA_ON_LOGIN = "captcha_on_login"
    PASSKEY_FIRST = "passkey_first"  # 2FA opens on a passkey prompt


class Layout(StrEnum):
    """Page markup variant."""

    V1 = "v1"
    V2 = "v2"  # renamed classes/ids, label text moved
    IFRAME = "iframe"  # eligibility form inside an iframe


class StatementMode(StrEnum):
    """How the statement is offered."""

    DOWNLOAD = "download"
    NEW_TAB = "new_tab"


class Prefix(StrEnum):
    """Faults that carry a value."""

    HTTP500_ONCE = "http500_once:"
    SLOW = "slow:"
    LAYOUT = "layout="
    STATEMENT = "statement="


@dataclass
class Faults:
    """Active faults plus one-shot bookkeeping."""

    flags: set[Flag] = field(default_factory=set)
    http500_once: set[str] = field(default_factory=set)
    slow: set[str] = field(default_factory=set)
    layout: Layout | None = None
    statement: StatementMode | None = None
    fired: set[str] = field(default_factory=set)

    @classmethod
    def parse(cls, spec: str) -> "Faults":
        """Parse `a,b,http500_once:/x,layout=v2`."""
        faults = cls()
        for raw in filter(None, (p.strip() for p in spec.split(","))):
            if raw.startswith(Prefix.HTTP500_ONCE):
                faults.http500_once.add(raw.removeprefix(Prefix.HTTP500_ONCE))
            elif raw.startswith(Prefix.SLOW):
                faults.slow.add(raw.removeprefix(Prefix.SLOW))
            elif raw.startswith(Prefix.LAYOUT):
                faults.layout = Layout(raw.removeprefix(Prefix.LAYOUT))
            elif raw.startswith(Prefix.STATEMENT):
                value = raw.removeprefix(Prefix.STATEMENT)
                faults.statement = StatementMode(value)
            else:
                faults.flags.add(Flag(raw))
        return faults

    @property
    def page_layout(self) -> Layout:
        """The layout to render."""
        return self.layout or Layout.V1

    @property
    def statement_mode(self) -> StatementMode:
        """How to offer the statement."""
        return self.statement or StatementMode.DOWNLOAD

    def merged(self, header: str | None) -> "Faults":
        """Header faults add to the configured ones for one request."""
        if not header:
            return self
        extra = Faults.parse(header)
        return replace(
            self,
            flags=self.flags | extra.flags,
            http500_once=self.http500_once | extra.http500_once,
            slow=self.slow | extra.slow,
            layout=extra.layout or self.layout,
            statement=extra.statement or self.statement,
        )

    def fire_once(self, key: str) -> bool:
        """True the first time `key` is seen."""
        if key in self.fired:
            return False
        self.fired.add(key)
        return True


def from_env() -> Faults:
    """Faults from `ACME_FAULTS`."""
    return Faults.parse(os.environ.get(ENV_VAR, ""))
