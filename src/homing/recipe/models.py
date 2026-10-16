"""Pydantic models for recipes.

`recipes/schema.json` is exported from `Recipe`.
"""

import re
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

# Playwright only exposes its ARIA role Literal privately; it is the source of
# truth for `get_by_role`, so recipes are validated against exactly the roles
# Playwright accepts.
from playwright._impl._internal_structures import AriaRole
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    create_model,
    field_validator,
    model_validator,
)

ActionName = Literal[
    "goto",
    "click",
    "fill",
    "select",
    "wait_for",
    "download",
    "print_pdf",
    "switch_frame",
    "switch_tab",
    "assert_text",
    "custom",
]
StrategyName = Literal["role", "label", "near_text", "structure", "css"]


class CompareMode(StrEnum):
    """How "the same document" is judged across runs."""

    BYTES = "bytes"  # byte-identical files (downloads)
    TEXT = "text"  # same extracted text (printed pages carry timestamps)


class Strict(BaseModel):
    """Base for recipe models.

    Unknown keys are errors, so typos surface in `validate`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class Within(Strict):
    """Scope for the `structure` strategy: a container found by role/name."""

    role: AriaRole | None = None
    name: str | None = None
    css: str | None = None


class LocatorSpec(Strict):
    """Independent descriptions of one element, tried semantic to brittle.

    The order of *strategies* is fixed by the resolver (role, label, near_text,
    structure, css); a spec only supplies the descriptions it has.
    """

    field: str | None = Field(
        default=None, description="Logical name, used in logs/manifest."
    )
    role: AriaRole | None = None
    name: str | None = None
    name_regex: str | None = None
    exact: bool = False  # `name` must match whole, not as a substring
    label: str | None = None
    near_text: str | None = None
    within: Within | None = None
    tag: str | None = None
    type: str | None = None
    nth: int | None = None
    css: list[str] = Field(default_factory=list)

    @field_validator("name_regex")
    @classmethod
    def _regex_compiles(cls, value: str | None) -> str | None:
        if value is not None:
            re.compile(value)
        return value

    @property
    def strategies(self) -> list[StrategyName]:
        """Strategies this spec can feed, in resolver order."""
        out: list[StrategyName] = []
        if self.role:
            out.append("role")
        if self.label:
            out.append("label")
        if self.near_text:
            out.append("near_text")
        if self.within or self.tag:
            out.append("structure")
        if self.css:
            out.append("css")
        return out

    @property
    def display(self) -> str:
        """Human label for logs."""
        return (
            self.field or self.name or self.label or self.near_text or "element"
        )


class StepParams(LocatorSpec):
    """Locator plus action-specific parameters.

    Unused keys are rejected per action.
    """

    value: str | None = None
    url: str | None = None
    text: str | None = None
    save_as: str | None = None
    compare: CompareMode = CompareMode.BYTES
    hook: str | None = None
    optional: bool = False
    popup: bool = False
    reload: bool = False  # wait_for: reload between tries (lagging lists)
    timeout_ms: int | None = None


class Step(Strict):
    """One action. Written in YAML as a single-key mapping: `- click: {...}`."""

    action: ActionName
    params: StepParams

    @model_validator(mode="before")
    @classmethod
    def _from_single_key(cls, data: Any) -> Any:  # noqa: ANN401 - raw YAML
        if isinstance(data, dict) and "action" not in data:
            if len(data) != 1:
                msg = (
                    "a step must have exactly one action key, "
                    f"got {sorted(data)}"
                )
                raise ValueError(msg)
            ((action, params),) = data.items()
            if isinstance(params, str):
                params = (
                    {"url": params} if action == "goto" else {"value": params}
                )
            return {"action": action, "params": params or {}}
        return data


class FileCheck(Strict):
    """A captured file must exist, start with `magic`, be >= `min_bytes`."""

    path: str
    magic: str | None = None
    min_bytes: int = 1


class Postcondition(Strict):
    """What must be true after a stage.

    Success is decided here, not by "no exception".
    """

    url_matches: str | None = None
    text_present: str | None = None
    element: LocatorSpec | None = None
    read_back: list[str] = Field(default_factory=list)
    files: list[FileCheck] = Field(default_factory=list)


class IdempotencyCheck(Strict):
    """Look for the result of a side effect before performing it."""

    goto: str
    exists: LocatorSpec
    on_exists: Literal["skip"] = "skip"


class Stage(Strict):
    """A checkpointed unit of work. One DBOS step; retried from `entry_url`."""

    key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    entry_url: str | None = None
    steps: list[Step] = Field(default_factory=list)
    post: Postcondition | None = None
    side_effect: bool = False
    idempotency_check: IdempotencyCheck | None = None
    max_attempts: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def _side_effect_rules(self) -> Self:
        if self.side_effect and self.post is None:
            msg = (
                f"side-effect stage '{self.key}' needs a `post` "
                "to confirm the write landed"
            )
            raise ValueError(msg)
        return self


class NoSecondFactor(Strict):
    """The portal asks for no code."""

    type: Literal["none"] = "none"

    @property
    def secret_names(self) -> list[str]:
        """Secrets this provider reads."""
        return []


class TotpFactor(Strict):
    """RFC 6238 codes from a base32 secret (`totp_bad_first`: mock only)."""

    type: Literal["totp", "totp_bad_first"]
    secret_key: str  # name of the secret, e.g. GITHUB_TOTP_SECRET
    interval: int = 30

    @property
    def secret_names(self) -> list[str]:
        """Secrets this provider reads."""
        return [self.secret_key]


class EmailFactor(Strict):
    """Codes mailed to an inbox, fetched over IMAP."""

    type: Literal["email"]
    host: str
    user_key: str
    password_key: str
    port: int = 993
    ssl: bool = True
    from_: str | None = None
    subject: str | None = None
    code_regex: str = r"\b(\d{6})\b"
    timeout_s: float = 60.0
    poll_s: float = 2.0
    folder: str = "INBOX"

    @property
    def secret_names(self) -> list[str]:
        """Secrets this provider reads."""
        return [self.user_key, self.password_key]


class HumanFactor(Strict):
    """The operator types the code (off unless explicitly enabled)."""

    type: Literal["human"]
    timeout_s: float = 300.0

    @property
    def secret_names(self) -> list[str]:
        """Secrets this provider reads."""
        return []


SecondFactor = Annotated[
    NoSecondFactor | TotpFactor | EmailFactor | HumanFactor,
    Field(discriminator="type"),
]
"""Which `CodeProvider` to use, with its options; picked by `type`."""


class MetaCheck(Strict):
    """`<meta name=...>` must equal a secret; compared by hash, never logged."""

    name: str
    equals_secret: str


class AliveCheck(Strict):
    """A *positive* proof the session is logged in.

    A missing redirect proves nothing.
    """

    url: str
    meta: MetaCheck | None = None
    element: LocatorSpec | None = None
    absent_text: str | None = None

    @model_validator(mode="after")
    def _needs_positive_signal(self) -> Self:
        if self.meta is None and self.element is None:
            msg = "alive_check needs `meta` or `element`, a positive signal"
            raise ValueError(msg)
        return self


class TextMarker(Strict):
    """Text whose presence means something (e.g. the code was rejected)."""

    text: str
    url_matches: str | None = None


class AuthFields(Strict):
    """Locators for the login form. Each spec's `field` defaults to its key."""

    username: LocatorSpec
    password: LocatorSpec
    submit: LocatorSpec
    otp: LocatorSpec | None = None
    otp_submit: LocatorSpec | None = None

    @model_validator(mode="before")
    @classmethod
    def _name_fields(cls, data: Any) -> Any:  # noqa: ANN401 - raw YAML
        if isinstance(data, dict):
            return {
                key: {"field": key, **spec} if isinstance(spec, dict) else spec
                for key, spec in data.items()
            }
        return data


class AuthSecrets(Strict):
    """Names of secrets (keys for the `SecretProvider`), never the values."""

    username: str
    password: str


class Auth(Strict):
    """How to log in and how to tell whether we already are."""

    flow: Literal["password"] = "password"
    login_url: str
    secrets: AuthSecrets
    fields: AuthFields
    second_factor: SecondFactor = NoSecondFactor()
    otp_url_matches: str | None = None
    otp_entry_url: str | None = Field(
        default=None,
        description=(
            "Where the code form lives, for portals that open another "
            "second-factor method first (GitHub shows a passkey prompt)."
        ),
    )
    code_rejected: TextMarker | None = None
    bad_credentials: TextMarker | None = None
    interstitials: list[LocatorSpec] = Field(default_factory=list)
    alive_check: AliveCheck
    sensitive_values: list[str] = Field(
        default_factory=list,
        description=(
            "Extra secret keys whose values must be redacted (e.g. owner name)."
        ),
    )


class GuardRule(Strict):
    """Matches a clickable element by role/accessible name."""

    role: str | None = None
    name: str | None = None
    name_regex: str | None = None

    def matches(self, role: str | None, name: str) -> bool:
        """Return whether an element with this role and name matches."""
        if self.role and (role or "").lower() != self.role.lower():
            return False
        if self.name and self.name.strip().lower() != name.strip().lower():
            return False
        return not (self.name_regex and not re.search(self.name_regex, name))


class WriteGuard(Strict):
    """Allow list wins; deny list blocks. Checked before every click."""

    allow: list[GuardRule] = Field(default_factory=list)
    deny: list[GuardRule] = Field(default_factory=list)


class InputField(Strict):
    """Declared input; the run's inputs JSON is validated against these."""

    type: Literal["str", "int", "bool", "date"] = "str"
    required: bool = True
    default: Any = None
    pattern: str | None = None
    sensitive: bool = False
    description: str | None = None
    placeholder: str | None = Field(
        default=None,
        description=(
            "Text that means 'not filled in yet' (e.g. OWNER); "
            "rejected with a hint."
        ),
    )


class Redaction(Strict):
    """Recipe-declared sensitive things (PHI in production)."""

    inputs: list[str] = Field(default_factory=list)
    mask: list[LocatorSpec] = Field(default_factory=list)
    patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regexes for account identifiers the portal prints that are not "
            "inputs or secrets (e.g. numeric user ids in avatar URLs)."
        ),
    )

    @field_validator("patterns")
    @classmethod
    def _patterns_compile(cls, value: list[str]) -> list[str]:
        for pattern in value:
            re.compile(pattern)
        return value


class Recipe(Strict):
    """A portal recipe."""

    portal: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]
    version: int = 1
    base_url: str
    credential_id: str = "default"
    inputs: dict[str, InputField] = Field(default_factory=dict)
    derived: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Extra template variables computed from inputs, e.g. repo_name."
        ),
    )
    auth: Auth
    write_guard: WriteGuard = WriteGuard()
    redaction: Redaction = Redaction()
    captcha_selectors: list[str] = Field(default_factory=list)
    stages: list[Stage]

    @model_validator(mode="after")
    def _unique_stage_keys(self) -> Self:
        keys = [s.key for s in self.stages]
        dupes = {k for k in keys if keys.count(k) > 1}
        if dupes:
            msg = f"duplicate stage keys: {sorted(dupes)}"
            raise ValueError(msg)
        return self

    def stage(self, key: str) -> Stage:
        """Return the stage with `key`."""
        for stage in self.stages:
            if stage.key == key:
                return stage
        msg = f"no stage '{key}'"
        raise KeyError(msg)

    def input_model(self) -> type[BaseModel]:
        """Build a pydantic model for this recipe's inputs."""
        py_types: dict[str, type] = {
            "str": str,
            "int": int,
            "bool": bool,
            "date": str,
        }
        fields: dict[str, Any] = {}
        for name, spec in self.inputs.items():
            annotation: Any = py_types[spec.type]
            if spec.pattern:
                annotation = Annotated[annotation, Field(pattern=spec.pattern)]
            default = (
                ... if spec.required and spec.default is None else spec.default
            )
            fields[name] = (annotation, default)
        return create_model(
            f"{self.portal.title()}Inputs",
            __config__=ConfigDict(extra="forbid"),
            **fields,
        )

    @property
    def sensitive_inputs(self) -> set[str]:
        """Input names that must be redacted wherever they appear."""
        marked = {n for n, s in self.inputs.items() if s.sensitive}
        return marked | set(self.redaction.inputs)
