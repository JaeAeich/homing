"""The reviewers' evaluation table, end to end, against real GitHub.

    # all six checks (2 fresh logins)
    uv run python tools/verify_live.py
    # just the broken-step evidence bundle
    uv run python tools/verify_live.py break

Needs `.env` (GITHUB_USERNAME, GITHUB_PASSWORD, GITHUB_TOTP_SECRET,
SESSION_KEY) and `make bootstrap` (writes inputs/local/*.json).
"""

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from ruamel.yaml import YAML

from homing.manifest import FileKind, Manifest
from homing.paths import RunPaths
from homing.session import SessionMode

ROOT = Path(__file__).parents[1]
INPUTS = ROOT / "inputs" / "local"
STATE = ROOT / "state" / "github__default.json.enc"
SECRET_KEYS = (
    "GITHUB_USERNAME",
    "GITHUB_PASSWORD",
    "GITHUB_TOTP_SECRET",
    "SESSION_KEY",
)


@dataclass
class Run:
    """Exit code + manifest of one CLI run."""

    code: int
    manifest: Manifest

    @property
    def mode(self) -> SessionMode | None:
        """Session mode recorded in the manifest."""
        return self.manifest.session.mode

    @property
    def sha(self) -> str:
        """sha256 of the downloaded statement."""
        downloads = (
            f.sha256 for f in self.manifest.files if f.kind is FileKind.DOWNLOAD
        )
        return next(downloads, "-")


def homing_run(inputs: Path, recipe: str | Path = "github") -> Run:
    """Run the CLI and load the manifest it wrote."""
    cmd = [
        sys.executable,
        "-m",
        "homing.cli",
        "run",
        "-r",
        str(recipe),
        "-i",
        str(inputs),
    ]
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        cmd, cwd=ROOT, capture_output=True, text=True, check=False
    )
    line = next(x for x in proc.stdout.splitlines() if x.startswith("→ run "))
    run_id = line.split()[2]
    manifest_path = RunPaths(ROOT / "runs" / run_id).manifest
    manifest = Manifest.model_validate_json(manifest_path.read_text())
    return Run(proc.returncode, manifest)


def report(label: str, detail: str, *, ok: bool) -> bool:
    """Print one row of the table."""
    print(f"{'✓' if ok else '✗'} {label:<18} {detail}")
    return ok


def leak_scan(paths: list[Path]) -> list[str]:
    """Grep outputs for every secret value in `.env`."""
    env = {
        k: v
        for k, v in (dotenv_values(ROOT / ".env") | os.environ).items()
        if k in SECRET_KEYS and v
    }
    hits = []
    for path in paths:
        for file in (
            p for p in path.rglob("*") if p.is_file() and p.suffix != ".pdf"
        ):
            blob = file.read_bytes()
            hits += [
                f"{k} in {file}" for k, v in env.items() if v.encode() in blob
            ]
    return hits


def broken_recipe() -> Path:
    """github.yaml with the release-title locator pointing at nothing."""
    yaml = YAML()
    recipe = yaml.load(ROOT / "recipes" / "github.yaml")
    fill_form = next(s for s in recipe["stages"] if s["key"] == "fill_form")
    title = next(
        step["fill"]
        for step in fill_form["steps"]
        if step.get("fill", {}).get("field") == "title"
    )
    title["name"] = "Release headline that does not exist"
    del title["label"]
    title["css"] = ["#nope"]
    path = ROOT / "runs" / "broken-github.yaml"
    path.parent.mkdir(exist_ok=True)
    yaml.dump(recipe, path)
    return path


def check_break(v0: Path) -> bool:
    """Broken step → final failure, evidence bundle written, leak scan clean."""
    run = homing_run(v0, broken_recipe())
    paths = RunPaths(ROOT / "runs" / run.manifest.run_id)
    evidence = paths.evidence
    hits = leak_scan([paths.root])
    code = run.manifest.error.code if run.manifest.error else None
    written = "written" if evidence.is_dir() else "MISSING"
    scan = hits or "clean"
    ok = run.code == 1 and evidence.is_dir() and not hits
    return report(
        "broken step", f"{code}, evidence {written}, leak scan {scan}", ok=ok
    )


def main() -> int:
    """Run the table; exit non-zero if any row fails."""
    v0, v1 = INPUTS / "github-v0.1.0.json", INPUTS / "github-v0.1.1.json"
    if not v0.exists():
        sys.exit("run `make bootstrap` first (writes inputs/local/)")
    if sys.argv[1:] == ["break"]:
        return 0 if check_break(v0) else 1

    results = []
    STATE.unlink(missing_ok=True)
    fresh = homing_run(v0)
    ok = fresh.code == 0 and fresh.mode is SessionMode.FRESH_LOGIN
    results.append(
        report(
            "fresh run",
            f"{fresh.mode!s:<19} statement sha {fresh.sha[:12]}…",
            ok=ok,
        )
    )

    again = homing_run(v0)
    faster = again.manifest.duration_ms < fresh.manifest.duration_ms
    same = again.sha == fresh.sha
    ok = (
        again.code == 0 and again.mode is SessionMode.REUSED and same and faster
    )
    detail = (
        f"reused, {'faster' if faster else 'NOT faster'}  "
        f"{'same sha' if same else 'DIFFERENT sha'}"
    )
    results.append(report("second run", detail, ok=ok))

    STATE.write_bytes(b"corrupted on purpose")
    corrupt = homing_run(v0)
    ok = corrupt.code == 0 and corrupt.mode is SessionMode.RELOGIN_AFTER_DEAD
    results.append(
        report(
            "corrupted state",
            f"{corrupt.mode!s:<19} exit {corrupt.code}",
            ok=ok,
        )
    )

    changed = homing_run(v1)
    differs = changed.sha != fresh.sha
    ok = changed.code == 0 and differs
    results.append(
        report(
            "changed inputs",
            f"v0.1.1  {'different' if differs else 'SAME'} sha",
            ok=ok,
        )
    )

    offline = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/offline", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (
        "all variants resolved offline"
        if offline.returncode == 0
        else offline.stdout[-300:]
    )
    results.append(report("mutated HTML", detail, ok=offline.returncode == 0))

    results.append(check_break(v0))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
