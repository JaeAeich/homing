import sys
from pathlib import Path

import pytest

from homing.recipe.models import LocatorSpec, Recipe
from homing.resolver.resolver import resolve_once

sys.path.insert(0, str(Path(__file__).parents[2] / "tools"))
from mutate_html import VARIANTS, write_variants

pytestmark = [pytest.mark.offline, pytest.mark.asyncio(loop_scope="module")]

FIXTURES = Path(__file__).parent.parent / "fixtures"

CASES = [
    ("acme/login.v1.html", "username"),
    ("acme/login.v1.html", "password"),
    ("acme/login.v1.html", "submit"),
    ("acme/mfa.v1.html", "otp"),
    ("acme/search_member.html", "member_search"),
    ("acme/search_member.html", "member_link"),
    ("acme/member.v1.html", "note"),
    ("acme/eligibility.v1.html", "plan"),
    ("acme/eligibility.v1.html", "service_type"),
    ("acme/eligibility.v1.html", "date_of_service"),
    ("acme/eligibility.v1.html", "check"),
    (
        "github/login.html",
        "username",
    ),  # real page, saved with `homing snapshot`-style redaction
    ("github/login.html", "password"),
    ("github/login.html", "submit"),
]
OUTSIDE_FORMS = {"member_link"}


def field_spec(recipe: Recipe, name: str) -> LocatorSpec:
    fields = recipe.auth.fields
    if name in type(fields).model_fields and getattr(fields, name):
        return getattr(fields, name)
    for stage in recipe.stages:
        for step in stage.steps:
            if step.params.field == name:
                return step.params
    raise KeyError(name)


def recipe_for(recipes, fixture: str) -> Recipe:
    return recipes[fixture.split("/", maxsplit=1)[0]]


@pytest.fixture(scope="module")
def mutated(tmp_path_factory):
    out = tmp_path_factory.mktemp("mutated")
    return {
        fixture: write_variants(FIXTURES / fixture, out / fixture.split("/")[0])
        for fixture in {fixture for fixture, _ in CASES}
    }


@pytest.mark.parametrize(("fixture", "field"), CASES)
async def test_resolves_on_saved_page_with_preferred_strategy(
    open_html, recipes, fixture, field
):
    page = await open_html(FIXTURES / fixture)
    found, attempts = await resolve_once(
        page, field_spec(recipe_for(recipes, fixture), field)
    )
    assert found is not None, attempts
    assert not found.drift, f"{field} fell back to {found.strategy}: {attempts}"
    assert found.frame == "main"


@pytest.mark.parametrize("variant", [*VARIANTS, "combined"])
@pytest.mark.parametrize(("fixture", "field"), CASES)
async def test_survives_every_mutation(
    open_html, recipes, mutated, fixture, field, variant
):
    path = next(
        p for p in mutated[fixture] if p.name.endswith(f".{variant}.html")
    )
    page = await open_html(path)
    found, attempts = await resolve_once(
        page, field_spec(recipe_for(recipes, fixture), field)
    )
    assert found is not None, f"{field} lost in {variant}: {attempts}"
    if variant in {"iframe", "combined"} and field not in OUTSIDE_FORMS:
        assert found.frame != "main", (
            "the control moved into an iframe and must be found there"
        )


@pytest.mark.parametrize(
    ("fixture", "field", "strategy"),
    [
        ("acme/login.v2.html", "submit", "structure"),
        ("acme/login.v2.html", "username", "near_text"),
        ("acme/eligibility.v2.html", "plan", "near_text"),
        ("acme/eligibility.v2.html", "service_type", "near_text"),
        ("acme/eligibility.v2.html", "date_of_service", "near_text"),
        ("acme/eligibility.v2.html", "check", "structure"),
    ],
)
async def test_layout_v2_falls_back_and_flags_drift(
    open_html, acme_recipe, fixture, field, strategy
):
    page = await open_html(FIXTURES / fixture)
    found, attempts = await resolve_once(page, field_spec(acme_recipe, field))
    assert found is not None, attempts
    assert found.strategy == strategy
    assert found.drift is True
    rejected = [a for a in attempts if a.rejected_reason]
    assert rejected, "the failed preferred strategy must be recorded"


AMBIGUOUS = """<html><body>
<form><button type="button">Search</button></form>
<form><button type="button" id="the-one">Search</button></form>
</body></html>"""


async def test_ambiguous_match_falls_through(open_html, tmp_path):
    path = tmp_path / "ambiguous.html"
    path.write_text(AMBIGUOUS)
    page = await open_html(path)
    spec = LocatorSpec(
        field="search", role="button", name="Search", css=["#the-one"]
    )
    found, attempts = await resolve_once(page, spec)
    assert found is not None
    assert found.strategy == "css"
    role_attempt = next(a for a in attempts if a.strategy == "role")
    assert role_attempt.matches == 2
    assert role_attempt.rejected_reason == "ambiguous"


async def test_nothing_matches_records_every_attempt(open_html, tmp_path):
    path = tmp_path / "empty.html"
    path.write_text("<html><body><p>nothing here</p></body></html>")
    page = await open_html(path)
    spec = LocatorSpec(
        field="x",
        role="button",
        name="Go",
        label="Go",
        near_text="Go",
        css=["#go", ".go"],
    )
    found, attempts = await resolve_once(page, spec)
    assert found is None
    assert {a.strategy for a in attempts} >= {"role", "label", "css"}
    assert all(a.rejected_reason == "no_match" for a in attempts)


async def test_hidden_elements_do_not_count(open_html, tmp_path):
    path = tmp_path / "hidden.html"
    path.write_text(
        '<button style="display:none">Go</button><button>Go</button>'
    )
    page = await open_html(path)
    found, _ = await resolve_once(page, LocatorSpec(role="button", name="Go"))
    assert found is not None
    assert found.strategy == "role"


async def test_hidden_duplicate_does_not_break_the_action(open_html, tmp_path):
    """Counted visible-only, so the locator must be too (audit bug)."""
    path = tmp_path / "dupes.html"
    path.write_text(
        '<input class="name" style="display:none"><input class="name">'
    )
    page = await open_html(path)
    found, _ = await resolve_once(
        page, LocatorSpec(field="name", css=[".name"])
    )
    assert found is not None
    await found.locator.fill("works")  # strict mode would raise on two matches
    assert await page.locator(".name").nth(1).input_value() == "works"


async def test_main_frame_wins_over_iframe(open_html, tmp_path):
    path = tmp_path / "frames.html"
    path.write_text(
        '<button>Go</button><iframe srcdoc="<button>Go</button>"></iframe>',
    )
    page = await open_html(path)
    await page.wait_for_timeout(200)
    found, _ = await resolve_once(page, LocatorSpec(role="button", name="Go"))
    assert found is not None
    assert found.frame == "main"
