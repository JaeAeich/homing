"""Encrypted, atomically-written Playwright `storage_state`.

One file per `(portal, credential_id)`.
"""

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ValidationError

from homing.errors import ErrorCode, FinalError


class SessionMode(StrEnum):
    """How the run got its session; recorded in the manifest."""

    REUSED = "reused"
    FRESH_LOGIN = "fresh_login"
    RELOGIN_AFTER_DEAD = "relogin_after_dead"


class SessionLoad(StrEnum):
    """Outcome of reading the stored session."""

    LOADED = "loaded"
    MISSING = "missing"
    UNDECRYPTABLE = "undecryptable"
    BAD_JSON = "bad_json"
    BAD_STRUCTURE = "bad_structure"


class _Cookie(BaseModel):
    name: str
    value: str
    domain: str
    path: str


class _Origin(BaseModel):
    origin: str
    localStorage: list[dict[str, str]]  # noqa: N815 - Playwright's key name


class StorageState(BaseModel):
    """Minimal structural check of Playwright's `storage_state` format."""

    cookies: list[_Cookie]
    origins: list[_Origin]


@dataclass(frozen=True)
class LoadResult:
    """Either a usable state or the reason there is none."""

    state: dict[str, Any] | None
    reason: SessionLoad


class SessionStore(Protocol):
    """Persistence for browser sessions, keyed by `(portal, credential_id)`."""

    def load(self, portal: str, credential_id: str) -> LoadResult:
        """Return the stored state, or `state=None` with a reason.

        Never raises.
        """
        ...

    def save(
        self, portal: str, credential_id: str, state: dict[str, Any]
    ) -> None:
        """Persist `state` atomically."""
        ...


class EncryptedFileSessionStore:
    """Fernet-encrypted JSON session files.

    Stored under `state/<portal>__<credential_id>.json.enc`.
    """

    def __init__(self, directory: Path, key: str) -> None:
        """Create a store; `key` is a urlsafe-base64 32-byte Fernet key."""
        self._dir = directory
        try:
            self._fernet = Fernet(key.encode())
        except ValueError:
            msg = (
                "SESSION_KEY is not a valid Fernet key; "
                "generate one with `homing keygen`"
            )
            raise FinalError(msg, code=ErrorCode.CONFIG_MISSING) from None

    def path(self, portal: str, credential_id: str) -> Path:
        """File path for one session."""
        return self._dir / f"{portal}__{credential_id}.json.enc"

    def load(self, portal: str, credential_id: str) -> LoadResult:
        """Decrypt, parse, validate shape; any failure means "no session"."""
        path = self.path(portal, credential_id)
        if not path.exists():
            return LoadResult(None, SessionLoad.MISSING)
        try:
            plain = self._fernet.decrypt(path.read_bytes())
        except (InvalidToken, OSError, ValueError):
            return LoadResult(None, SessionLoad.UNDECRYPTABLE)
        try:
            data = json.loads(plain)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return LoadResult(None, SessionLoad.BAD_JSON)
        try:
            StorageState.model_validate(data)
        except ValidationError:
            return LoadResult(None, SessionLoad.BAD_STRUCTURE)
        return LoadResult(data, SessionLoad.LOADED)

    def save(
        self, portal: str, credential_id: str, state: dict[str, Any]
    ) -> None:
        """Encrypt and write via temp file + `os.replace`.

        Readers never see a partial file.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self.path(portal, credential_id)
        token = self._fernet.encrypt(json.dumps(state).encode())
        fd, tmp = tempfile.mkstemp(
            dir=self._dir, prefix=target.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp).chmod(0o600)
            Path(tmp).replace(target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                Path(tmp).unlink()
            raise


def generate_key() -> str:
    """A fresh Fernet key for `SESSION_KEY`."""
    return Fernet.generate_key().decode()
