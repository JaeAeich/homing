"""Shared fixtures: GreenMail, a fresh mock portal per test, a CLI runner."""

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from ruamel.yaml import YAML, CommentedMap

from homing.manifest import FileKind, Manifest, StageRecord
from homing.paths import RunPaths
from homing.session import generate_key
from mock_portal.app import Stats

ROOT = Path(__file__).parents[1]
IMAP_PORT, SMTP_PORT = 3143, 3025
GREENMAIL_IMAGE = "greenmail/standalone:2.1.5"
GREENMAIL_OPTS = (
    "-Dgreenmail.setup.test.smtp -Dgreenmail.setup.test.imap "
    "-Dgreenmail.hostname=0.0.0.0 "
    "-Dgreenmail.users=demo:secret@acme-payer.test "
    "-Dgreenmail.users.login=local_part"
)
SECRETS = {
    "ACME_USERNAME": "demo",
    "ACME_PASSWORD": "demo-password",
    "ACME_IMAP_USER": "demo",
    "ACME_IMAP_PASSWORD": "secret",
}


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def greenmail():
    """GreenMail (SMTP 3025 / IMAP 3143).

    Reuses a running one, else starts it in Docker.
    """
    if _port_open(IMAP_PORT):
        yield
        return
    if not shutil.which("docker"):
        pytest.skip("GreenMail not running and docker not available")
    name = "homing-greenmail-test"
    subprocess.run(
        ["docker", "rm", "-f", name], capture_output=True, check=False
    )
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"{SMTP_PORT}:3025",
            "-p",
            f"{IMAP_PORT}:3143",
            "-e",
            f"GREENMAIL_OPTS={GREENMAIL_OPTS}",
            GREENMAIL_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    deadline = time.monotonic() + 60
    while not _port_open(IMAP_PORT):
        if time.monotonic() > deadline:
            pytest.fail("GreenMail did not start")
        time.sleep(0.5)
    time.sleep(2)
    yield
    subprocess.run(
        ["docker", "rm", "-f", name], capture_output=True, check=False
    )


@dataclass
class Portal:
    base_url: str
    process: subprocess.Popen

    def faults(self, spec: str) -> None:
        httpx.post(
            f"{self.base_url}/__admin/faults", content=spec
        ).raise_for_status()

    def expire_sessions(self) -> None:
        httpx.post(
            f"{self.base_url}/__admin/expire_sessions"
        ).raise_for_status()

    def stats(self) -> Stats:
        response = httpx.get(f"{self.base_url}/__admin/stats")
        return Stats.model_validate_json(response.content)


@pytest.fixture
def portal(request):
    """A fresh Acme Payer instance per test (state is in memory)."""
    request.getfixturevalue("greenmail")  # it emails its codes
    port = _free_port()
    env = os.environ | {"ACME_MFA": "email", "ACME_FAULTS": ""}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "mock_portal.app:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while not _port_open(port):
        if time.monotonic() > deadline:
            proc.kill()
            pytest.fail("mock portal did not start")
        time.sleep(0.2)
    yield Portal(base, proc)
    proc.terminate()
    proc.wait(timeout=10)


@dataclass
class Result:
    code: int
    output: str
    home: Path

    @property
    def run_id(self) -> str:
        """Id of the run whose manifest was printed (resume/fork: new run)."""
        lines = self.output.splitlines()
        return next(
            line.split()[2]
            for line in lines
            if line.startswith(("✓ run ", "✗ run "))
        )

    @property
    def paths(self) -> RunPaths:
        return RunPaths(self.home / "runs" / self.run_id)

    @property
    def manifest(self) -> Manifest:
        return Manifest.model_validate_json(self.paths.manifest.read_text())

    def stage(self, key: str) -> StageRecord:
        return next(s for s in self.manifest.stages if s.key == key)

    @property
    def statement_sha(self) -> str:
        return next(
            f.sha256 for f in self.manifest.files if f.kind is FileKind.DOWNLOAD
        )


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def cli_env(home):
    home.mkdir(exist_ok=True)
    return (
        os.environ
        | SECRETS
        | {
            "HOMING_HOME": str(home),
            "SESSION_KEY": generate_key(),
            "RECIPES_DIR": str(ROOT / "recipes"),
            "NO_COLOR": "1",
            "COLUMNS": "200",
        }
    )


@pytest.fixture
def homing(cli_env, home, portal):
    def run(
        *args: str,
        recipe: str | Path = "acme",
        inputs: str = "inputs/acme-member-1.json",
        timeout: int = 150,
    ) -> Result:
        cmd = [sys.executable, "-m", "homing.cli"]
        if args and args[0] in {"resume", "fork", "runs", "doctor"}:
            cmd += list(args)
        else:
            cmd += [
                "run",
                "-r",
                str(recipe),
                "-i",
                str(ROOT / inputs),
                "--base-url",
                portal.base_url,
                *args,
            ]
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return Result(proc.returncode, proc.stdout + proc.stderr, home)

    return run


RecipeEdit = Callable[[CommentedMap], None]


def recipe_variant(tmp_path: Path, edit: RecipeEdit) -> Path:
    """A copy of acme.yaml with one structural change. Same engine."""
    yaml = YAML()
    recipe = yaml.load(ROOT / "recipes" / "acme.yaml")
    edit(recipe)
    path = tmp_path / "acme.yaml"
    yaml.dump(recipe, path)
    return path


def stage_of(recipe: CommentedMap, key: str) -> CommentedMap:
    """The recipe stage called `key`."""
    return next(s for s in recipe["stages"] if s["key"] == key)
