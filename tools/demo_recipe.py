"""Write acme.yaml with the statement link renamed: the demo's broken step.

Usage: `uv run python tools/demo_recipe.py OUT.yaml`.
"""

import sys
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).parents[1]


def main() -> None:
    """Point the download step at a link that does not exist."""
    yaml = YAML()
    recipe = yaml.load(ROOT / "recipes" / "acme.yaml")
    capture = next(
        s for s in recipe["stages"] if s["key"] == "capture_statement"
    )
    download = capture["steps"][0]["download"]
    download["name"] = "Download receipt"
    download["css"] = ["a.receipt"]
    yaml.dump(recipe, Path(sys.argv[1]))


if __name__ == "__main__":
    main()
