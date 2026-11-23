#!/bin/sh
# Install skills-lite for Claude Code and Codex (macOS, Linux).
#   ./install.sh               install or update (safe to re-run)
#   ./install.sh --uninstall   remove what the installer added
set -eu
here=$(cd "$(dirname "$0")" && pwd)

py=""
for c in python3 /usr/bin/python3; do
  p=$(command -v "$c" 2>/dev/null) || continue
  if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    py=$p
    break
  fi
done
if [ -z "$py" ]; then
  echo "install.sh: python3 3.8 or newer is required" >&2
  exit 1
fi

case "${1:-}" in
  --uninstall) exec "$py" "$here/bin/agentloop" uninstall ;;
  "") exec "$py" "$here/bin/agentloop" install --python "$py" --source "$here" ;;
  *) echo "usage: ./install.sh [--uninstall]" >&2; exit 2 ;;
esac
