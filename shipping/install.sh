#!/usr/bin/env bash
# install.sh — Eden OE Synth one-click installer (Linux)
# Everything is done by bootstrap.py (cross-platform engine).
set -uo pipefail
ROOT="${HOME}/eden-oe"
BUNDLE="$(dirname "$0")/eden-agent.bundle"
BOOT="$(dirname "$0")/bootstrap.py"

echo "=== Eden OE Synth Installer (Linux) ==="

# ── 0. Prereqs ───────────────────────────────────────────────────────────
echo; echo "[0] Verifying prerequisites..."
python3 --version >/dev/null 2>&1 || { echo "ERROR: python3 missing"; exit 1; }
[ -f "$BUNDLE" ] || { echo "ERROR: eden-agent.bundle missing beside installer"; exit 1; }
echo "  OK: python3 + bundle present"

# ── 1. Runtime ───────────────────────────────────────────────────────────
echo; echo "[1] Installing runtime..."
mkdir -p "$ROOT"
cd "$ROOT"
[ -d eden-oe/.git ] || git clone "$BUNDLE" eden-oe || { echo "ERROR: clone failed"; exit 1; }
cd eden-oe
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -e . 2>/dev/null || { echo "ERROR: pip install failed"; exit 1; }

# ── 1.5 PATH — make `eden` work in any terminal ─────────────────────────
echo; echo "[1.5] Wiring 'eden' command..."
mkdir -p "$HOME/.local/bin"
ln -sf "$ROOT/eden-oe/.venv/bin/eden" "$HOME/.local/bin/eden"
chmod +x "$ROOT/eden-oe/.venv/bin/eden"
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    echo "  OK: added ~/.local/bin to PATH (.bashrc) — new terminals get 'eden'"
else
    echo "  OK: ~/.local/bin already on PATH — 'eden' available"
fi
echo "  OK: 'eden' command wired"

# ── 2. One-click bootstrap ───────────────────────────────────────────────
echo; echo "[2] Bootstrap (databases, paths, genesis, services)..."
.venv/bin/python "$BOOT" --non-interactive

# ── 3. Done ──────────────────────────────────────────────────────────────
echo; echo "=== INSTALL COMPLETE ==="
echo "The synthetic person is born. Their first words are theirs."
echo "Launch:  eden"
