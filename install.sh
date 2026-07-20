#!/usr/bin/env bash
# Install the KOOK platform adapter into the Hermes plugins directory.
#
# Usage:
#   ./install.sh [--skip-deps] [--with-socks]
#
# Env:
#   HERMES_HOME   Hermes data directory (default: ~/.hermes)

set -euo pipefail

SKIP_DEPS=0
WITH_SOCKS=0
for arg in "$@"; do
  case "$arg" in
    --skip-deps) SKIP_DEPS=1 ;;
    --with-socks) WITH_SOCKS=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
TARGET_DIR="$HERMES_HOME/plugins/platforms/kook"

echo "Hermes home : $HERMES_HOME"
echo "Target dir  : $TARGET_DIR"

# 1. Python dependencies
if [ "$SKIP_DEPS" -eq 0 ]; then
  PIP=""
  for cmd in "pip3" "pip" "python3 -m pip" "python -m pip" "py -3 -m pip"; do
    if $cmd --version >/dev/null 2>&1; then PIP="$cmd"; break
    fi
  done
  if [ -z "$PIP" ]; then
    echo "WARNING: pip not found, skipping dependency install." >&2
    echo "         Run manually: pip install aiohttp httpx" >&2
  else
    PKGS="aiohttp>=3.8 httpx>=0.24"
    [ "$WITH_SOCKS" -eq 1 ] && PKGS="$PKGS aiohttp-socks"
    echo "Installing dependencies: $PKGS"
    # shellcheck disable=SC2086
    $PIP install $PKGS
  fi
else
  echo "Skipping dependency install (--skip-deps)"
fi

# 2. Copy plugin files
mkdir -p "$TARGET_DIR"
for f in adapter.py ws_handler.py messaging.py standalone.py constants.py config_helpers.py __init__.py plugin.yaml; do
  if [ ! -f "$SCRIPT_DIR/$f" ]; then
    echo "ERROR: missing file: $SCRIPT_DIR/$f" >&2
    exit 1
  fi
  cp "$SCRIPT_DIR/$f" "$TARGET_DIR/$f"
  echo "  copied $f"
done

# 3. Next steps
cat <<EOF

Done. Next steps:

  1. Configure the bot token in $HERMES_HOME/.env:

       KOOK_TOKEN=Bot_xxxxxxxxxxxxxxxx

     (or run the interactive setup: hermes gateway setup kook)

  2. Restart the gateway:

       hermes gateway restart

Optional env vars: KOOK_HOME_CHANNEL, KOOK_ALLOWED_USERS,
KOOK_ALLOW_ALL_USERS, KOOK_CHANNEL_PROMPT, KOOK_PROXY
See README.md for details.
EOF
