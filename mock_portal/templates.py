"""Jinja environment for the mock portal's templates."""

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from mock_portal.faults import Layout, StatementMode

env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(default=True),
)
template_globals: dict[str, Any] = env.globals
template_globals.update(Layout=Layout, StatementMode=StatementMode)
