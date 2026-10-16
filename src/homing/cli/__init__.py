"""`homing` command line.

One pattern for every command, so it reads the same everywhere:

* What a command acts on is positional (`RUN_ID`, recipe files); settings
  are options.
* A flag means the same thing in every command: `-r/--recipe`, `-i/--inputs`,
  `-f/--field`, `-o/--output`, `--base-url`, `--html`.
* Output: `→` an action starts, `✓` success, `✗ CODE message` failure
  (stderr); the last line points at the artifact or the next command to run.
* Exit codes: 0 ok, 1 final failure, 2 retries exhausted, 3 invalid input or
  usage.
"""

from homing.cli.app import app, main

# Importing a module registers its commands; this order is the order of the
# --help panels.
# isort: off
from homing.cli import runs  # noqa: F401
from homing.cli import recipes  # noqa: F401
from homing.cli import setup  # noqa: F401
# isort: on

__all__ = ["app", "main"]
