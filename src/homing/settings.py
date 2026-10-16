"""Process-wide settings, from the environment and an optional `.env`."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Non-portal settings. Portal credentials come via a `SecretProvider`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    homing_home: Path = Field(
        default=Path(), description="Root for state/ and runs/."
    )
    homing_db_url: str | None = Field(
        default=None, description="DBOS system DB URL."
    )
    session_key: SecretStr | None = Field(
        default=None, description="Fernet key for sessions."
    )
    headed: bool = False
    recipes_dir: Path = Path("recipes")

    action_timeout_ms: int = 15_000
    stage_timeout_s: float = 90.0
    run_timeout_s: float = 600.0

    @property
    def state_dir(self) -> Path:
        """Directory holding encrypted session files."""
        return self.homing_home / "state"

    @property
    def runs_dir(self) -> Path:
        """Directory holding one sub-directory per run."""
        return self.homing_home / "runs"

    @property
    def database_url(self) -> str:
        """DBOS system database URL; SQLite under `state/` unless overridden."""
        if self.homing_db_url:
            return self.homing_db_url
        return f"sqlite:///{(self.state_dir / 'dbos.sqlite').resolve()}"


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
