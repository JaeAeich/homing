import json

import pytest

from homing.redact import Redactor

pytestmark = pytest.mark.unit


@pytest.fixture
def redactor():
    r = Redactor()
    r.register("hunter2-password", "GITHUB_PASSWORD")
    r.register("octocat", "GITHUB_USERNAME")
    r.register("123456", "CODE")
    return r


def test_known_values_are_replaced_with_labels(redactor):
    text = "login octocat with hunter2-password and code 123456"
    assert redactor.text(text) == (
        "login [REDACTED_GITHUB_USERNAME] with [REDACTED_GITHUB_PASSWORD] "
        "and code [REDACTED_CODE]"
    )


def test_longest_value_wins_when_values_overlap():
    r = Redactor()
    r.register("abc", "SHORT")
    r.register("abcdef", "LONG")
    assert r.text("xabcdefx") == "x[REDACTED_LONG]x"


def test_short_values_are_ignored_so_text_survives():
    r = Redactor()
    r.register("ab", "TINY")
    assert r.text("ab cab") == "ab cab"


def test_patterns_redact_tokens_never_registered():
    r = Redactor()
    assert "[REDACTED]" in r.text("Authorization: Bearer abc.def")
    assert "[REDACTED]" in r.text("cookie: user_session=xyz; other=1")
    assert "s3cr3t" not in r.text('authenticity_token="s3cr3t"')
    assert "[REDACTED_GH_TOKEN]" in r.text("token ghp_" + "a" * 36)


def test_url_sensitive_query_params(redactor):
    url = "https://x.test/octocat/repo?token=abc&page=2&X-Amz-Signature=zz"
    out = redactor.url(url)
    assert "abc" not in out
    assert "zz" not in out
    assert "page=2" in out
    assert "octocat" not in out


def test_html_hidden_inputs_metas_and_scripts():
    r = Redactor()
    html = (
        '<html><head><meta name="csrf-token" content="tok">'
        '<meta name="user-login" content="me">'
        '<script>var t="secret"</script></head><body>'
        '<input type="hidden" name="authenticity_token" value="tok2">'
        '<input name="q" value="ok">'
        "</body></html>"
    )
    out = r.html(html)
    for leaked in ('content="tok"', 'value="tok2"', "secret", 'content="me"'):
        assert leaked not in out
    assert 'value="ok"' in out


def test_obj_recurses_and_structlog_processor(redactor):
    data = {"user": "octocat", "nested": [{"code": "123456"}], "n": 3}
    assert "octocat" not in json.dumps(redactor.obj(data))
    event = redactor.structlog_processor(
        None, "info", {"event": "hi octocat", "n": 1}
    )
    assert event == {"event": "hi [REDACTED_GITHUB_USERNAME]", "n": 1}


def test_known_values_match_any_casing(redactor):
    assert redactor.text("Hello OctoCat") == "Hello [REDACTED_GITHUB_USERNAME]"


def test_registered_patterns_redact_unseen_identifiers():
    r = Redactor()
    r.register_pattern(r"(?<=githubusercontent\.com/u/)\d+")
    url = "https://avatars.githubusercontent.com/u/4242?v=4"
    assert r.text(url) == (
        "https://avatars.githubusercontent.com/u/[REDACTED_ID]?v=4"
    )
