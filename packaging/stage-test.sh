#!/usr/bin/env bash
# stage-test.sh -- the staged dom0 test for qubes-top (card t_2e8dda49).
#
# The owner's single command on the target: runs the full staged test
# (python3 -m qubes_top --stage — imports, both GTK namespaces,
# is_composited, py_compile, the stdlib suite including the guards, the
# headless CLI against real qubes), and ONLY when the fail count is zero
# launches the GUI in the foreground for the human. A run on the wrong
# host prints FAIL and refuses (exit 2); a failed staged test exits
# non-zero and does NOT launch the app.
#
# Which copy of the code runs: the working-tree copy at $TREE (the
# fresh extract the owner copied in), via PYTHONPATH — never a qubes-top
# entry point on PATH, never a unit ExecStart (there is no systemd unit,
# DESIGN.md D-8). The stage prints the same NOTE line.
#
# Usage:
#   bash packaging/stage-test.sh [TREE_DIR]
# TREE_DIR defaults to this script's parent (the repo root).
#
# Read-only with respect to $HOME: the test runs in a mktemp -d scratch
# directory (which also proves the test is cwd-independent), removed on
# every exit path. The test itself writes no files (py_compile runs
# in-process; the suite's temp files are the tests' own temp dirs). The
# one $HOME write the staged run can cause is the GUI's own paned-position
# file (~/.config/qubes-top/) — the app's designed behaviour (card t_4d9516cd),
# not the script's.
#
# Exit codes:
#   0   staged test passed and the GUI was closed by the human (or, if
#       the GUI never launched, the test completed clean)
#   2   REFUSED -- this host is not dom0 (all Qubes imports failed)
#   1   staged test completed with FAIL lines (fail count in the marker),
#       or the GUI / the test crashed (raw traceback printed)
#   64  usage / tree not found
set -u

TREE="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
if [ ! -f "$TREE/qubes_top/__init__.py" ]; then
    echo "stage-test: tree not found at $TREE (pass the repo root as the first argument)" >&2
    exit 64
fi

# mktemp -d scratch as the cwd; removed on every exit path.
SCRATCH="$(mktemp -d)" || {
    echo "stage-test: mktemp -d failed" >&2
    exit 1
}
trap 'rm -rf "$SCRATCH"' EXIT

# stdout + stderr captured AND streamed: the earlier version redirected to a
# file and only printed at the end, so a slow-but-healthy run looked exactly
# like a hang. ``tee`` keeps the pasted-back text identical to what ran while
# showing progress; PIPESTATUS keeps the probe's exit code (not tee's).
echo "stage-test: running the staged dom0 test (streaming; this can take a minute)"
# ``-u``: unbuffered. Piped stdout is block-buffered, so a hard crash (SIGSEGV,
# rc 139) discards every line still in the buffer and the run looks silent.
PYTHONUNBUFFERED=1 PYTHONPATH="$TREE" python3 -u -m qubes_top --stage 2>&1 | tee "$SCRATCH/stage.out"
rc=${PIPESTATUS[0]}

case "$rc" in
    2)
        echo "stage-test: REFUSED -- this host is not dom0 (see the lines above)"
        exit 2
        ;;
    0)
        echo "stage-test: all checks passed -- launching the GUI in the foreground"
        echo "stage-test: the window is yours; close it to end the staged test"
        exec env PYTHONPATH="$TREE" python3 -m qubes_top --gui
        ;;
    *)
        echo "stage-test: staged test failed (rc=$rc) -- the GUI will not launch"
        exit 1
        ;;
esac
