import sys

import pytest

from homing import dbos_app

pytestmark = pytest.mark.unit


def test_launch_registers_the_workflow_in_every_process(monkeypatch, tmp_path):
    """resume/fork/serve never import the workflow; launch must (regression)."""

    class FakeDBOS:
        def __init__(self, config):
            self.config = config

        @staticmethod
        def launch():
            pass

    monkeypatch.setattr(dbos_app, "DBOS", FakeDBOS)
    monkeypatch.setenv("HOMING_HOME", str(tmp_path))
    monkeypatch.delitem(sys.modules, "homing.engine.workflow", raising=False)
    dbos_app.get_settings.cache_clear()
    dbos_app.launch("test")
    dbos_app.get_settings.cache_clear()
    assert "homing.engine.workflow" in sys.modules
