"""One redactor for every output path.

Covers logs, manifest, evidence, step outputs, errors.

Three layers, applied in order:

1. **Known values**: exact secrets registered at runtime (password, TOTP
   secret, every issued code, username, repo owner, ...). Replaced with
   `[REDACTED_<LABEL>]`.
2. **Patterns**: token-shaped things we never want to persist even if we never
   saw them before (CSRF tokens, signed query params, cookies, Authorization
   headers).
3. **HTML structure**: hidden input values and sensitive `<meta>` contents
   (`html()` only).
"""

import re
import threading
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from structlog.typing import EventDict, WrappedLogger

MIN_KNOWN_LENGTH = 3
"""Shorter values would shred ordinary text; nothing we register is shorter."""

SENSITIVE_QUERY_KEYS = re.compile(
    r"(?i)^(token|access_token|id_token|code|state|signature|sig"
    r"|x-amz-[a-z-]+|jwt|authenticity_token|session|sessionid|password"
    r"|otp|key)$",
)
SENSITIVE_META_NAMES = re.compile(
    r"(?i)^(csrf-token|csrf-param|user-login|octolytics-.*|.*token.*)$",
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+(?:\s+\S+)?)"),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?i)((?:set-)?cookie\s*[:=]\s*)([^\n]+)"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)(authenticity_token|csrf[_-]?token)"
            r"([\"']?\s*[:=]\s*[\"']?)"
            r"([^\"'&\s<>]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)([?&](?:token|access_token|signature|sig|jwt|code"
            r"|X-Amz-[A-Za-z-]+)=)"
            r"[^&\s\"'<>]+"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})\b"), "[REDACTED_GH_TOKEN]"),
)


class Redactor:
    """Thread-safe registry of known secret values plus static patterns."""

    def __init__(self) -> None:
        """Create an empty redactor."""
        self._lock = threading.Lock()
        self._known: dict[str, str] = {}
        self._compiled: re.Pattern[str] | None = None
        self._extra: dict[str, re.Pattern[str]] = {}

    def register(self, value: str | None, label: str = "SECRET") -> None:
        """Remember `value` so later output replaces it.

        The replacement is `[REDACTED_<LABEL>]`.
        """
        if not value or len(value) < MIN_KNOWN_LENGTH:
            return
        with self._lock:
            key = value.lower()  # portals echo usernames in their own casing
            if self._known.get(key) == label.upper():
                return
            self._known[key] = label.upper()
            ordered = sorted(self._known, key=len, reverse=True)
            self._compiled = re.compile(
                "|".join(re.escape(v) for v in ordered), re.IGNORECASE
            )

    def register_pattern(self, pattern: str) -> None:
        """Replace every match of `pattern` with `[REDACTED_ID]`."""
        with self._lock:
            self._extra.setdefault(pattern, re.compile(pattern))

    def clear(self) -> None:
        """Forget every known value and pattern (tests only)."""
        with self._lock:
            self._known.clear()
            self._compiled = None
            self._extra.clear()

    def text(self, value: str) -> str:
        """Redact a free-text string."""
        compiled = self._compiled
        if compiled is not None:
            known = self._known
            value = compiled.sub(
                lambda m: f"[REDACTED_{known[m.group(0).lower()]}]", value
            )
        for pattern, replacement in _PATTERNS:
            value = pattern.sub(replacement, value)
        for pattern in tuple(self._extra.values()):
            value = pattern.sub("[REDACTED_ID]", value)
        return value

    def url(self, value: str) -> str:
        """Redact a URL: sensitive query params, then generic text rules."""
        parts = urlsplit(value)
        if parts.query:
            query = [
                (k, "[REDACTED]" if SENSITIVE_QUERY_KEYS.match(k) else v)
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
            ]
            value = urlunsplit(
                parts._replace(query=urlencode(query, safe="[]"))
            )
        return self.text(value)

    def html(self, markup: str) -> str:
        """Redact page HTML: hidden inputs, sensitive metas, then text rules."""
        soup = BeautifulSoup(markup, "lxml")
        for node in soup.find_all("input"):
            input_type = str(node.get("type", "")).lower()
            if input_type in {"hidden", "password"} and node.get("value"):
                node["value"] = "[REDACTED]"
        for node in soup.find_all("meta"):
            name = str(node.get("name", ""))
            if SENSITIVE_META_NAMES.match(name) and node.get("content"):
                node["content"] = "[REDACTED]"
        for node in soup.find_all(["script", "noscript"]):
            node.string = "/* [REDACTED script] */" if node.string else ""
        return self.text(str(soup))

    # Redacts arbitrary JSON-like data.
    def obj(self, value: Any) -> Any:  # noqa: ANN401
        """Recursively redact every string inside a JSON-like structure."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {self.text(str(k)): self.obj(v) for k, v in value.items()}
        if isinstance(value, list | tuple | set):
            return [self.obj(v) for v in value]
        return value

    def mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Typed `obj()` for dicts: redact every key and value."""
        return {self.text(str(k)): self.obj(v) for k, v in value.items()}

    def structlog_processor(
        self,
        _logger: WrappedLogger,
        _method: str,
        event_dict: EventDict,
    ) -> EventDict:
        """`structlog` processor: redact every value of every log event."""
        return {k: self.obj(v) for k, v in event_dict.items()}


REDACTOR = Redactor()
"""Process-wide redactor. Secrets are registered the moment they are read."""
