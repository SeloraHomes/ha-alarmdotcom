# Alarm.com — Home Assistant Integration
# Run `just` to see all available recipes.

set dotenv-load

component := "custom_components/alarmdotcom"

# List available recipes
default:
    @just --list

# ── Test ──────────────────────────────────────────────────────────────────────

# Run the test suite
test *args='':
    pytest tests/ {{ args }}

# ── Lint & Format ────────────────────────────────────────────────────────────

# Run ruff + mypy (the fast subset; `just check` runs every pre-commit hook)
lint: lint-ruff lint-mypy

# Lint with ruff (same scope as the pre-commit hook: the whole tree)
lint-ruff:
    ruff check .

# Type-check with mypy
lint-mypy:
    mypy custom_components/

# Format code
fmt:
    ruff format custom_components/ tests/
    ruff check --fix custom_components/ tests/

# Run the full pre-commit suite
check:
    pre-commit run --all-files

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
