"""Staged dom0 test (card t_2e8dda49): extend the M2 probe into the full
M3 gate — the one tier that can prove what no local run can.

Runs on the target's own interpreter (the wrapper resolves the tree, sets
``PYTHONPATH`` to it, and runs ``python3 -m qubes_top --stage``). One
PASS/FAIL/NOTE line per check, then a completion marker carrying the fail
count; a run on the wrong host prints FAIL and refuses (exit 2) — it must
never read as green.

Phase order (the card's list):

1. **which copy** — a NOTE naming the copy of the code this stage runs
   (working tree on ``PYTHONPATH`` — never a PATH entry point, never a
   unit ``ExecStart``; there is no systemd unit, DESIGN.md D-8);
2. **imports** — ``import qubes_top`` in a fresh interpreter
   (``sys.executable -c``), same ``PYTHONPATH``;
3. **both GTK namespaces + is_composited()** — the probe's ``gtk_check``
   (``gi.require_version`` on Gtk 3.0 then Gdk 3.0, ``gi.repository``
   import) and ``composited_line`` (reused, not re-implemented);
4. **py_compile** — every ``.py`` in the tree parsed by the target
   interpreter; in-process ``compile()`` (the same syntax check as
   ``py_compile``) so the stage writes no ``.pyc`` into the fresh
   extract;
5. **the stdlib suite, including the guards** — each ``tests/test_*.py``
   in a fresh interpreter under an explicit timeout; a hung module
   expires to a red line naming the module, not "the staged test never
   finished" (t_7ce867 finding). The guard modules (``test_import_guard``,
   ``test_ui_purity``, ``test_invariants``) run inside the suite — they
   are the guards, so one run covers both, and the NOTE line names them;
6. **the headless CLI against real qubes** — ``python3 -m qubes_top
   --probe`` as a subprocess (the entry point the owner's wrapper uses;
   the probe itself runs two real Sampler ticks 5 s apart);
7. the environment block (reused from the probe: qubes-release, python
   version, qubesadmin version, ``command -v qubes-top``, the name-free
   gate check).

After the marker, the wrapper (``packaging/stage-test.sh``) launches the
GUI in the foreground for the human — only when the fail count is zero;
a failed staged test never launches the app.

Exit codes: 0 completed (some checks may be FAIL — the marker carries
the count), 2 refused (not dom0), 1 crashed (raw traceback printed;
paraphrasing loses the line that matters).
"""
import os
import pathlib
import subprocess
import sys
import traceback

from qubes_top.dom0.probe import (
    EXIT_OK, EXIT_CRASHED, EXIT_REFUSED,
    gate_refuse, qubesadmin_import_check, libxenstore_check,
    qubes_release_check, python_version_line, qubesadmin_version_line,
    gtk_check, composited_line, command_v_line, name_free_check,
)

__all__ = [
    "EXIT_OK", "EXIT_CRASHED", "EXIT_REFUSED",
    "TEST_TIMEOUT_S", "CLI_TIMEOUT_S", "IMPORT_TIMEOUT_S",
    "import_check", "py_compile_files", "py_compile_phase",
    "suite_phase", "headless_cli_phase", "copy_line", "run_stage",
]

#: Per-module timeout for the suite phase (card t_7ce867: a hung module
#: must expire to a red line, not stall the whole staged run).
TEST_TIMEOUT_S = 120
#: The headless CLI (the --probe) phase: the 5 s meminfo window + adapter
#: time, with margin.
CLI_TIMEOUT_S = 60
#: The fresh-interpreter import check.
IMPORT_TIMEOUT_S = 30


def _tree_root() -> pathlib.Path:
    """The tree root this stage runs from (stage.py lives two levels
    down: ``qubes_top/dom0/stage.py``)."""
    return pathlib.Path(__file__).resolve().parents[2]


def import_check(cwd=None) -> list:
    """``import qubes_top`` in a fresh interpreter — the target's own
    (``sys.executable``), inheriting the ``PYTHONPATH`` the wrapper set
    to the tree. ``cwd`` defaults to the stage's own cwd (the wrapper's
    scratch dir — deliberately *not* the tree: the import must work via
    PYTHONPATH, not because the cwd happens to be the tree). Returns the
    PASS/FAIL lines."""
    code = ("import qubes_top, sys; "
            "print('import qubes_top: ok (interpreter %s)' "
            "% sys.version.split()[0])")
    try:
        proc = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True,
                             timeout=IMPORT_TIMEOUT_S, cwd=cwd)
    except subprocess.TimeoutExpired:
        return ["FAIL import-qubes-top: fresh-interpreter import timed out "
                "after %ds" % IMPORT_TIMEOUT_S]
    if proc.returncode == 0:
        out = (proc.stdout or "").strip()
        return ["PASS import-qubes-top: %s" % (out or "ok")]
    return ["FAIL import-qubes-top: rc=%d; stderr: %s"
            % (proc.returncode, (proc.stderr or "").strip() or "(empty)")]


def py_compile_files(files) -> list:
    """Parse every file with the target interpreter's ``compile()`` — the
    same syntax check ``py_compile`` performs, without writing ``.pyc``
    into the fresh extract. Returns the PASS/FAIL lines. ``files`` is a
    list of paths (absolute or as labelled); names are reported relative
    to the common root when they share one."""
    bad = []
    for p in files:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                compile(fh.read(), str(p), "exec")
        except SyntaxError as exc:
            bad.append("%s: %s" % (p, exc))
    if bad:
        return ["FAIL py-compile: %d of %d files do not parse under %s:"
                % (len(bad), len(files), sys.version.split()[0])] \
            + ["  " + line for line in bad]
    return ["PASS py-compile: all %d files parse under %s (in-process "
            "compile; no .pyc written to the extract)"
            % (len(files), sys.version.split()[0])]


def py_compile_phase() -> list:
    """py_compile over the whole tree (every ``.py`` under the root)."""
    root = _tree_root()
    files = sorted(root.rglob("*.py"))
    if not files:
        return ["FAIL py-compile: no .py files found under %s" % root]
    return py_compile_files(files)


def suite_phase(tests_dir=None, timeout: int = TEST_TIMEOUT_S) -> list:
    """The stdlib suite — each ``tests/test_*.py`` in a fresh
    interpreter under an explicit timeout. A hung module expires to a
    FAIL line naming the module (t_7ce867: a hung suite is a worse
    signal than a red one, especially unattended on dom0). The guard
    modules run inside the suite — they ARE the guards; the NOTE line
    names them so the handover can see one run covered both."""
    if tests_dir is None:
        tests_dir = _tree_root() / "tests"
    tests_dir = pathlib.Path(tests_dir)
    tests = sorted(tests_dir.glob("test_*.py"))
    lines = ["suite: %d test modules — fresh interpreter each, timeout %ds; "
             "a hang expires to a red line naming the module"
             % (len(tests), timeout)]
    if not tests:
        lines.append("FAIL suite: no test modules found in %s" % tests_dir)
        return lines
    failed = []
    for mod in tests:
        timed_out = False
        rc = None
        err = ""
        try:
            proc = subprocess.run([sys.executable, str(mod)],
                                 capture_output=True, text=True,
                                 timeout=timeout)
            rc = proc.returncode
            err = (proc.stderr or "").strip()
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            raw = exc.stderr
            if isinstance(raw, bytes):
                err = raw.decode("utf-8", "replace").strip()
        if timed_out:
            failed.append(mod.name)
            lines.append("FAIL suite %s: timeout after %ds (a hung module "
                        "is a failure, not a stall)" % (mod.name, timeout))
        elif rc != 0:
            failed.append(mod.name)
            lines.append("FAIL suite %s: rc=%d" % (mod.name, rc))
            for line in err.splitlines()[-5:]:
                lines.append("  " + line)
    if failed:
        lines.append("FAIL suite: %d/%d modules failed: %s"
                     % (len(failed), len(tests), ", ".join(failed)))
    else:
        lines.append("PASS suite: %d/%d modules" % (len(tests), len(tests)))
    lines.append("NOTE suite: the guard modules in this run: "
                 "test_import_guard, test_ui_purity, test_invariants "
                 "(one run covered the guards and the suite)")
    return lines


def headless_cli_phase() -> list:
    """The headless CLI against real qubes: ``python3 -m qubes_top
    --probe`` as a subprocess under the same interpreter and
    ``PYTHONPATH`` (the entry point the owner's wrapper uses — not a
    re-implementation; the probe itself runs two real Sampler ticks 5 s
    apart). rc 2 is the probe's refusal: after the gate above it passed,
    so a refusal here is a contradiction the NOTE calls out."""
    try:
        proc = subprocess.run([sys.executable, "-m", "qubes_top", "--probe"],
                             capture_output=True, text=True,
                             timeout=CLI_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return ["FAIL headless-cli: python3 -m qubes_top --probe timed out "
                "after %ds" % CLI_TIMEOUT_S]
    marker = ""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("PROBE"):
            marker = line
    if proc.returncode == 0:
        return ["PASS headless-cli: python3 -m qubes_top --probe completed: "
                "%s" % (marker or "no marker line")]
    lines = ["FAIL headless-cli: python3 -m qubes_top --probe rc=%d: %s"
             % (proc.returncode, marker or "no marker line")]
    for line in (proc.stdout or "").splitlines()[-5:]:
        lines.append("  " + line)
    if proc.returncode == 2:
        lines.append("  NOTE: rc=2 is the probe's refusal — the gate above "
                     "passed, so a refusal here is a contradiction to "
                     "investigate")
    return lines


def copy_line() -> str:
    """Which copy of the code this stage runs (card t_2e8dda49
    acceptance): the working-tree copy the wrapper put on ``PYTHONPATH``
    — never a PATH entry point (there is no ``qubes-top`` on PATH; the
    ``command -v`` line reports it) and never a unit ``ExecStart`` (no
    systemd unit, DESIGN.md D-8). Those are three different copies; this
    stage runs the first."""
    pp = os.environ.get("PYTHONPATH", "")
    first = pp.split(os.pathsep)[0] if pp else ""
    where = ("the working-tree copy at %s" % first
             if first else "the current directory")
    return ("NOTE copy: this stage runs %s (python3 -m qubes_top via "
            "PYTHONPATH); no qubes-top entry point on PATH is used, and "
            "there is no systemd unit, so no ExecStart copy exists "
            "(DESIGN.md D-8)" % where)


def api_audit_phase() -> list:
    """Every gi symbol and widget method the UI calls, resolved against the
    real Gtk/Pango/Gdk on this host.

    The GUI can only fail here, never off-box: two invented GTK APIs
    (StyleContext.get_default, Label.get_pango_attributes) each cost a full
    staged run. This phase turns "one crash per attempt" into one report.
    """
    try:
        from pathlib import Path
        from qubes_top.ui.api_audit import audit_dir
    except Exception as exc:  # pragma: no cover - defensive
        return ["NOTE api-audit: not importable (%s)" % exc]
    ui_dir = Path(__file__).resolve().parent.parent / "ui"
    status, problems = audit_dir(ui_dir)
    if status == "SKIP":
        return ["NOTE api-audit: skipped -- no gi on this interpreter"]
    if status == "PASS":
        return ["PASS api-audit: every gi symbol and widget method resolves"]
    return ["FAIL api-audit %s:%d: %s" % (label, lineno, message)
            for label, lineno, message in problems]


def run_stage() -> int:
    """Run the whole staged test, printing to stdout. Returns the exit
    code (0 completed / 2 refused / 1 crashed). The fail count in the
    completion marker is the number of FAIL lines printed."""
    fails = 0

    def say(line: str) -> None:
        nonlocal fails
        print(line)
        if line.startswith("FAIL"):
            fails += 1

    try:
        say("== qubes-top staged test (card t_2e8dda49) ==")
        say(copy_line())

        qa = qubesadmin_import_check()
        xs = libxenstore_check()
        if gate_refuse(qa[0], xs[0]):
            say("FAIL qubesadmin-import: %s" % qa[1])
            say("FAIL libxenstore-load: %s" % xs[1])
            say("FAIL dom0-gate: all Qubes imports failed -- this is not "
                "dom0")
            say("STAGED TEST REFUSED: not dom0 (stop; a run on the wrong "
                "host must not read as green)")
            return EXIT_REFUSED
        say(("PASS" if qa[0] else "FAIL") + " qubesadmin-import: %s" % qa[1])
        say(("PASS" if xs[0] else "FAIL") + " libxenstore-load: %s" % xs[1])
        say("PASS dom0-gate: at least one Qubes import path works")

        for line in import_check():
            say(line)

        say("-- gtk --")
        gtk_lines, gdk = gtk_check()
        for line in gtk_lines:
            say(line)
        say(composited_line(gdk))

        for line in api_audit_phase():
            say(line)

        for line in py_compile_phase():
            say(line)

        for line in suite_phase():
            say(line)

        for line in headless_cli_phase():
            say(line)

        say("-- environment --")
        for line in qubes_release_check():
            say(line)
        say(python_version_line())
        say(qubesadmin_version_line(qa))
        say(command_v_line())
        for line in name_free_check():
            say(line)

        say("STAGED TEST COMPLETE (fails=%d)" % fails)
        # Non-zero whenever anything failed: the owner's wrapper launches the
        # GUI only on rc 0. Returning EXIT_OK regardless of `fails` made it
        # print "all checks passed" and open the app on a 5-failure run. The
        # value shares EXIT_CRASHED deliberately -- the wrapper only needs
        # "non-zero and not a refusal"; the COMPLETE/CRASHED marker says which.
        return EXIT_OK if fails == 0 else EXIT_CRASHED
    except Exception:
        say("FAIL stage-crash: raw traceback follows (paraphrasing loses "
            "the line that matters)")
        print(traceback.format_exc())
        say("STAGED TEST CRASHED (fails=%d)" % fails)
        return EXIT_CRASHED
