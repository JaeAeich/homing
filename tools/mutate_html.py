r"""Write mutated copies of a saved page: proves the resolver survives drift.

Variants: renamed classes/ids, reordered siblings, extra wrapper divs, form
moved into an iframe (`srcdoc`), and all of them combined.

    uv run python tools/mutate_html.py \
        tests/fixtures/acme/eligibility.html --all
"""

import argparse
import hashlib
from collections.abc import Callable
from pathlib import Path

from bs4 import BeautifulSoup, Tag

CONTROLS = ("input", "select", "textarea", "button")


def _rename(value: str) -> str:
    return (
        "m"
        + hashlib.sha1(value.encode(), usedforsecurity=False).hexdigest()[:6]
    )


def rename_classes(soup: BeautifulSoup) -> BeautifulSoup:
    """Rename every class and id.

    Keeps `<label for>` consistent, like a build tool would.
    """
    for node in soup.find_all(name=True):
        if node.get("class"):
            node["class"] = " ".join(
                _rename(c) for c in node.get_attribute_list("class")
            )
        if node.get("id"):
            node["id"] = _rename(str(node["id"]))
        if node.name == "label" and node.get("for"):
            node["for"] = _rename(str(node["for"]))
    return soup


def reorder_siblings(soup: BeautifulSoup) -> BeautifulSoup:
    """Reverse each form's children, keeping label+control pairs together."""
    for form in soup.find_all("form"):
        children = [c for c in form.children if isinstance(c, Tag)]
        groups: list[list[Tag]] = []
        for child in children:
            if groups and groups[-1][-1].name == "label":
                groups[-1].append(child)
            else:
                groups.append([child])
        for child in children:
            child.extract()
        for group in reversed(groups):
            for child in group:
                form.append(child)
    return soup


def add_wrappers(soup: BeautifulSoup) -> BeautifulSoup:
    """Wrap every form control in two extra `<div>`s."""
    for control in soup.find_all(CONTROLS):
        outer = soup.new_tag("div", attrs={"class": "wrapper-outer"})
        inner = soup.new_tag("div", attrs={"class": "wrapper-inner"})
        control.wrap(inner)
        inner.wrap(outer)
    return soup


def move_into_iframe(soup: BeautifulSoup) -> BeautifulSoup:
    """Move the first form into an `<iframe srcdoc>` (scripts go with it)."""
    form = soup.find("form")
    if not isinstance(form, Tag):
        return soup
    scripts = [
        s for s in soup.find_all("script") if s.find_previous("form") is form
    ]
    payload = str(form) + "".join(str(s) for s in scripts)
    iframe = soup.new_tag(
        "iframe",
        attrs={
            "title": "Embedded form",
            "name": "embedded-form",
            "srcdoc": f"<!doctype html><html><body>{payload}</body></html>",
            "style": "width:800px;height:500px",
        },
    )
    form.replace_with(iframe)
    for script in scripts:
        script.decompose()
    return soup


VARIANTS: dict[str, Callable[[BeautifulSoup], BeautifulSoup]] = {
    "renamed": rename_classes,
    "reordered": reorder_siblings,
    "wrapped": add_wrappers,
    "iframe": move_into_iframe,
}


def mutate(source: str, names: list[str]) -> str:
    """Apply the named variants in order."""
    soup = BeautifulSoup(source, "lxml")
    for name in names:
        soup = VARIANTS[name](soup)
    return str(soup)


def write_variants(path: Path, out_dir: Path | None = None) -> list[Path]:
    """Write every variant plus a combined one.

    Written next to `path` (or in `out_dir`).
    """
    source = path.read_text(encoding="utf-8")
    target_dir = out_dir or path.parent / "mutated"
    target_dir.mkdir(parents=True, exist_ok=True)
    plans = {name: [name] for name in VARIANTS} | {"combined": list(VARIANTS)}
    written = []
    for label, names in plans.items():
        target = target_dir / f"{path.stem}.{label}.html"
        target.write_text(mutate(source, names), encoding="utf-8")
        written.append(target)
    return written


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path)
    parser.add_argument(
        "--all", action="store_true", help="write every variant"
    )
    parser.add_argument(
        "--variant", choices=sorted(VARIANTS), action="append", default=[]
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    if args.all:
        for written in write_variants(args.file, args.out):
            print(written)
    else:
        print(mutate(args.file.read_text(encoding="utf-8"), args.variant))


if __name__ == "__main__":
    main()
