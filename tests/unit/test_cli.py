import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from homing.cli import app

pytestmark = pytest.mark.unit
runner = CliRunner()
FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_validate_ok_and_line_numbered_failure(tmp_path):
    ok = runner.invoke(
        app, ["validate", "recipes/acme.yaml", "recipes/github.yaml"]
    )
    assert ok.exit_code == 0, ok.output

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        Path("recipes/acme.yaml")
        .read_text()
        .replace("portal: acme", "portal: Acme!")
    )
    result = runner.invoke(app, ["validate", str(bad)])
    assert result.exit_code == 3
    assert "bad.yaml:" in result.output


def test_resolve_prints_winner_and_attempts():
    result = runner.invoke(
        app,
        [
            "resolve",
            "--html",
            str(FIXTURES / "acme/login.v2.html"),
            "-r",
            "acme",
            "-f",
            "submit",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "structure" in result.output
    assert "drift" in result.output
    assert "no_match" in result.output


def test_resolve_needs_inputs_for_placeholders():
    html = str(FIXTURES / "acme/search_member.html")
    result = runner.invoke(
        app, ["resolve", "--html", html, "-r", "acme", "-f", "member_link"]
    )
    assert result.exit_code != 0
    assert "--inputs" in result.output
    with_inputs = runner.invoke(
        app,
        [
            "resolve",
            "--html",
            html,
            "-r",
            "acme",
            "-f",
            "member_link",
            "-i",
            "inputs/acme-member-1.json",
        ],
    )
    assert with_inputs.exit_code == 0, with_inputs.output


def test_run_rejects_placeholder_inputs_before_launching_anything():
    result = runner.invoke(
        app, ["run", "-r", "github", "-i", "inputs/github-v0.1.0.json"]
    )
    assert result.exit_code == 3
    assert "placeholder" in result.output


def test_schema_export(tmp_path):
    out = tmp_path / "schema.json"
    assert runner.invoke(app, ["schema", "--output", str(out)]).exit_code == 0
    assert json.loads(out.read_text())["title"] == "Recipe"
