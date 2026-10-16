"""Write guard: checked against the resolved element before every click.

* An `allow` match wins.
* A `deny` match raises `WRITE_BLOCKED`.
* A click that would submit a POST form is a *write* and is blocked unless
  allow-listed. Links and plain buttons (GET navigation, menus) pass.
"""

import re
from dataclasses import dataclass

from playwright.async_api import Locator
from pydantic import TypeAdapter

from homing.errors import ErrorCode, FinalError
from homing.recipe.models import WriteGuard

_SNAPSHOT_LINE = re.compile(
    r'^-\s+(?P<role>[\w-]+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?'
)

_DESCRIBE_JS = """
el => {
  const form = el.form || el.closest('form');
  const type = (el.getAttribute('type')
    || (el.tagName === 'BUTTON' ? 'submit' : '')).toLowerCase();
  const submits = (el.tagName === 'BUTTON' || el.tagName === 'INPUT')
    && type === 'submit';
  const method = form
    ? (el.getAttribute('formmethod') || form.getAttribute('method') || 'get')
    : '';
  return {
    text: (el.getAttribute('aria-label') || el.innerText || el.value || '')
      .trim().slice(0, 200),
    posts: Boolean(form) && submits && method.toLowerCase() === 'post',
  };
}
"""


@dataclass(frozen=True)
class ElementFacts:
    """What `_DESCRIBE_JS` returns."""

    text: str
    posts: bool


FACTS = TypeAdapter(ElementFacts)


@dataclass(frozen=True)
class Clickable:
    """What the guard knows about an element."""

    role: str | None
    name: str
    posts_form: bool


async def describe(locator: Locator) -> Clickable:
    """Role and accessible name (ARIA snapshot), and whether it POSTs a form."""
    facts = FACTS.validate_python(await locator.evaluate(_DESCRIBE_JS))
    role, name = None, facts.text
    snapshot = await locator.aria_snapshot()
    match = (
        _SNAPSHOT_LINE.match(snapshot.strip().splitlines()[0])
        if snapshot.strip()
        else None
    )
    if match:
        role = match.group("role")
        name = (match.group("name") or name).replace('\\"', '"')
    return Clickable(role=role, name=name, posts_form=facts.posts)


def check(guard: WriteGuard, element: Clickable) -> str:
    """Return why the click is allowed, or raise `FinalError(WRITE_BLOCKED)`."""
    if any(rule.matches(element.role, element.name) for rule in guard.allow):
        return "allow_list"
    if any(rule.matches(element.role, element.name) for rule in guard.deny):
        msg = (
            "write guard blocked a click on "
            f"{element.role or 'element'} '{element.name}'"
        )
        raise FinalError(msg, code=ErrorCode.WRITE_BLOCKED)
    if element.posts_form:
        msg = (
            "write guard blocked an un-allow-listed form submit "
            f"'{element.name}'"
        )
        raise FinalError(msg, code=ErrorCode.WRITE_BLOCKED)
    return "not_a_write"
