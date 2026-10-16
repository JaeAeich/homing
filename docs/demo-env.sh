# Environment for docs/demo.tape: a throwaway home and the mock's demo
# credentials (not secrets: they only exist in the local mock portal).
export HOMING_HOME=/tmp/homing-demo-gif
export PORTAL=http://127.0.0.1:8080
export BROKEN=$HOMING_HOME/broken-acme.yaml
export CHANGED=tests/fixtures/acme/eligibility.v2.html
export INPUTS=inputs/acme-member-1.json
export ACME_USERNAME=demo ACME_PASSWORD=demo-password
export ACME_IMAP_USER=demo ACME_IMAP_PASSWORD=secret
export COLUMNS=100 PS1='> '
rm -rf "$HOMING_HOME" && mkdir -p "$HOMING_HOME"
SESSION_KEY=$(uv run homing keygen)
export SESSION_KEY
uv run python tools/demo_recipe.py "$BROKEN"
homing() { uv run homing "$@"; }
say() { clear; printf '\033[1;34m▸ %s\033[0m\n\n' "$*"; }
