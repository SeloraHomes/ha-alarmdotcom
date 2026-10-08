# Alarm.com — Home Assistant Integration
# Run `just` to see all available recipes.

set dotenv-load

# Prefer the tools installed by `just setup`
export PATH := justfile_directory() + "/.venv/bin:" + env_var("PATH")

component := "custom_components/alarmdotcom"

# List available recipes
default:
    @just --list

# ── Setup ─────────────────────────────────────────────────────────────────────

# Create .venv with the dev and runtime dependencies, and install the git hooks
setup:
    uv venv --allow-existing -p 3.14 .venv
    uv pip install --python .venv/bin/python -r requirements-dev.txt \
      $(python3 -c 'import json; print(" ".join(json.load(open("{{ component }}/manifest.json"))["requirements"]))')
    .venv/bin/lefthook install

# ── Test ──────────────────────────────────────────────────────────────────────

# Run the test suite
test *args='':
    pytest tests/ {{ args }}

# ── Lint & Format ────────────────────────────────────────────────────────────

# Run ruff + mypy (the fast subset; `just check` runs every lint job)
lint: lint-ruff lint-mypy

# Lint with ruff (the whole tree)
lint-ruff:
    ruff check .

# Type-check with mypy
lint-mypy:
    mypy custom_components/

# Format code
fmt:
    ruff format custom_components/ tests/
    ruff check --fix custom_components/ tests/

# Run every lefthook pre-commit job on all files, as CI does
check:
    lefthook run pre-commit --all-files

# ── Release ─────────────────────────────────────────────────────────────────

# Preview the version and notes the next release would get
release-preview:
    @python3 scripts/release.py plan --notes-file /dev/stdout

# ── Deploy ──────────────────────────────────────────────────────────────────

ha_host := env_var_or_default("HA_HOST", "root@homeassistant.local")
ha_port := env_var_or_default("HA_PORT", "22")
ha_path := env_var_or_default("HA_PATH", "~/config/custom_components/")

# Deploy to the dev HA instance and restart core.
# The restart is prefixed with `-` on purpose: `ha core restart` tears down the
# SSH session that issued it, so ssh exits non-zero even on success. The sync
# step above is *not* tolerant — a failed rsync still fails the recipe.
deploy: (_sync-to-ha)
    -ssh -p {{ ha_port }} {{ ha_host }} -t 'ha core restart'

# Deploy to the dev HA instance without restarting core
deploy-no-restart: (_sync-to-ha)

_sync-to-ha:
    rsync -az -e 'ssh -p {{ ha_port }}' --delete \
      --exclude '__pycache__' --exclude '*.pyc' \
      {{ component }}/ {{ ha_host }}:{{ ha_path }}alarmdotcom/

# Tail the HA log, filtered to this integration
logs:
    ssh -p {{ ha_port }} {{ ha_host }} -t 'ha core logs --follow' | grep -i alarmdotcom
