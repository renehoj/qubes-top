"""The staged dom0 probe (card t_ae308ca8): off-box behaviour.

What this box can prove (no gi, no qubesadmin, no /etc/qubes-release,
no libxenstore -- re-asserted by TestOffBoxPreconditions): the refusal
branch -- a run on the wrong host prints FAIL and stops, and never
reads as green. What only dom0 can prove: the complete run (the real
adapters, the 5 s window, the GTK pin, the M2 name check) -- that is
the staged handover; this module pins the guard, not the target.

Run standalone:

    python3 tests/test_probe.py
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

import qubes_top.dom0.probe as probe  # noqa: E402
from qubes_top.core import SampleRecord  # noqa: E402


def _off_box() -> bool:
    """True only when this host cannot sample Qubes at all.

    The refusal branch is an *off-box* property: on dom0 both Qubes imports
    succeed, so the CLI legitimately samples instead of refusing. Tests that
    assert the refusal must state this precondition rather than assume it --
    running them on the target turned a one-second refusal into a real
    two-tick sampling run with no timeout above it, which wedged the suite.
    """
    if os.path.exists("/etc/qubes-release"):
        return False
    try:
        return importlib.util.find_spec("qubesadmin") is None
    except (ImportError, ValueError):
        return True


#: Skip condition for every off-box-only assertion in this module.
OFF_BOX_PREMISE = unittest.skipUnless(
    _off_box(), "off-box premise does not hold on this host (looks like dom0)")


@OFF_BOX_PREMISE
class TestOffBoxPreconditions(unittest.TestCase):
    def test_this_box_is_not_dom0(self):
        """The premise that makes the refusal test meaningful: on this
        box both Qubes import paths fail and the release file is
        absent. If the box ever gains qubesadmin, this fails loudly
        instead of silently re-proving a branch it no longer needs."""
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


class TestRefusal(unittest.TestCase):
    @OFF_BOX_PREMISE
    def test_refuse_on_non_dom0_host(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = probe.run_probe()
        text = buf.getvalue()
        self.assertEqual(rc, probe.EXIT_REFUSED)
        self.assertIn("PROBE REFUSED: not dom0", text)
        self.assertIn("FAIL dom0-gate: all Qubes imports failed", text)
        self.assertIn("FAIL qubesadmin-import: ModuleNotFoundError", text)
        self.assertIn("FAIL libxenstore-load: no candidate loaded", text)
        # A wrong-host run never reads as green: no completion marker,
        # no qube rows, no environment block -- it stops at the gate.
        self.assertNotIn("PROBE COMPLETE", text)
        self.assertNotIn("PROBE CRASHED", text)
        self.assertNotIn("qube rows:", text)
        self.assertNotIn("-- environment --", text)

    @OFF_BOX_PREMISE
    def test_refuse_exit_code_via_cli(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        proc = subprocess.run(
            [sys.executable, "-m", "qubes_top", "--probe"],
            cwd=str(root), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, probe.EXIT_REFUSED)
        self.assertIn("PROBE REFUSED: not dom0", proc.stdout)
        self.assertNotIn("PROBE COMPLETE", proc.stdout)

    def test_usage_exit_code_via_cli(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        # A *mistyped* invocation (a bad arg) is a usage error (64), distinct
        # from a refusal (2). A bare `python3 -m qubes_top` now launches the GUI
        # (card t_4d9516cd), so the usage trigger is a genuinely mistyped arg.
        proc = subprocess.run(
            [sys.executable, "-m", "qubes_top", "--bogus"],
            cwd=str(root), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 64)
        self.assertIn("usage", proc.stderr)


class TestGatePredicate(unittest.TestCase):
    def test_refuse_only_when_all_qubes_imports_fail(self):
        self.assertTrue(probe.gate_refuse(False, False))
        self.assertFalse(probe.gate_refuse(True, False))
        self.assertFalse(probe.gate_refuse(False, True))
        self.assertFalse(probe.gate_refuse(True, True))


class TestMeminfoChanged(unittest.TestCase):
    def test_none_safe(self):
        self.assertFalse(probe.meminfo_changed(None, None))
        self.assertFalse(probe.meminfo_changed(123, 123))
        self.assertTrue(probe.meminfo_changed(123, 124))
        self.assertTrue(probe.meminfo_changed(None, 5))
        self.assertTrue(probe.meminfo_changed(5, None))


class TestNameFreeCheck(unittest.TestCase):
    def test_clean_tools_dir_is_pass(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("__init__.py", "qvm_ls.py", "qvm_run.py"):
                pathlib.Path(d, name).write_text("x")
            lines = probe.name_free_check(d)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("PASS name-free"))

    def test_collision_is_fail_and_names_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "qubes_top.py").write_text("x")
            lines = probe.name_free_check(d)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("FAIL name-free"))
        self.assertIn("qubes_top.py", lines[0])

    def test_missing_tools_dir_is_fail(self):
        lines = probe.name_free_check("/nonexistent/qubes-top/tools")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("FAIL name-free"))


class TestQubeRowFormat(unittest.TestCase):
    def _rec(self, **kw):
        base = dict(name="work", domid=2, state="Running", cpu_time_ns=0,
                    mem_assigned_kib=2097152, mem_max_kib=3145728,
                    mem_used_kib=1048576, swap_used_kib=None, ts=0.0)
        base.update(kw)
        return SampleRecord(**base)

    def test_column_order_and_alignment(self):
        line = probe.format_qube_row(self._rec(), "reported", "no")
        self.assertEqual(line.split(),
                         ["work", "2", "Running", "2097152", "3145728",
                          "1048576", "-", "reported", "no"])
        # Alignment pinned to the card's column widths: the field
        # starts after the sum of the preceding column widths.
        self.assertEqual(line.index("2097152"), 20 + 6 + 8)
        self.assertEqual(line.index("3145728"), 20 + 6 + 8 + 9)
        self.assertEqual(line.index("1048576"), 20 + 6 + 8 + 9 + 9)

    def test_unresolved_domid_and_none_cells(self):
        line = probe.format_qube_row(self._rec(domid=-1,
                                              mem_used_kib=None),
                                     "mute", None)
        self.assertEqual(line.split(),
                         ["work", "-1", "Running", "2097152", "3145728",
                          "-", "-", "mute", "-"])

    def test_header_matches_columns(self):
        self.assertEqual(probe.row_header().split(),
                         [name for name, _ in probe.ROW_COLUMNS])


class TestProbeImportContract(unittest.TestCase):
    def test_probe_import_pulls_no_heavy_dependency(self):
        # Fresh interpreter (the runner runs each module as its own
        # script); the probe was imported at the top of this module.
        # Neither heavy package may have been pulled in by that import
        # -- the deferred-import design, the same contract as the
        # dom0 adapters.
        self.assertNotIn("gi", sys.modules)
        self.assertNotIn("qubesadmin", sys.modules)


@OFF_BOX_PREMISE
class TestOffBoxDegradation(unittest.TestCase):
    def test_qubesadmin_import_check_reports_the_raw_exception(self):
        ok, raw = probe.qubesadmin_import_check()
        self.assertFalse(ok)
        self.assertIn("ModuleNotFoundError", raw)

    def test_libxenstore_check_no_candidate(self):
        ok, raw = probe.libxenstore_check()
        self.assertFalse(ok)
        self.assertIn("no candidate loaded", raw)

    def test_gtk_pin_gi_absent_is_a_single_fail_line(self):
        lines, gdk = probe.gtk_check()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("FAIL gtk-pin"))
        self.assertIn("gi import failed", lines[0])
        self.assertIsNone(gdk)

    def test_composited_degrades_to_note_when_gtk_absent(self):
        line = probe.composited_line(None)
        self.assertTrue(line.startswith("NOTE composited"))


class TestLabelColorsCheck(unittest.TestCase):
    """``label_colors_check`` (D-20): the pre-GUI proof that the row dots can
    read real Qubes labels on this target.

    The API shape it exercises (``vm.label`` -> a Label object, ``.color`` ->
    ``#rrggbb``) is read from ``release4.3`` source and has never run on the
    target, so the check's own reporting is what is pinned here: a readable
    domain is a PASS row carrying the colour, an unreadable one is a FAIL
    carrying the raw exception, and a partial read can never print a PASS
    summary. Nothing is silently hashed.
    """

    class _Label:
        def __init__(self, name, colour):
            self.name = name
            self._colour = colour

        @property
        def color(self):
            if isinstance(self._colour, Exception):
                raise self._colour
            return self._colour

    def _vm(self, name, label):
        class VM:
            def __init__(self, name, label):
                self.name = name
                self.label = label
        return VM(name, label)

    def _adapter(self, domains):
        class App:
            def __init__(self, domains):
                self.domains = domains

        class Adapter:
            def __init__(self, app):
                self.app = app

        return Adapter(App(domains))

    def test_every_readable_domain_is_a_pass(self):
        # The two default labels are answered by the Nord map (D-22), so the
        # colour column shows "-" — no RPC was needed — and the dot column
        # shows the tone the row will actually paint.
        lines = probe.label_colors_check(self._adapter([
            self._vm("work", self._Label("purple", "0x75507b")),
            self._vm("sys-net", self._Label("red", "0xcc0000")),
        ]))
        self.assertIn("PASS labels: 2/2 domains resolve to a Qubes label colour",
                      lines)
        body = "\n".join(lines)
        self.assertIn("work", body)
        self.assertIn("purple", body)
        self.assertIn("dot #b48ead", body)      # nord15
        self.assertIn("dot #bf616a", body)      # nord11

    def test_a_mapped_label_needs_no_colour_read(self):
        # mirrors the app: a default label must pass even when the colour RPC
        # would fail, because the dot never asks qubesd for it
        lines = probe.label_colors_check(self._adapter([
            self._vm("work", self._Label("yellow", RuntimeError("refused"))),
        ]))
        joined = "\n".join(lines)
        self.assertIn("PASS labels: 1/1", joined)
        self.assertIn("dot #ebcb8b", joined)    # nord13

    def test_a_custom_label_shows_its_raw_colour_and_the_dot(self):
        lines = probe.label_colors_check(self._adapter([
            self._vm("work", self._Label("teal", "0x008080")),
        ]))
        joined = "\n".join(lines)
        self.assertIn("PASS labels: 1/1", joined)
        self.assertIn("0x008080", joined)
        self.assertIn("dot #008080", joined)

    def test_an_unreadable_label_is_a_fail_with_its_exception(self):
        # a CUSTOM label must read its colour, so a refusal is a real failure
        lines = probe.label_colors_check(self._adapter([
            self._vm("good", self._Label("green", "0x73d216")),
            self._vm("bad", self._Label("teal", RuntimeError("label.Get refused"))),
        ]))
        joined = "\n".join(lines)
        self.assertIn("FAIL label bad: RuntimeError: label.Get refused", joined)
        self.assertIn("FAIL labels: 1/2 domains", joined)

    def test_a_missing_label_is_a_fail_not_a_hash(self):
        lines = probe.label_colors_check(
            self._adapter([self._vm("work", None)]))
        joined = "\n".join(lines)
        self.assertIn("FAIL label work: label is None", joined)
        self.assertIn("FAIL labels: 0/1", joined)

    def test_a_non_colour_value_is_a_fail(self):
        # the label NAME is not a colour: a client that returned the name
        # where the colour belongs must be a FAIL for a custom label
        lines = probe.label_colors_check(
            self._adapter([self._vm("work", self._Label("teal", "yellow"))]))
        joined = "\n".join(lines)
        self.assertIn("is neither 0xrrggbb nor #rrggbb", joined)
        self.assertIn("FAIL labels: 0/1", joined)

    def test_an_empty_domain_list_is_a_note(self):
        lines = probe.label_colors_check(self._adapter([]))
        self.assertTrue(any(ln.startswith("NOTE labels:") for ln in lines))

    def test_an_unavailable_app_does_not_raise(self):
        class Adapter:
            @property
            def app(self):
                raise RuntimeError("qubesd gone (test)")
        lines = probe.label_colors_check(Adapter())
        joined = "\n".join(lines)
        self.assertIn("FAIL labels: app.domains raised", joined)
        self.assertIn("RuntimeError: qubesd gone (test)", joined)

    def test_the_check_is_in_the_probe_run(self):
        # A check that exists but is never called proves nothing: pin that
        # run_probe() invokes it (AST — the run itself needs dom0).
        import ast
        src = pathlib.Path(probe.__file__).read_text()
        calls = [n.func.id for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)]
        self.assertIn("label_colors_check", calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
