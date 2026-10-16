"""YAML → validated `Recipe`.

Line-numbered errors and Jinja-rendered placeholders.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

from homing.errors import ErrorCode, FinalError
from homing.recipe.models import Recipe

_JINJA = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)


@dataclass(frozen=True)
class LoadedRecipe:
    """A recipe rendered against concrete inputs."""

    recipe: Recipe
    source: Path
    sha256: str
    inputs: dict[str, Any]
    inputs_sha256: str
    context: dict[str, Any]


def sha256_bytes(data: bytes) -> str:
    """Hex sha256 of `data`."""
    return hashlib.sha256(data).hexdigest()


def resolve_recipe_path(ref: str | Path, recipes_dir: Path) -> Path:
    """Accept `github`, `github.yaml` or a path."""
    path = Path(ref)
    if path.exists():
        return path
    for candidate in (recipes_dir / f"{ref}.yaml", recipes_dir / str(ref)):
        if candidate.exists():
            return candidate
    msg = f"recipe not found: {ref}"
    raise FinalError(msg, code=ErrorCode.RECIPE_INVALID)


def _read_yaml(path: Path) -> CommentedMap:
    try:
        data = YAML(typ="rt").load(path.read_text(encoding="utf-8"))
    except YAMLError as exc:
        msg = f"{path}: invalid YAML: {exc}"
        raise FinalError(msg, code=ErrorCode.RECIPE_INVALID) from exc
    if not isinstance(data, CommentedMap):
        msg = f"{path}: top level must be a mapping"
        raise FinalError(msg, code=ErrorCode.RECIPE_INVALID)
    return data


def _line_of(
    data: Any,  # noqa: ANN401 - raw YAML
    loc: tuple[int | str, ...],
) -> int | None:
    """Best-effort 1-based line for a pydantic error loc in ruamel data."""
    line: int | None = None
    node = data
    for part in loc:
        try:
            if isinstance(node, CommentedMap) and part in node:
                line = node.lc.key(part)[0] + 1
                node = node[part]
            elif (
                isinstance(node, CommentedSeq)
                and isinstance(part, int)
                and part < len(node)
            ):
                line = node.lc.item(part)[0] + 1
                node = node[part]
            elif isinstance(node, CommentedSeq | CommentedMap):
                # pydantic adds synthetic loc parts (e.g. union tags); skip them
                continue
            else:
                break
        except (KeyError, IndexError, AttributeError):
            break
    return line


def format_validation_error(
    path: Path,
    data: Any,  # noqa: ANN401 - raw YAML
    exc: ValidationError,
) -> str:
    """Render a pydantic error as `file:line: loc: message` lines."""
    lines = []
    for err in exc.errors():
        loc = tuple(err["loc"])
        line = _line_of(data, loc)
        where = f"{path}:{line}" if line else str(path)
        dotted = ".".join(str(p) for p in loc) or "<root>"
        lines.append(f"{where}: {dotted}: {err['msg']}")
    return "\n".join(lines)


def _plain(data: Any) -> Any:  # noqa: ANN401 - raw YAML
    """Ruamel round-trip types → plain dict/list."""
    return json.loads(json.dumps(data, default=str))


def validate_recipe_file(path: Path) -> Recipe:
    """Validate structure only (placeholders left unrendered)."""
    data = _read_yaml(path)
    try:
        return Recipe.model_validate(_plain(data))
    except ValidationError as exc:
        raise FinalError(
            format_validation_error(path, data, exc),
            code=ErrorCode.RECIPE_INVALID,
        ) from exc


# JSON-like tree.
def _render(value: Any, context: dict[str, Any]) -> Any:  # noqa: ANN401
    if isinstance(value, str) and "{{" in value:
        return _JINJA.from_string(value).render(context)
    if isinstance(value, dict):
        return {k: _render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, context) for v in value]
    return value


def validate_inputs(recipe: Recipe, inputs: dict[str, Any]) -> dict[str, Any]:
    """Validate inputs against the recipe's declared input model."""
    for name, spec in recipe.inputs.items():
        value = inputs.get(name)
        if (
            spec.placeholder
            and isinstance(value, str)
            and spec.placeholder in value
        ):
            msg = (
                f"inputs invalid: '{name}' still contains the placeholder "
                f"{spec.placeholder!r}; edit the file (or run "
                "`make bootstrap`, which writes inputs/local/)"
            )
            raise FinalError(msg, code=ErrorCode.INPUT_INVALID)
    model: type[BaseModel] = recipe.input_model()
    try:
        return model.model_validate(inputs).model_dump()
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        )
        msg = f"inputs invalid: {details}"
        raise FinalError(msg, code=ErrorCode.INPUT_INVALID) from exc


def load_inputs(path: Path) -> tuple[dict[str, Any], str]:
    """Read an inputs JSON file, returning the data and its sha256."""
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"cannot read inputs {path}: {exc}"
        raise FinalError(msg, code=ErrorCode.INPUT_INVALID) from exc
    if not isinstance(data, dict):
        msg = f"inputs {path} must be a JSON object"
        raise FinalError(msg, code=ErrorCode.INPUT_INVALID)
    return data, sha256_bytes(raw)


def load_recipe(
    path: Path,
    inputs: dict[str, Any],
    *,
    inputs_sha256: str = "",
    base_url: str | None = None,
) -> LoadedRecipe:
    """Validate the recipe and inputs, then render every `{{placeholder}}`."""
    raw_bytes = path.read_bytes()
    template = validate_recipe_file(path)
    clean_inputs = validate_inputs(template, inputs)

    context: dict[str, Any] = {
        **clean_inputs,
        "base_url": base_url or template.base_url,
    }
    try:
        for key, expr in template.derived.items():
            context[key] = _JINJA.from_string(expr).render(context)
        data = _plain(_read_yaml(path))
        data["base_url"] = context["base_url"]
        rendered = _render(data, context)
    except TemplateError as exc:
        msg = f"{path}: placeholder error: {exc}"
        raise FinalError(msg, code=ErrorCode.RECIPE_INVALID) from exc

    try:
        recipe = Recipe.model_validate(rendered)
    except ValidationError as exc:
        msg = f"{path}: invalid after rendering:\n{exc}"
        raise FinalError(msg, code=ErrorCode.RECIPE_INVALID) from exc

    return LoadedRecipe(
        recipe=recipe,
        source=path,
        sha256=sha256_bytes(raw_bytes),
        inputs=clean_inputs,
        inputs_sha256=inputs_sha256
        or sha256_bytes(json.dumps(inputs, sort_keys=True).encode()),
        context=context,
    )
