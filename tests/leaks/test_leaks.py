"""Plant fake secrets, run passing and failing flows, grep everything we wrote.

Scanned: runs/ (manifests, evidence, files), CLI stdout/stderr, and every cell
of every table in the DBOS SQLite database. Encrypted session files are
skipped (ciphertext by design).
"""

import os
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from imap_tools import MailBoxUnencrypted

from homing.session import generate_key
from support import ROOT, SECRETS, Portal

pytestmark = pytest.mark.leaks

PLANTED = {
    "ACME_USERNAME": "leakyuser7f3a",
    "ACME_PASSWORD": "Leaky-Pass-9c1e!",
}
SENSITIVE_INPUTS = ["M-1001"]


@pytest.fixture
def planted_portal(monkeypatch):
    for key, value in PLANTED.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def leak_home(tmp_path):
    return tmp_path / "home"


def _codes_issued(since: datetime) -> set[str]:
    """Codes emailed during this test.

    The shared mailbox also holds other tests' codes.
    """
    with MailBoxUnencrypted("127.0.0.1", port=3143).login(
        "demo", "secret"
    ) as box:
        return {
            m.group(1)
            for msg in box.fetch(mark_seen=False, bulk=True)
            if (msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=UTC))
            >= since
            and (m := re.search(r"code is (\d{6})", msg.text or ""))
        }


TIMESTAMPS = re.compile(
    rb"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?"
)


def _scan_bytes(
    blob: bytes, needles: dict[str, bytes], where: str
) -> list[str]:
    """Find needles.

    Numeric needles (codes) must stand alone, not inside a longer number, and
    ISO timestamps are removed first: their 6-digit microseconds would collide
    by chance.
    """
    blob = TIMESTAMPS.sub(b"<ts>", blob)
    hits = []
    for label, needle in needles.items():
        pattern = (
            rb"(?<!\d)" + re.escape(needle) + rb"(?!\d)"
            if needle.isdigit()
            else re.escape(needle)
        )
        if re.search(pattern, blob):
            hits.append(f"{label} in {where}")
    return hits


def _scan_sqlite(path: Path, needles: dict[str, bytes]) -> list[str]:
    hits: list[str] = []
    with sqlite3.connect(path) as db:
        tables = [
            r[0]
            for r in db.execute(
                "select name from sqlite_master where type='table'"
            )
        ]
        for table in tables:
            # Table names from sqlite itself.
            for row in db.execute(f'select * from "{table}"'):  # noqa: S608
                for cell in row:
                    if cell is None:
                        continue
                    blob = (
                        cell if isinstance(cell, bytes) else str(cell).encode()
                    )
                    hits += _scan_bytes(blob, needles, f"dbos:{table}")
    hits += _scan_bytes(path.read_bytes(), needles, "dbos.sqlite (raw pages)")
    return hits


@pytest.mark.usefixtures("planted_portal")
def test_no_secret_reaches_any_output(portal: Portal, leak_home: Path):
    leak_home.mkdir()
    session_key = generate_key()
    env = (
        os.environ
        | SECRETS
        | PLANTED
        | {
            "ACME_IMAP_USER": "demo",
            "ACME_IMAP_PASSWORD": "secret",
            "HOMING_HOME": str(leak_home),
            "SESSION_KEY": session_key,
            "NO_COLOR": "1",
        }
    )
    outputs: list[str] = []
    started = datetime.now(UTC).replace(microsecond=0)

    def run(*extra: str) -> int:
        cmd = [
            sys.executable,
            "-m",
            "homing.cli",
            "--json-logs",
            "run",
            "-r",
            "acme",
            "-i",
            str(ROOT / "inputs/acme-member-1.json"),
            "--base-url",
            portal.base_url,
            *extra,
        ]
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=150,
            check=False,
        )
        outputs.append(proc.stdout + proc.stderr)
        return proc.returncode

    assert run() == 0, outputs[-1]  # fresh login, success
    portal.faults("wrong_code_once")
    portal.expire_sessions()
    assert run() == 0, outputs[-1]  # relogin with a rejected code
    portal.faults("wrong_code_always")
    portal.expire_sessions()
    assert run() == 1, outputs[-1]  # final failure → evidence bundle
    portal.faults("layout=v2,captcha_on_login")
    portal.expire_sessions()
    assert run() == 1, outputs[-1]  # captcha → evidence bundle

    needles = {k: v.encode() for k, v in PLANTED.items()}
    needles |= {
        "SESSION_KEY": session_key.encode(),
        "IMAP_PASSWORD": b'secret"',
    }
    needles |= {f"input:{v}": v.encode() for v in SENSITIVE_INPUTS}
    codes = _codes_issued(started)
    assert codes, "the test must actually have issued codes"
    needles |= {f"code:{c}": c.encode() for c in codes}

    hits: list[str] = []
    for i, text in enumerate(outputs):
        hits += _scan_bytes(text.encode(), needles, f"cli output #{i}")
    for path in (leak_home / "runs").rglob("*"):
        if path.is_file() and path.suffix != ".pdf":
            hits += _scan_bytes(
                path.read_bytes(), needles, str(path.relative_to(leak_home))
            )
    hits += _scan_sqlite(leak_home / "state" / "dbos.sqlite", needles)

    evidence = list((leak_home / "runs").glob("*/evidence/error.json"))
    assert len(evidence) == 2, "both failures wrote evidence"
    assert not hits, "\n".join(hits)
