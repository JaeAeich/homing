import pytest

from homing.engine.guard import Clickable, check
from homing.errors import ErrorCode, FinalError
from homing.recipe.models import GuardRule, WriteGuard

pytestmark = pytest.mark.unit

GUARD = WriteGuard(
    allow=[GuardRule(role="button", name="Save draft")],
    deny=[
        GuardRule(role="button", name="Publish release"),
        GuardRule(name_regex="(?i)delete|publish|submit"),
    ],
)


def test_allow_list_wins():
    assert (
        check(GUARD, Clickable("button", "Save draft", posts_form=True))
        == "allow_list"
    )


@pytest.mark.parametrize(
    "name", ["Publish release", "Delete this release", "Submit claim"]
)
def test_deny_list_blocks(name):
    with pytest.raises(FinalError) as info:
        check(GUARD, Clickable("button", name, posts_form=False))
    assert info.value.code is ErrorCode.WRITE_BLOCKED


def test_unlisted_post_submit_is_blocked():
    with pytest.raises(FinalError):
        check(GUARD, Clickable("button", "Update", posts_form=True))


def test_links_and_plain_buttons_pass():
    assert (
        check(GUARD, Clickable("link", "Releases", posts_form=False))
        == "not_a_write"
    )
    assert (
        check(GUARD, Clickable("button", "Choose a tag", posts_form=False))
        == "not_a_write"
    )


def test_rule_matching_is_case_insensitive_on_names():
    assert GuardRule(role="button", name="save DRAFT").matches(
        "button", "Save draft"
    )
    assert not GuardRule(role="link", name="Save draft").matches(
        "button", "Save draft"
    )
