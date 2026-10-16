"""Set up (or tear down) the reviewer's private scratch repo.

Setup only: uses `gh`, not a browser.

    # repo + releases v0.1.0 / v0.1.1
    uv run python tools/bootstrap_github.py bootstrap
    # delete draft releases made by runs
    uv run python tools/bootstrap_github.py clean-drafts

Needs `gh auth login` (or GITHUB_TOKEN). Writes
`inputs/local/github-v0.1.*.json` with the real owner filled in (gitignored).
"""

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF

REPO_NAME = "homing-scratch"
RELEASES = {
    "v0.1.0": [
        "Statement v0.1.0",
        "Deductible: $500",
        "Out-of-pocket max: $3,000",
    ],
    "v0.1.1": [
        "Statement v0.1.1",
        "Deductible: $750",
        "Out-of-pocket max: $4,500",
    ],
}


def gh(*args: str, check: bool = True) -> str:
    """Run `gh` and return stdout."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["gh", *args],  # noqa: S607 - gh on PATH is expected
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        sys.exit(f"gh {' '.join(args[:3])} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def statement(lines: list[str], path: Path) -> None:
    """A small, deterministic PDF (different content per release)."""
    pdf = FPDF(format="A4")
    pdf.set_creation_date(datetime(2026, 1, 1, tzinfo=UTC))
    pdf.add_page()
    pdf.set_font("Helvetica", size=14)
    for line in lines:
        pdf.cell(0, 10, line, new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))


def bootstrap() -> None:
    """Create the private repo and two releases with distinct statement PDFs."""
    owner = gh("api", "user", "--jq", ".login")
    repo = f"{owner}/{REPO_NAME}"
    if gh("repo", "view", repo, "--json", "name", check=False):
        print(f"✓ {repo} exists")
    else:
        gh(
            "repo",
            "create",
            repo,
            "--private",
            "--add-readme",
            "--description",
            "homing scratch (safe to delete)",
        )
        print(f"✓ created private {repo}")
    existing = gh(
        "release",
        "list",
        "-R",
        repo,
        "--json",
        "tagName",
        "--jq",
        ".[].tagName",
        check=False,
    ).split()
    with tempfile.TemporaryDirectory() as tmp:
        for tag, lines in RELEASES.items():
            if tag in existing:
                print(f"✓ release {tag} exists")
                continue
            pdf = Path(tmp) / "statement.pdf"
            statement(lines, pdf)
            gh(
                "release",
                "create",
                tag,
                str(pdf),
                "-R",
                repo,
                "--title",
                tag,
                "--notes",
                f"Fixture {tag}",
            )
            print(f"✓ created release {tag} with statement.pdf")
    out = Path("inputs/local")
    out.mkdir(parents=True, exist_ok=True)
    for template in sorted(Path("inputs").glob("github-*.json")):
        data = json.loads(template.read_text())
        data["repo"] = repo
        (out / template.name).write_text(json.dumps(data, indent=2) + "\n")
        print(f"✓ wrote {out / template.name}")


def clean_drafts() -> None:
    """Delete every *draft* release in the scratch repo.

    Published ones are untouched.
    """
    owner = gh("api", "user", "--jq", ".login")
    repo = f"{owner}/{REPO_NAME}"
    drafts = gh(
        "api", f"repos/{repo}/releases", "--jq", ".[] | select(.draft) | .id"
    ).split()
    for release_id in drafts:
        gh("api", "-X", "DELETE", f"repos/{repo}/releases/{release_id}")
    print(f"✓ deleted {len(drafts)} draft release(s) in {repo}")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["bootstrap", "clean-drafts"])
    args = parser.parse_args()
    {"bootstrap": bootstrap, "clean-drafts": clean_drafts}[args.command]()


if __name__ == "__main__":
    main()
