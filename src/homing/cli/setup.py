"""Setup panel: `doctor`, `keygen`."""

import asyncio
from typing import Annotated

import typer

from homing.cli.app import (
    PANEL_SETUP,
    BaseUrlOpt,
    RecipeOpt,
    app,
    failure,
    out,
    recipe_path,
    success,
)
from homing.errors import EXIT_FINAL


@app.command(
    rich_help_panel=PANEL_SETUP, epilog="Example: homing doctor -r github"
)
def doctor(recipe: RecipeOpt = "github", base_url: BaseUrlOpt = None) -> None:
    """Preflight: secrets, TOTP drift, session decrypts, Chromium launches."""
    from homing.doctor import run_doctor  # noqa: PLC0415

    all_good = True
    for check in asyncio.run(run_doctor(recipe_path(recipe), base_url)):
        all_good &= check.ok
        if check.ok:
            success(check.message)
        else:
            failure("CHECK", check.message)
    raise typer.Exit(0 if all_good else EXIT_FINAL)


@app.command(
    rich_help_panel=PANEL_SETUP,
    epilog="Example: echo SESSION_KEY=$(homing keygen) >> .env",
)
def keygen() -> None:
    """Print a fresh SESSION_KEY (only the key, so it can be piped)."""
    from homing.session import generate_key  # noqa: PLC0415

    out.print(generate_key())


@app.command(
    rich_help_panel=PANEL_SETUP,
    epilog=(
        "Example: homing serve --port 8000  (then open http://127.0.0.1:8000)"
    ),
)
def serve(
    host: Annotated[
        str, typer.Option("--host", metavar="HOST", help="Interface to bind.")
    ] = "127.0.0.1",
    port: Annotated[
        int, typer.Option("--port", "-p", metavar="PORT", help="Port.")
    ] = 8000,
) -> None:
    """Open the dashboard.

    It also runs queued runs, so keep it running while they work.
    """
    import uvicorn  # noqa: PLC0415 - only needed here

    from homing.web.app import create_app  # noqa: PLC0415

    success(f"dashboard on http://{host}:{port}")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
