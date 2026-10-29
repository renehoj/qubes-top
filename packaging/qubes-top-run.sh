#!/usr/bin/env bash
# qubes-top-run.sh -- the app launcher (card t_2e8dda49, packaging).
#
# Launches the app BY MODULE: `python3 -m qubes_top "$@"` — never a
# module by file path. A file-path launch (`python3 /path/qubes_top/
# __main__.py`) bypasses the package entry contract (__main__'s arg
# handling and the deferred gi import are part of the module's contract,
# and a file-path launch changes which copy the interpreter loads).
# The wrapper resolves the tree (its parent), puts it on PYTHONPATH,
# and exec's the module — the app's own exit code is this script's.
#
# Read-only with respect to $HOME: it writes nothing. The one $HOME
# write the app itself makes while running is its paned-position file
# (~/.config/qubes-top/, card t_4d9516cd) — designed app behaviour.
#
# Usage:
#   packaging/qubes-top-run.sh            # the GUI
#   packaging/qubes-top-run.sh --probe    # the M2 probe
#   packaging/qubes-top-run.sh --stage    # the full staged test (card t_2e8dda49)
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -f "$ROOT/qubes_top/__init__.py" ]; then
    echo "qubes-top-run: tree not found at $ROOT (this script must sit in the tree's packaging/)" >&2
    exit 64
fi

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m qubes_top "$@"
