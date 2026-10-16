"""Where a run keeps its things. One place for every file and folder name."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from homing.settings import get_settings


class EvidenceFile(StrEnum):
    """Files in a failure evidence bundle."""

    SCREENSHOT = "screenshot.png"
    PAGE = "page.html"
    URL = "url.txt"
    CONSOLE = "console.jsonl"
    STAGE = "stage.txt"
    RESOLVER_ATTEMPTS = "resolver_attempts.json"
    ERROR = "error.json"


class RunFolder(StrEnum):
    """Sub-folders of a run that the dashboard may serve files from."""

    FILES = "files"
    EVIDENCE = "evidence"


@dataclass(frozen=True)
class RunPaths:
    """`runs/<run_id>/` and everything inside it."""

    root: Path

    @classmethod
    def of(cls, run_id: str) -> "RunPaths":
        """Paths for `run_id` under the configured runs directory."""
        return cls(get_settings().runs_dir / run_id)

    @property
    def manifest(self) -> Path:
        """`manifest.json`, written when the run finishes."""
        return self.root / "manifest.json"

    @property
    def files(self) -> Path:
        """Captured documents."""
        return self.root / RunFolder.FILES

    @property
    def evidence(self) -> Path:
        """The failure evidence bundle."""
        return self.root / RunFolder.EVIDENCE

    def folder(self, folder: RunFolder) -> Path:
        """One of the servable sub-folders."""
        return self.root / folder

    def evidence_file(self, name: EvidenceFile) -> Path:
        """One file of the evidence bundle."""
        return self.evidence / name

    def trace(self, run_id: str) -> Path:
        """The local-only Playwright trace."""
        return self.root / f"{run_id}.trace.zip"
