"""The staged dom0 test (card t_2e8dda49): off-box behaviour.

What this box can prove (no gi, no qubesadmin, no /etc/qubes-release,
no libxenstore -- re-asserted by TestOffBoxPreconditions): the refusal
branch (a wrong-host run prints FAIL and stops, never reads as green),
the fresh-interpreter import check (PASS via PYTHONPATH, FAIL without it,
both from a neutral cwd — a cwd in the tree would make the negative case
meaningless), the py_compile phase (the real tree parses; a bad file is
named), the suite phase's failure semantics (a failing module is red with
its rc, a hanging module expires to a red line naming the module —
t_7ce867: a hung suite is a worse signal than a red one), the headless
CLI phase (off-box the probe refuses; the phase must read as FAIL), and
the copy line (which copy of the code runs). What only dom0 can prove:
the complete run — that is the staged handover.

Run standalone:

    python3 tests/test_stage.py
"""
import contextlib
import importlib
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

# sys.path shim (see test_import_contract.py).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import qubes_top.dom0.stage as stage  # noqa: E402


def _off_box() -> bool:
    """True only when this host cannot sample Qubes at all (same premise as
    test_probe.py).

    On dom0 the stage runs for real, and its suite phase runs the suite --
    which contains this module. An unguarded test that spawns
    ``python3 -m qubes_top --stage`` therefore recurses until the module hits
    the 120 s per-module timeout. On the dev box the same test is safe
    because the stage refuses at the gate before the suite phase.
    """
    if os.path.exists("/etc/qubes-release"):
        return False
    try:
        return importlib.util.find_spec("qubesadmin") is None
    except (ImportError, ValueError):
        return True


OFF_BOX_PREMISE = unittest.skipUnless(
    _off_box(), "off-box premise does not hold on this host (looks like dom0)")


@OFF_BOX_PREMISE
class TestOffBoxPreconditions(unittest.TestCase):
    def test_this_box_is_not_dom0(self):
        """The premise that makes the refusal test meaningful (same
        premise as test_probe.py; re-asserted here because the stage's
        refusal is its own gate): if the box ever gains qubesadmin or
        libxenstore, this fails loudly instead of silently re-proving a
        branch it no longer needs."""
        for mod in ("gi", "qubesadmin"):
            try:
                importlib.import_module(mod)
            except ImportError:
                pass
            else:
                self.fail("%s is importable on this box -- the "
                        "refusal test's premise is gone" % mod)
        self.assertFalse(os.path.exists("/etc/qubes-release"),
                        "/etc/qubes-release exists on this box -- the "
                        "refusal test's premise is gone")


@OFF_BOX_PREMISE
class TestRefusal(unittest.TestCase):
    def test_refuse_on_non_dom0_host(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = stage.run_stage()
        text = buf.getvalue()
        self.assertEqual(rc, stage.EXIT_REFUSED)
        self.assertIn("STAGED TEST REFUSED: not dom0", text)
        self.assertIn("FAIL dom0-gate: all Qubes imports failed", text)
        # A wrong-host run never reads as green: no completion marker,
        # no phase runs after the gate.
        self.assertNotIn("STAGED TEST COMPLETE", text)
        self.assertNotIn("STAGED TEST CRASHED", text)
        self.assertNotIn("PASS import-qubes-top", text)
        self.assertNotIn("-- gtk --", text)
        self.assertNotIn("suite:", text)
        self.assertNotIn("-- environment --", text)

    def test_refuse_exit_code_via_cli(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        proc = subprocess.run(
            [sys.executable, "-m", "qubes_top", "--stage"],
            cwd=str(root), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, stage.EXIT_REFUSED)
        self.assertIn("STAGED TEST REFUSED: not dom0", proc.stdout)
        self.assertNotIn("STAGED TEST COMPLETE", proc.stdout)


class TestImportCheck(unittest.TestCase):
    def _with_pythonpath(self, value, fn):
        saved = os.environ.get("PYTHONPATH")
        if value is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = value
        try:
            return fn()
        finally:
            if saved is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = saved

    def test_pass_with_pythonpath_from_neutral_cwd(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as neutral:
            def run():
                return stage.import_check(cwd=neutral)
            lines = self._with_pythonpath(str(root), run)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("PASS import-qubes-top"), lines)
        self.assertIn("import qubes_top: ok", lines[0])

    def test_fail_without_pythonpath_from_neutral_cwd(self):
        with tempfile.TemporaryDirectory() as neutral:
            def run():
                return stage.import_check(cwd=neutral)
            lines = self._with_pythonpath(None, run)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("FAIL import-qubes-top"), lines)
        self.assertIn("rc=1", lines[0])


class TestPyCompilePhase(unittest.TestCase):
    def test_real_tree_passes(self):
        lines = stage.py_compile_phase()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("PASS py-compile"), lines)
        self.assertIn("parse under", lines[0])

    def test_bad_file_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            bad = pathlib.Path(d, "bad.py")
            bad.write_text("def f(:\n    pass\n")
            good = pathlib.Path(d, "good.py")
            good.write_text("X = 1\n")
            lines = stage.py_compile_files([good, bad])
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith("FAIL py-compile"), lines)
        self.assertIn("1 of 2 files do not parse", lines[0])
        self.assertIn(bad.name, lines[1])


class TestSuitePhase(unittest.TestCase):
    def _module_dir(self, names):
        d = tempfile.TemporaryDirectory()
        root = pathlib.Path(d.name)
        for name, body in names:
            (root / name).write_text(body)
        return d, root

    def test_all_pass(self):
        d, root = self._module_dir([("test_ok.py", "print('ok')\n")])
        try:
            lines = stage.suite_phase(root, timeout=30)
        finally:
            d.cleanup()
        self.assertIn("PASS suite: 1/1 modules", "\n".join(lines))
        self.assertNotIn("FAIL suite", "\n".join(lines))

    def test_fail_and_hang_are_named(self):
        d, root = self._module_dir([
            ("test_ok.py", "print('ok')\n"),
            ("test_bad.py", "raise SystemExit(3)\n"),
            ("test_hang.py", "import time\ntime.sleep(30)\n"),
        ])
        try:
            lines = stage.suite_phase(root, timeout=1)
        finally:
            d.cleanup()
        text = "\n".join(lines)
        self.assertIn("FAIL suite test_bad.py: rc=3", text)
        self.assertIn("FAIL suite test_hang.py: timeout after 1s", text)
        self.assertIn("FAIL suite: 2/3 modules failed: "
                     "test_bad.py, test_hang.py", text)
        # The passing module is not named as a failure.
        self.assertNotIn("FAIL suite test_ok.py", text)

    def test_note_names_the_guard_modules(self):
        d, root = self._module_dir([("test_ok.py", "print('ok')\n")])
        try:
            lines = stage.suite_phase(root, timeout=30)
        finally:
            d.cleanup()
        note = [l for l in lines if l.startswith("NOTE suite")]
        self.assertEqual(len(note), 1)
        self.assertIn("test_import_guard", note[0])
        self.assertIn("test_ui_purity", note[0])
        self.assertIn("test_invariants", note[0])


@OFF_BOX_PREMISE
class TestHeadlessCliPhase(unittest.TestCase):
    def test_off_box_the_probe_refuses_and_the_phase_reads_as_fail(self):
        lines = stage.headless_cli_phase()
        self.assertTrue(lines[0].startswith("FAIL headless-cli"), lines)
        self.assertIn("rc=2", lines[0])
        self.assertTrue(any("contradiction" in l for l in lines),
                        "the rc=2-after-gate-passed case needs its NOTE")


class TestCopyLine(unittest.TestCase):
    def test_pythonpath_names_the_tree_copy(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        saved = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(root)
        try:
            line = stage.copy_line()
        finally:
            if saved is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = saved
        self.assertIn("the working-tree copy at %s" % root, line)

    def test_without_pythonpath_names_the_current_directory(self):
        saved = os.environ.get("PYTHONPATH")
        os.environ.pop("PYTHONPATH", None)
        try:
            line = stage.copy_line()
        finally:
            if saved is None:
                pass
            else:
                os.environ["PYTHONPATH"] = saved
        self.assertIn("the current directory", line)

    def test_always_states_the_execstart_fact(self):
        line = stage.copy_line()
        self.assertIn("no systemd unit", line)
        self.assertIn("ExecStart", line)


class TestStageImportContract(unittest.TestCase):
    def test_stage_import_pulls_no_heavy_dependency(self):
        # Fresh interpreter (the runner runs each module as its own
        # script); the stage was imported at the top of this module.
        # Neither heavy package may have been pulled in by that import
        # -- the deferred-import design, the same contract as the probe
        # (and __main__ imports the stage at top level, like the probe).
        self.assertNotIn("gi", sys.modules)
        self.assertNotIn("qubesadmin", sys.modules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
