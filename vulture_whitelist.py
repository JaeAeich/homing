"""Names vulture can't see being used; frameworks read them, not our code.

Regenerate a draft with `uv run vulture --make-whitelist`; keep only what is
justified here.
"""

# pydantic: model configuration and validated-but-unbranched recipe fields
model_config  # read by pydantic
on_exists  # only "skip" exists today; validated so recipes stay explicit

# Manifest fields: written to manifest.json, read by dashboard and reviewers
resolver
text_sha256
homing
git_sha
finished_at
pages_visited

# Playwright storage_state structure, validated field by field
domain
origin
localStorage
cookies
origins

# Dashboard view fields: read by Jinja templates
total
success_rate
median_ms
offset_ms
tone
segments
total_ms
failed_stage
resumable
number
hints
locators

# Enum members and fields used by recipes, templates or mock fault specs
TEXT  # CompareMode, chosen in recipes
V2  # mock Layout, compared in templates
IFRAME
NEW_TAB  # mock StatementMode, compared in templates
dob  # mock Member, rendered in templates

# Only in a string annotation (TYPE_CHECKING import)
StepInfo

# click: UsageError.exit_code is read by click when it exits
_.exit_code
