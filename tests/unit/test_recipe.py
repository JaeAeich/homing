import json
from pathlib import Path

import pytest

from homing.errors import ErrorCode, FinalError
from homing.recipe.loader import load_recipe, validate_recipe_file
from homing.recipe.models import Recipe, Step

pytestmark = pytest.mark.unit

RECIPES = Path("recipes")


def _inputs(name):
    return json.loads(Path("inputs", name).read_text())


@pytest.mark.parametrize("path", sorted(RECIPES.glob("*.yaml")))
def test_shipped_recipes_validate(path):
    assert isinstance(validate_recipe_file(path), Recipe)


def test_different_inputs_render_different_steps():
    a = load_recipe(RECIPES / "acme.yaml", _inputs("acme-member-1.json")).recipe
    b = load_recipe(RECIPES / "acme.yaml", _inputs("acme-member-2.json")).recipe

    def pick(recipe: Recipe) -> str | None:
        return recipe.stage("search_member").steps[0].params.value

    assert pick(a) == "Jane"
    assert pick(b) == "John"
    assert (
        a.stage("capture_statement").post != b.stage("capture_statement").post
    )


def test_base_url_override_is_rendered_everywhere():
    loaded = load_recipe(
        RECIPES / "acme.yaml",
        _inputs("acme-member-1.json"),
        base_url="http://h:1",
    )
    assert loaded.recipe.auth.login_url == "http://h:1/login"
    assert (
        loaded.recipe.stage("search_member").entry_url == "http://h:1/members"
    )


def test_missing_input_is_input_invalid():
    inputs = _inputs("acme-member-1.json")
    del inputs["plan"]
    with pytest.raises(FinalError) as info:
        load_recipe(RECIPES / "acme.yaml", inputs)
    assert info.value.code is ErrorCode.INPUT_INVALID


def test_pattern_violation_is_input_invalid():
    inputs = _inputs("acme-member-1.json") | {"member_id": "not-an-id"}
    with pytest.raises(FinalError) as info:
        load_recipe(RECIPES / "acme.yaml", inputs)
    assert info.value.code is ErrorCode.INPUT_INVALID


def test_unknown_input_is_rejected():
    inputs = _inputs("acme-member-1.json") | {"surprise": 1}
    with pytest.raises(FinalError):
        load_recipe(RECIPES / "acme.yaml", inputs)


def test_errors_carry_line_numbers(tmp_path):
    text = (
        (RECIPES / "acme.yaml")
        .read_text()
        .replace("  - key: open_member", "  - key: Bad-Key\n    oops: 1")
    )
    bad = tmp_path / "bad.yaml"
    bad.write_text(text)
    with pytest.raises(FinalError) as info:
        validate_recipe_file(bad)
    lines = text.splitlines()
    key_line = next(i for i, line in enumerate(lines, 1) if "Bad-Key" in line)
    assert f"bad.yaml:{key_line}: stages." in info.value.message
    assert f"bad.yaml:{key_line + 1}: stages." in info.value.message
    assert info.value.code is ErrorCode.RECIPE_INVALID


def test_undefined_placeholder_is_recipe_invalid(tmp_path):
    text = (
        (RECIPES / "acme.yaml")
        .read_text()
        .replace("{{ member_query }}", "{{ nope }}")
    )
    bad = tmp_path / "bad.yaml"
    bad.write_text(text)
    with pytest.raises(FinalError) as info:
        load_recipe(bad, _inputs("acme-member-1.json"))
    assert info.value.code is ErrorCode.RECIPE_INVALID


def test_jinja_is_sandboxed(tmp_path):
    text = (
        (RECIPES / "acme.yaml")
        .read_text()
        .replace(
            "{{ member_query }}",
            "{{ ''.__class__.__mro__[1].__subclasses__() }}",
        )
    )
    bad = tmp_path / "bad.yaml"
    bad.write_text(text)
    with pytest.raises(FinalError):
        load_recipe(bad, _inputs("acme-member-1.json"))


def test_step_single_key_shorthand():
    step = Step.model_validate({"goto": "https://x"})
    assert step.action == "goto"
    assert step.params.url == "https://x"
    with pytest.raises(ValueError, match="exactly one action"):
        Step.model_validate({"goto": "a", "click": {}})


def test_side_effect_stage_requires_post(tmp_path):
    text = (
        (RECIPES / "acme.yaml")
        .read_text()
        .replace(
            '    post: { url_matches: "/members/M-\\\\d+$", '
            'text_present: "{{ note }}" }\n',
            "",
        )
    )
    bad = tmp_path / "bad.yaml"
    bad.write_text(text)
    with pytest.raises(FinalError, match="side-effect"):
        validate_recipe_file(bad)
