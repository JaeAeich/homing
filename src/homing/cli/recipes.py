"""Recipes panel: `validate`, `resolve`, `snapshot`, `schema`."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from homing.cli.app import (
    PANEL_RECIPES,
    BaseUrlOpt,
    InputsOpt,
    OptionalInputsOpt,
    RecipeOpt,
    app,
    failure,
    invalid,
    out,
    recipe_path,
    start,
    success,
)
from homing.errors import EXIT_FINAL, EXIT_INVALID, HomingError
from homing.recipe.loader import load_inputs, load_recipe, validate_recipe_file
from homing.recipe.models import LocatorSpec, Recipe
from homing.settings import get_settings


def _find_field(recipe: Recipe, name: str) -> LocatorSpec:
    """Find a locator by field name: login fields first, then stage steps."""
    fields = recipe.auth.fields
    found: dict[str, LocatorSpec] = {
        key: getattr(fields, key)
        for key in type(fields).model_fields
        if getattr(fields, key)
    }
    for stage in recipe.stages:
        for step in stage.steps:
            if step.params.field:
                found.setdefault(step.params.field, step.params)
    if name not in found:
        msg = f"no field '{name}'; known: {', '.join(sorted(found))}"
        raise invalid(msg)
    return found[name]


@app.command(
    rich_help_panel=PANEL_RECIPES,
    epilog="Example: homing validate recipes/*.yaml",
)
def validate(
    recipes: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="RECIPE...", help="Names or paths (default: every recipe)."
        ),
    ] = None,
) -> None:
    """Check recipes against the schema; errors point at file:line."""
    refs = recipes or [
        str(p) for p in sorted(get_settings().recipes_dir.glob("*.yaml"))
    ]
    failed = False
    for ref in refs:
        try:
            path = recipe_path(ref)
            recipe = validate_recipe_file(path)
        except HomingError as exc:
            failed = True
            failure(exc.code, exc.message)
            continue
        success(f"{path}  portal={recipe.portal}  stages={len(recipe.stages)}")
    raise typer.Exit(EXIT_INVALID if failed else 0)


@app.command(
    rich_help_panel=PANEL_RECIPES,
    epilog=(
        "Example: homing resolve "
        "--html tests/fixtures/acme/login.v2.html -r acme -f submit"
    ),
)
def resolve(
    html: Annotated[
        Path,
        typer.Option(
            "--html",
            metavar="FILE",
            help="Saved HTML page.",
            exists=True,
            dir_okay=False,
        ),
    ],
    recipe: RecipeOpt,
    field: Annotated[
        str,
        typer.Option(
            "--field", "-f", metavar="FIELD", help="Field name in the recipe."
        ),
    ],
    inputs: OptionalInputsOpt = None,
) -> None:
    """Resolve one field against a saved page, offline.

    Shows the winner and every attempt.
    """
    from homing.resolver.offline import resolve_offline  # noqa: PLC0415

    path = recipe_path(recipe)
    parsed = (
        load_recipe(path, load_inputs(inputs)[0]).recipe
        if inputs
        else validate_recipe_file(path)
    )
    spec = _find_field(parsed, field)
    if "{{" in spec.model_dump_json():
        msg = (
            f"'{field}' uses {{{{placeholders}}}}; "
            "pass --inputs so they can be filled in"
        )
        raise invalid(msg)

    start(f"resolve {field} in {html.name}")
    winner, attempts = asyncio.run(
        resolve_offline(html.resolve().as_uri(), spec)
    )
    table = Table("strategy", "frame", "matches", "result", "query", box=None)
    for a in attempts:
        verdict = a.rejected_reason or "[green]WIN[/green]"
        table.add_row(
            a.strategy, a.frame, str(a.matches), verdict, escape(a.query)
        )
    out.print(table)
    if winner is None:
        failure(
            "LOCATOR_NOT_FOUND",
            f"'{field}' did not resolve to exactly one element",
        )
        raise typer.Exit(EXIT_FINAL)
    drift = (
        " [yellow](drift: not the first-listed strategy)[/yellow]"
        if winner.drift
        else ""
    )
    success(
        f"{field} → {winner.strategy} in {winner.frame}: "
        f"{escape(winner.query)}{drift}"
    )


@app.command(
    rich_help_panel=PANEL_RECIPES,
    epilog=(
        "Example: homing snapshot -r acme -i inputs/acme-member-1.json "
        "--base-url http://127.0.0.1:8080"
    ),
)
def snapshot(
    recipe: RecipeOpt, inputs: InputsOpt, base_url: BaseUrlOpt = None
) -> None:
    """Walk a recipe and save each page's redacted HTML as a test fixture."""
    from homing.snapshot import take_snapshots  # noqa: PLC0415

    path = recipe_path(recipe)
    start(f"snapshot {path.name} (side-effect stages are skipped)")
    for saved in asyncio.run(take_snapshots(path, inputs, base_url)):
        success(f"saved {saved}")


@app.command(
    rich_help_panel=PANEL_RECIPES,
    epilog="Example: homing schema -o recipes/schema.json",
)
def schema(
    output: Annotated[
        Path,
        typer.Option("--output", "-o", metavar="FILE", help="Where to write."),
    ] = Path("recipes/schema.json"),
) -> None:
    """Export the recipe JSON Schema (for editor autocomplete)."""
    output.write_text(
        json.dumps(Recipe.model_json_schema(), indent=2) + "\n",
        encoding="utf-8",
    )
    success(f"wrote {output}")
