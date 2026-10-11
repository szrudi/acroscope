#!/bin/sh
# Headless Chromium for the player tests (tests/test_player.py), without root.
#
# Playwright goes into the dev venv (the optional extra `e2e`; the app itself stays stdlib + orangebox), the browser
# into ~/.cache/ms-playwright (Playwright's own cache), and the shared libraries the browser needs that the machine
# lacks are fetched with `apt-get download` and unpacked into ~/.cache/acroscope-browser (no install, no root).
# The tests put that directory on LD_LIBRARY_PATH when they launch the browser. Safe to run again.
#
#   scripts/browser-setup.sh            (from the repo root; VENV=... for another venv)
set -eu
VENV=${VENV:-.venv}
LIBS=${ACROSCOPE_BROWSER_LIBS:-$HOME/.cache/acroscope-browser}

# playwright only (the `e2e` extra's one package): `pip install -e .[e2e]` would point the venv's editable install
# at wherever this runs from, a worktree included
"$VENV/bin/pip" install -q playwright
"$VENV/bin/playwright" install --only-shell chromium

SHELL_BIN=$(ls -d "$HOME"/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell 2>/dev/null | sort -V | tail -1)
[ -x "$SHELL_BIN" ] || { echo "no headless shell found under ~/.cache/ms-playwright" >&2; exit 1; }

missing() { LD_LIBRARY_PATH="$LIBS/usr/lib/x86_64-linux-gnu" ldd "$SHELL_BIN" | awk '/not found/ {print $1}'; }
# the libraries a bare Debian 13 (trixie) machine lacks, and their packages; another release may name them otherwise
pkg_of() {
  case "$1" in
    libatk-1.0.so.0) echo libatk1.0-0t64 ;;
    libatk-bridge-2.0.so.0) echo libatk-bridge2.0-0t64 ;;
    libatspi.so.0) echo libatspi2.0-0t64 ;;
    libXcomposite.so.1) echo libxcomposite1 ;;
    libXdamage.so.1) echo libxdamage1 ;;
    *) echo "" ;;
  esac
}

need=$(missing)
if [ -n "$need" ]; then
  tmp=$(mktemp -d)
  for lib in $need; do
    p=$(pkg_of "$lib")
    if [ -z "$p" ]; then echo "no package known for $lib: add it to pkg_of, or run 'sudo $VENV/bin/playwright install-deps chromium'" >&2; continue; fi
    (cd "$tmp" && apt-get download -q "$p" >/dev/null)
  done
  mkdir -p "$LIBS"
  for d in "$tmp"/*.deb; do [ -e "$d" ] && dpkg-deb -x "$d" "$LIBS"; done
  rm -rf "$tmp"
fi

left=$(missing)
if [ -n "$left" ]; then echo "still missing: $left" >&2; exit 1; fi
echo "headless Chromium ready: $SHELL_BIN (libraries from $LIBS)"
