#!/usr/bin/env bash
# dom0-probe.sh -- the staged dom0 probe for qubes-top (card t_ae308ca8).
#
# The first thing the owner runs on the target, before the UI is built:
# it executes the real adapters + sampler (python3 -m qubes_top --probe)
# and reports PASS/FAIL/NOTE lines plus a completion marker. A run on
# the wrong host prints FAIL and refuses (exit 2) -- it must never read
# as green. The check itself lives in the python probe (one canonical
# implementation); this wrapper resolves the tree, provides the scratch
# directory, and labels the exit code.
#
# Usage:
#   bash packaging/dom0-probe.sh [TREE_DIR]
# TREE_DIR defaults to this script's parent (the repo root).
#
# Read-only with respect to $HOME: the probe runs in a mktemp -d
# scratch directory (which also proves the probe is cwd-independent),
# removed on every exit path. Nothing is written to $HOME or the tree.
#
# Exit codes:
#   0   probe completed (read the PASS/FAIL lines; the fail count is in
#       the completion marker)
#   2   REFUSED -- this host is not dom0 (all Qubes imports failed)
#   1   probe crashed (raw traceback printed)
#   64  usage / tree not found
set -u

TREE="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
if [ ! -f "$TREE/qubes_top/__init__.py" ]; then
    echo "dom0-probe: tree not found at $TREE (pass the repo root as the first argument)" >&2
    exit 64
fi

# mktemp -d scratch as the cwd; removed on every exit path.
SCRATCH="$(mktemp -d)" || {
    echo "dom0-probe: mktemp -d failed" >&2
    exit 1
}
trap 'rm -rf "$SCRATCH"' EXIT

# stdout + stderr captured in one place so the pasted-back text is
# exactly what ran: raw output and raw tracebacks, nothing summarised.
PYTHONPATH="$TREE" python3 -m qubes_top --probe > "$SCRATCH/probe.out" 2>&1
rc=$?
cat "$SCRATCH/probe.out"

case "$rc" in
    2) echo "dom0-probe: REFUSED -- this host is not dom0 (see the lines above)" ;;
    0) ;;
    *) echo "dom0-probe: probe exited with code $rc" ;;
esac
exit "$rc"
