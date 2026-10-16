import base64
from datetime import UTC, datetime
from unittest.mock import patch

import pyotp
import pytest
from pydantic import SecretStr

from homing.auth.codes import Challenge
from homing.auth.codes.totp import BadFirstTotpProvider, TotpProvider
from homing.redact import REDACTOR

pytestmark = pytest.mark.unit

# RFC 6238 Appendix B, SHA-1, 8 digits, secret "12345678901234567890".
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


@pytest.mark.parametrize(("t", "expected"), RFC_VECTORS)
def test_rfc6238_vectors(t, expected):
    assert pyotp.TOTP(RFC_SECRET, digits=8).at(t) == expected


class FakeSecrets:
    def __init__(self, **values):
        self.values = values

    def get(self, key):
        return SecretStr(self.values[key])


def _challenge(attempt=1):
    return Challenge("acme", "default", "totp", attempt, datetime.now(UTC))


async def test_provider_returns_current_code_and_registers_it():
    secret = pyotp.random_base32()
    provider = TotpProvider(FakeSecrets(S=secret), secret_key="S")
    code = await provider.get_code(_challenge())
    assert pyotp.TOTP(secret).verify(code, valid_window=1)
    assert REDACTOR.text(f"code={code}") == "code=[REDACTED_CODE]"


class FakeClock:
    """`time.time` that only moves when `asyncio.sleep` is awaited."""

    def __init__(self, now: float):
        self.now = now
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _patched(clock):
    return (
        patch("homing.auth.codes.totp.time.time", clock.time),
        patch("homing.auth.codes.totp.asyncio.sleep", clock.sleep),
    )


async def test_retry_waits_for_next_window():
    clock = FakeClock(100.0)  # step 3, 10 s into the window
    provider = TotpProvider(
        FakeSecrets(S=pyotp.random_base32()), secret_key="S"
    )
    with _patched(clock)[0], _patched(clock)[1]:
        await provider.get_code(_challenge(attempt=2))
    assert clock.slept == [20.5]


async def test_used_step_is_never_reused_across_processes(tmp_path):
    secret = pyotp.random_base32()
    clock = FakeClock(100.0)
    with _patched(clock)[0], _patched(clock)[1]:
        first = await TotpProvider(
            FakeSecrets(S=secret), "S", state_dir=tmp_path
        ).get_code(_challenge())
        # A brand-new provider (think: next process) in the same window must
        # wait.
        second = await TotpProvider(
            FakeSecrets(S=secret), "S", state_dir=tmp_path
        ).get_code(_challenge())
    assert first != second
    assert clock.slept == [20.5]
    assert (tmp_path / "acme__default.totp-step").read_text() == "4"


async def test_bad_first_provider_is_wrong_then_right():
    secret = pyotp.random_base32()
    provider = BadFirstTotpProvider(FakeSecrets(S=secret), secret_key="S")
    clock = FakeClock(1_000_000.0)
    with _patched(clock)[0], _patched(clock)[1]:
        first = await provider.get_code(_challenge(1))
        second = await provider.get_code(_challenge(2))
    at = datetime.fromtimestamp(clock.now, UTC)
    assert not pyotp.TOTP(secret).verify(first, for_time=at, valid_window=1)
    assert pyotp.TOTP(secret).verify(second, for_time=at)
