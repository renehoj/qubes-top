#!/usr/bin/env python3
"""qubes-top test runner (stdlib only).

Executes each tests/test_*.py as its own script (``python3 <file>``), so each
module's own ``sys.path`` shim (top of file) makes ``import qubes_top`` work
without requiring ``tests/`` to be an importable package. ``unittest
discover`` is deliberately not used: it refuses a start directory that is not
an importable package and reports it opaquely ("Start directory is not
importable"), naming the directory, not the missing file. Per-module script
execution is layout- and version-independent and survives a partial copy that
loses an empty-ish file.

Each module runs in a fresh interpreter, so a module's ``sys.modules``
assertions are not polluted by its siblings.

Exit code: 0 when every module passes, 1 otherwise.
"""
import pathlib
import subprocess
import sys

#: Per-module wall-clock cap. A module that wedges must fail the run, not
#: stall it: without this, a test whose expected behaviour depends on the
#: host's role (e.g. the off-box refusal branch, which on dom0 becomes a
#: real sampling run) hung the whole suite with no output.
MODULE_TIMEOUT_S = 300


def main() -> int:
    tests_dir = pathlib.Path(__file__).resolve().parent / "tests"
    modules = sorted(tests_dir.glob("test_*.py"))
    if not modules:
        print(f"no test modules found in {tests_dir}")
        return 1

    failed = []
    for mod in modules:
        print(f"=== {mod.name} ===", flush=True)
        try:
            result = subprocess.run([sys.executable, str(mod)],
                                    timeout=MODULE_TIMEOUT_S)
            rc = result.returncode
        except subprocess.TimeoutExpired:
            print(f"TIMEOUT after {MODULE_TIMEOUT_S}s - treating as a failure "
                  f"(a hung module must never stall the runner)", flush=True)
            rc = -1
        if rc != 0:
            failed.append(mod.name)

    print()
    if failed:
        print(f"FAILED: {len(failed)}/{len(modules)} modules: {', '.join(failed)}")
        return 1
    print(f"OK: all {len(modules)} test modules passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
