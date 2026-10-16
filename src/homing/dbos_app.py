"""DBOS setup: SQLite under `state/`, Postgres via `HOMING_DB_URL`."""

import importlib

from dbos import DBOS, DBOSConfig, DBOSPortableJSONSerializer

from homing.settings import get_settings


def launch(executor_id: str) -> None:
    """Configure and launch DBOS.

    `executor_id` is the run id: on launch DBOS recovers only *this
    executor's* pending workflows, so `homing resume <run_id>` picks up
    exactly that crashed run and nothing else.
    """
    # Importing the module registers `run_recipe` with DBOS. It must happen
    # before launch in every process (run, resume, fork, serve), or
    # recovered/forked workflows can't execute.
    importlib.import_module("homing.engine.workflow")
    settings = get_settings()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    config: DBOSConfig = {
        "name": "homing",
        "system_database_url": settings.database_url,
        "executor_id": executor_id,
        "log_level": "WARNING",
        # JSON, not pickle: rows stay human-auditable and leak-scannable.
        "serializer": DBOSPortableJSONSerializer(),
    }
    DBOS(config=config)
    DBOS.launch()


async def register_queues() -> None:
    """Register the dashboard's run queue (persisted in DBOS system DB)."""
    from homing.engine.workflow import (  # noqa: PLC0415
        RUN_QUEUE,
        RUN_QUEUE_WORKERS,
    )

    await DBOS.register_queue_async(
        RUN_QUEUE, worker_concurrency=RUN_QUEUE_WORKERS
    )


def shutdown() -> None:
    """Destroy the DBOS instance (flushes and closes the system DB)."""
    DBOS.destroy(destroy_registry=False)
