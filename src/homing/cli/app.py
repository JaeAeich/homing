"""The Typer app, shared options, output helpers and central error handling."""

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

# Typer vendors click; its group/context/usage-error types live here (what
# TyperGroup uses).
from typer._click.core import Context
from typer._click.exceptions import UsageError
from typer.core import TyperGroup

from homing.errors import (
    EXIT_INVALID,
    ErrorCode,
    FinalError,
    HomingError,
    exit_code_for,
)
from homing.observability import configure_logging
from homing.recipe.loader import resolve_recipe_path
from homing.settings import get_settings

PANEL_RUN = "Run"
PANEL_RECIPES = "Recipes"
PANEL_SETUP = "Setup"

out = Console(soft_wrap=True, highlight=False)  # colour only where we ask
err = Console(stderr=True, soft_wrap=True, highlight=False)


# ---------------------------------------------------------------- output


def start(message: str) -> None:
    """`→` an action begins."""
    out.print(f"→ {message}")


def success(message: str) -> None:
    """`✓` something worked."""
    out.print(f"[green]✓[/green] {message}")


def failure(code: ErrorCode | str, message: str) -> None:
    """`✗ CODE message` on stderr."""
    code_text = code.value if isinstance(code, ErrorCode) else code
    err.print(f"[red]✗ {code_text}[/red] {escape(message)}")


# Paths, commands, anything printable.
def hint(label: str, value: Any) -> None:  # noqa: ANN401
    """An indented `label: value` line: an artifact or the next command."""
    out.print(f"  {label + ':':<9} {value}")


def invalid(message: str) -> FinalError:
    """A user error (bad argument, unknown run...): exit 3, like bad input."""
    return FinalError(message, code=ErrorCode.INPUT_INVALID)


# ---------------------------------------------------------------- errors


class HomingGroup(TyperGroup):
    """Handles every command's errors, so all look and exit the same."""

    # Whatever the command returns.
    def invoke(self, ctx: Context) -> Any:  # noqa: ANN401
        """Run the command; map our errors to `✗ CODE message` + exit code."""
        try:
            return super().invoke(ctx)
        except HomingError as exc:
            failure(exc.code, exc.message)
            raise typer.Exit(exit_code_for(exc.code)) from None
        except UsageError as exc:
            # click exits 2 on usage errors; here 2 means "retries exhausted".
            exc.exit_code = EXIT_INVALID
            raise


app = typer.Typer(
    cls=HomingGroup,
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    help="Durable, recipe-driven, deterministic portal automation.",
    epilog=(
        "Flags mean the same everywhere: "
        "-r recipe, -i inputs, -f field, -o output. "
        "Exit codes: 0 ok · 1 final failure · 2 retries exhausted · "
        "3 invalid input."
    ),
)


@app.callback()
def _global(
    json_logs: Annotated[
        bool, typer.Option("--json-logs", help="Logs as JSON lines.")
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet", "-q", help="Only warnings and errors in the log."
        ),
    ] = False,
) -> None:
    configure_logging(json_logs=json_logs, quiet=quiet)


def main() -> None:
    """Console-script entry point."""
    app()


# ---------------------------------------------------- shared arguments

RunIdArg = Annotated[
    str,
    typer.Argument(
        metavar="RUN_ID",
        help="As printed by `homing run` or `homing runs list`.",
    ),
]
RecipeOpt = Annotated[
    str,
    typer.Option(
        "--recipe",
        "-r",
        metavar="NAME|PATH",
        help="Recipe name (recipes/) or path.",
    ),
]
InputsOpt = Annotated[
    Path,
    typer.Option(
        "--inputs",
        "-i",
        metavar="FILE",
        help="Inputs JSON file.",
        exists=True,
        dir_okay=False,
    ),
]
OptionalInputsOpt = Annotated[
    Path | None,
    typer.Option(
        "--inputs",
        "-i",
        metavar="FILE",
        help="Inputs JSON file (fills {{placeholders}}).",
        exists=True,
        dir_okay=False,
    ),
]
BaseUrlOpt = Annotated[
    str | None,
    typer.Option(
        "--base-url",
        metavar="URL",
        help="Override the recipe's base_url (mock portal).",
    ),
]


def recipe_path(ref: str) -> Path:
    """`github`, `github.yaml` or a path → the recipe file."""
    return resolve_recipe_path(ref, get_settings().recipes_dir)
