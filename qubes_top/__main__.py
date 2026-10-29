"""Command-line entry: ``python3 -m qubes_top`` (the GUI, card t_4d9516cd),
``python3 -m qubes_top --probe`` (the dom0 probe, card t_ae308ca8) and
``python3 -m qubes_top --stage`` (the full staged dom0 test, card
t_2e8dda49 — probe superset: imports, both GTK namespaces, is_composited,
py_compile, the stdlib suite including the guards, the headless CLI).

The package ``__init__.py`` stays inert (the import contract, enforced by
tests/test_import_guard.py). The GUI modules import ``gi``; they are imported
**deferred** inside the GUI branch so that ``import qubes_top`` (and the probe
and stage paths) never pull gi on a gi-less box. On a gi-less box the GUI
branch fails **loudly and cleanly** (an exit code and a message, not a
traceback from deep inside the stack).

Exit codes: 0 probe completed / stage completed / GUI exited cleanly, 2
refused (no dom0 for the probe or the stage) or no GTK/display for the GUI,
1 probe or stage crashed (raw traceback printed), 64 usage.
"""
import sys

from qubes_top.dom0.probe import run_probe
from qubes_top.dom0.stage import run_stage

USAGE = ("usage: python3 -m qubes_top "
         "[--probe | --stage | --api-audit | (no args for the GUI) | --gui]")


def _api_audit() -> int:
    """Check every gi symbol and widget method the UI calls, on this host."""
    from pathlib import Path

    from qubes_top.ui.api_audit import audit_dir

    status, problems = audit_dir(Path(__file__).resolve().with_name("ui"))
    if status == "SKIP":
        print("api-audit: SKIP -- no 'gi' on this interpreter (run it on dom0)")
        return 0
    print("api-audit: %s (%d problem(s))" % (status, len(problems)))
    for label, lineno, message in problems:
        print("  FAIL %s:%d: %s" % (label, lineno, message))
    return 0 if status == "PASS" else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--probe"]:
        return run_probe()
    if argv == ["--stage"]:
        return run_stage()
    if argv == ["--api-audit"]:
        return _api_audit()
    if argv == [] or argv == ["--gui"]:
        # Deferred import: the GUI modules pull gi; a gi-less box must fail
        # here (a clean message + exit code), not with a ModuleNotFoundError
        # traceback from deep inside the stack.
        try:
            from qubes_top.ui.app import run as _run
        except ModuleNotFoundError:
            sys.stderr.write(
                "qubes-top: the GUI requires GTK3 (PyGObject); this interpreter "
                "has no 'gi'. The probe (--probe) does not need GTK3.\n")
            return 2
        return _run()
    print(USAGE, file=sys.stderr)
    return 64  # EX_USAGE: a mistyped invocation is not a refusal


if __name__ == "__main__":
    sys.exit(main())
