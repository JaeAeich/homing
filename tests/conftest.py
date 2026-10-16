import pytest

from homing.redact import REDACTOR

# Shared fixtures.
from support import cli_env, greenmail, home, homing, portal  # noqa: F401


@pytest.fixture(autouse=True)
def _clean_redactor():
    REDACTOR.clear()
    yield
    REDACTOR.clear()
