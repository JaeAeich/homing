"""Routes for `homing serve`: Runs, Run detail, plus Settings.

HTMX conventions: a full page on normal requests, the partial on
`HX-Request`. New failures arrive as Basecoat toasts swapped out-of-band
into `#toaster`.
"""

import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import humanize
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from homing import __version__, service
from homing.errors import HomingError
from homing.paths import EvidenceFile, RunFolder, RunPaths
from homing.session import EncryptedFileSessionStore, SessionLoad
from homing.settings import Settings, get_settings
from homing.web import data
from homing.web.data import Tone

HERE = Path(__file__).parent
EMPTY = "\N{EM DASH}"
MINUTE_S = 60
HOUR_S = 60 * MINUTE_S
DAY_S = 24 * HOUR_S
SESSION_SUFFIX = ".json.enc"
HX_REQUEST = "HX-Request"
HX_TARGET = "HX-Target"
HX_REDIRECT = "HX-Redirect"


class Tab(StrEnum):
    """Tabs above the runs table."""

    ALL = "all"
    ATTENTION = "attention"
    RUNNING = "running"

    @property
    def label(self) -> str:
        """Tab caption."""
        return TAB_LABELS[self]


TAB_LABELS = {
    Tab.ALL: "All",
    Tab.ATTENTION: "Needs attention",
    Tab.RUNNING: "Running",
}


@dataclass(frozen=True)
class RecipeChoice:
    """A recipe and its inputs files, for the New run form."""

    name: str
    inputs: list[str]


@dataclass(frozen=True)
class StoredSession:
    """One encrypted session file on the Settings page."""

    portal: str
    credential: str
    state: SessionLoad | None  # None: no SESSION_KEY to try it with

    @property
    def label(self) -> str:
        """Human wording for `state`."""
        if self.state is None:
            return "stored"
        if self.state is SessionLoad.LOADED:
            return "saved"
        return self.state.replace("_", " ")


TEMPLATE_ENUMS: dict[str, type[StrEnum]] = {
    e.__name__: e for e in (Tone, Tab, EvidenceFile, RunFolder)
}
"""Enums templates compare against, so they hold no magic strings."""


def _shell_context(_: Request) -> dict[str, Any]:
    """What every page's sidebar needs."""
    return {
        "attention_count": _attention_count,
        "version": __version__,
    }


templates = Jinja2Templates(
    directory=HERE / "templates", context_processors=[_shell_context]
)


def _duration(ms: float | None) -> str:
    if ms is None:
        return EMPTY
    seconds = ms / 1000
    return (
        f"{seconds:.1f}s"
        if seconds < MINUTE_S
        else f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
    )


def _ago(when: datetime | None) -> str:
    """Compact relative time ("4m ago"), a date after a day."""
    if when is None:
        return EMPTY
    seconds = (datetime.now(UTC) - when).total_seconds()
    if seconds < MINUTE_S:
        return "just now"
    if seconds < HOUR_S:
        return f"{int(seconds // MINUTE_S)}m ago"
    if seconds < DAY_S:
        return f"{int(seconds // HOUR_S)}h ago"
    return humanize.naturaldate(when)


def _store(settings: Settings) -> EncryptedFileSessionStore | None:
    """The session store, if a key is configured."""
    if settings.session_key is None:
        return None
    key = settings.session_key.get_secret_value()
    return EncryptedFileSessionStore(settings.state_dir, key)


def _attention_count() -> int:
    return len(data.unresolved(data.finished_rows()))


# Globals, not context: imported macros (status icons) see globals only.
template_globals: dict[str, Any] = templates.env.globals
template_globals.update(TEMPLATE_ENUMS)
templates.env.filters["duration"] = _duration
templates.env.filters["ago"] = _ago


@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncGenerator[None]:
    """The dashboard is also a worker: queued runs execute in this process."""
    from homing import dbos_app  # noqa: PLC0415 - heavy import

    dbos_app.launch("dashboard")
    await dbos_app.register_queues()
    try:
        yield
    finally:
        dbos_app.shutdown()


def create_app(*, launch_dbos: bool = True) -> FastAPI:
    """Build the app. Tests pass `launch_dbos=False` and read manifests only."""
    app = FastAPI(
        title="homing",
        lifespan=_lifespan if launch_dbos else None,
        docs_url=None,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.live = launch_dbos
    _routes(app)
    return app


async def _all_rows(app: FastAPI) -> list[data.RunRow]:
    rows = data.finished_rows()
    if app.state.live:
        rows = await data.live_rows({r.run_id for r in rows}) + rows
    return rows


def _filter(rows: list[data.RunRow], tab: Tab, q: str) -> list[data.RunRow]:
    if tab is Tab.ATTENTION:
        rows = data.unresolved(rows)
    elif tab is Tab.RUNNING:
        rows = [r for r in rows if r.status in data.LIVE]
    if q:
        needle = q.lower()
        rows = [
            r
            for r in rows
            if needle in r.title.lower() or needle in r.run_id.lower()
        ]
    return rows


def _toasts(rows: list[data.RunRow], since: str | None) -> list[data.RunRow]:
    """Runs that finished after `since` (the previous poll)."""
    if not since:
        return []
    cutoff = datetime.fromisoformat(since)
    return [r for r in rows if r.finished_at and r.finished_at > cutoff]


def _recipes() -> list[RecipeChoice]:
    settings = get_settings()
    folder = Path("inputs")
    inputs = sorted(folder.glob("**/*.json")) if folder.exists() else []
    return [
        RecipeChoice(
            name=path.stem,
            inputs=[str(i) for i in inputs if i.name.startswith(path.stem)],
        )
        for path in sorted(settings.recipes_dir.glob("*.yaml"))
    ]


def _sessions(settings: Settings) -> list[StoredSession]:
    """Every stored session file, and whether it still decrypts."""
    folder = settings.state_dir
    files = sorted(folder.glob(f"*{SESSION_SUFFIX}")) if folder.exists() else []
    store = _store(settings)
    sessions = []
    for path in files:
        name = path.name.removesuffix(SESSION_SUFFIX)
        portal, _, credential = name.partition("__")
        state = store.load(portal, credential).reason if store else None
        sessions.append(StoredSession(portal, credential, state))
    return sessions


def _is_partial(request: Request, target: str) -> bool:
    """An HTMX request that only wants `target` re-rendered."""
    headers = request.headers
    return (
        headers.get(HX_REQUEST) == "true" and headers.get(HX_TARGET) == target
    )


# A flat list of small route handlers.
def _routes(app: FastAPI) -> None:  # noqa: C901
    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse("/runs")

    @app.get("/runs", response_class=HTMLResponse)
    async def runs(
        request: Request,
        tab: str = Tab.ALL,
        q: str = "",
        since: str | None = None,
    ) -> Response:
        current = Tab(tab) if tab in Tab else Tab.ALL
        rows = await _all_rows(app)
        context = {
            "tab": current,
            "q": q,
            "rows": _filter(rows, current, q),
            "counts": {t: len(_filter(rows, t, "")) for t in Tab},
            "stats": data.stats(rows),
            "now": datetime.now(UTC).isoformat(),
            "toasts": _toasts(rows, since),
            "recipes": _recipes(),
        }
        partial = _is_partial(request, "runs-body")
        name = "partials/runs_body.html" if partial else "runs.html"
        return templates.TemplateResponse(request, name, context)

    @app.post("/runs")
    async def new_run(
        request: Request,
        recipe: Annotated[str, Form()],
        inputs: Annotated[str, Form()],
        base_url: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            spec = service.prepare_run(
                recipe, Path(inputs), base_url=base_url or None
            )
            await service.start(spec, queued=True)
        except HomingError as exc:
            return templates.TemplateResponse(
                request,
                "partials/toast.html",
                {
                    "category": "error",
                    "title": exc.code.value,
                    "description": exc.message,
                },
            )
        return Response(headers={HX_REDIRECT: f"/runs/{spec.run_id}"})

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_detail(request: Request, run_id: str) -> Response:
        manifest = data.load_manifest(run_id)
        if manifest is not None:
            view = data.run_view(manifest)
        elif app.state.live:
            view = await data.live_view(run_id)
        else:
            view = None
        if view is None:
            raise HTTPException(404, f"no run {run_id}")
        partial = _is_partial(request, "run-body")
        name = "partials/run_body.html" if partial else "run.html"
        return templates.TemplateResponse(request, name, {"run": view})

    @app.get("/runs/{run_id}/stages/{key}", response_class=HTMLResponse)
    async def stage_detail(request: Request, run_id: str, key: str) -> Response:
        manifest = data.load_manifest(run_id)
        panel = data.stage_panel(manifest, key) if manifest else None
        context = {"run_id": run_id, "panel": panel, "key": key}
        return templates.TemplateResponse(
            request, "partials/stage_panel.html", context
        )

    @app.post("/runs/{run_id}/resume")
    async def resume(run_id: str) -> Response:
        result_id, _ = await service.resume(run_id, service.fork_id(run_id))
        return Response(headers={HX_REDIRECT: f"/runs/{result_id}"})

    @app.post("/runs/{run_id}/fork")
    async def fork(run_id: str, stage: Annotated[str, Form()]) -> Response:
        new_id = service.fork_id(run_id)
        await service.fork(run_id, new_id, stage)
        return Response(headers={HX_REDIRECT: f"/runs/{new_id}"})

    @app.get("/runs/{run_id}/{folder}/{name}")
    async def run_file(run_id: str, folder: str, name: str) -> FileResponse:
        if folder not in RunFolder:
            raise HTTPException(404)
        base = RunPaths.of(run_id).folder(RunFolder(folder)).resolve()
        path = (base / name).resolve()
        if base not in path.parents or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> Response:
        settings = get_settings()
        context = {
            "settings": settings,
            "db": settings.database_url.split("://", 1)[0],
            "sessions": _sessions(settings),
            "recipes": _recipes(),
        }
        return templates.TemplateResponse(request, "settings.html", context)
