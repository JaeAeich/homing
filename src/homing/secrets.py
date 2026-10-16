"""Secret providers.

Every value read is registered with the redactor immediately.
"""

import os
from typing import Protocol

from dotenv import dotenv_values
from pydantic import SecretStr

from homing.errors import ErrorCode, FinalError
from homing.redact import REDACTOR


class SecretProvider(Protocol):
    """Source of named secrets (env today; Vault/AWS Secrets Manager later)."""

    def get(self, key: str) -> SecretStr:
        """Return secret named `key` or raise `FinalError(CONFIG_MISSING)`."""
        ...


class EnvSecrets:
    """Reads secrets from the process env, falling back to a `.env` file."""

    def __init__(self, env_file: str | None = ".env") -> None:
        """Load the optional `.env` file once."""
        self._file: dict[str, str | None] = (
            dotenv_values(env_file) if env_file else {}
        )

    def get(self, key: str) -> SecretStr:
        """Return the secret, registering it with the redactor under its key."""
        value = os.environ.get(key) or self._file.get(key)
        if not value:
            msg = f"secret '{key}' is not set (env or .env)"
            raise FinalError(msg, code=ErrorCode.CONFIG_MISSING)
        REDACTOR.register(value, label=key)
        return SecretStr(value)
