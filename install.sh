#!/usr/bin/env bash
# vac installer — no pipx required.
# Creates an isolated venv under ~/.vac and links `vac` into ~/.local/bin.
set -euo pipefail

REPO="${VAC_REPO:-https://github.com/homeo26/vac.git}"
PREFIX="${VAC_PREFIX:-$HOME/.local/bin}"
VENV="$HOME/.vac"

echo "Installing vac into $VENV, linking to $PREFIX/vac"

# Prefer uv if present (fast, isolated), else fall back to python venv + pip.
if command -v uv >/dev/null 2>&1; then
  uv tool install "git+$REPO"
  echo "Installed via uv. Ensure $(dirname "$(uv tool dir)")/bin or ~/.local/bin is on PATH."
  exit 0
fi

PY="$(command -v python3 || true)"
[ -z "$PY" ] && { echo "python3 not found"; exit 1; }

"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet "git+$REPO"

mkdir -p "$PREFIX"
ln -sf "$VENV/bin/vac" "$PREFIX/vac"
echo "Linked $PREFIX/vac -> $VENV/bin/vac"

case ":$PATH:" in
  *":$PREFIX:"*) : ;;
  *) echo "NOTE: add $PREFIX to your PATH:  export PATH=\"$PREFIX:\$PATH\"" ;;
esac

echo "Done. Run: vac --help"
