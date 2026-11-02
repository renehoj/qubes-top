"""The audit must catch the exact APIs that broke the target, and must not
flag the corrected forms.

Both bugs it exists to catch were invisible off-box: ``gi`` cannot be
imported here, so no test could call them. The audit is therefore split so
its *logic* — AST walk + attribute existence — runs against a module-shaped
stub, which is both testable and narrow: the stub offers only what GTK3
really has.
"""
import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.ui.api_audit import audit_cairo, audit_sources  # noqa: E402


class FakeLabel:
    """Only the methods a real Gtk.Label has (no get_pango_attributes)."""

    def set_text(self, *args):
        pass

    def get_text(self):
        return ""

    def set_attributes(self, *args):
        pass

    def get_attributes(self):
        return None

    def set_xalign(self, *args):
        pass


MODULES = {
    "Gtk": types.SimpleNamespace(
        Label=FakeLabel,
        STYLE_PROVIDER_PRIORITY_APPLICATION=600,
        StyleContext=types.SimpleNamespace(
            add_provider_for_screen=lambda *args: None,
        ),
    ),
    "Pango": types.SimpleNamespace(
        EllipsizeMode=types.SimpleNamespace(END=3),
        Weight=types.SimpleNamespace(BOLD=700),
    ),
}


class TestCatchesTheTargetBugs(unittest.TestCase):
    def test_flags_label_get_pango_attributes(self):
        src = "l = Gtk.Label()\nl.get_pango_attributes().set_weight(1)\n"
        problems = audit_sources({"right_pane.py": src}, MODULES)
        self.assertTrue(problems, "the invented Label method must be caught")
        self.assertTrue(any("get_pango_attributes" in p[2] for p in problems),
                        problems)

    def test_flags_stylecontext_get_default(self):
        src = "Gtk.StyleContext.get_default().add_provider(1, 2)\n"
        problems = audit_sources({"left_pane.py": src}, MODULES)
        self.assertTrue(any("get_default" in p[2] for p in problems), problems)

    def test_flags_a_missing_module_symbol(self):
        src = "p = Gtk.NonexistentThing()\n"
        problems = audit_sources({"x.py": src}, MODULES)
        self.assertTrue(any("NonexistentThing" in p[2] for p in problems),
                        problems)


class TestAcceptsTheCorrectedForms(unittest.TestCase):
    def test_corrected_call_sites_are_clean(self):
        src = ("l = Gtk.Label()\n"
               "l.set_attributes(None)\n"
               "l.set_xalign(0.0)\n"
               "Gtk.StyleContext.add_provider_for_screen(1, 2, 3)\n"
               "w = Pango.EllipsizeMode.END\n")
        self.assertEqual(audit_sources({"ok.py": src}, MODULES), [])

    def test_attribute_targets_are_tracked_too(self):
        src = ("self._name = Gtk.Label()\n"
               "self._name.set_text('x')\n")
        self.assertEqual(audit_sources({"ok.py": src}, MODULES), [])


class TestReportShape(unittest.TestCase):
    def test_one_line_per_distinct_problem_with_location(self):
        src = ("l = Gtk.Label()\n"
               "l.get_pango_attributes()\n"
               "l.get_pango_attributes()\n")
        problems = audit_sources({"dupe.py": src}, MODULES)
        self.assertEqual(len(problems), 1, problems)
        label, lineno, message = problems[0]
        self.assertEqual(label, "dupe.py")
        self.assertEqual(lineno, 2)

    def test_syntax_error_is_a_problem_not_a_traceback(self):
        problems = audit_sources({"bad.py": "def broken(:\n"}, MODULES)
        self.assertTrue(problems)
        self.assertIn("syntax error", problems[0][2])


class TestCairoAudit(unittest.TestCase):
    """Cairo calls get the same treatment as gi ones (D-21).

    ``draw_graph_top_label`` swallows its own errors on purpose — a drawing
    surprise costs a label, never a row — so a wrong cairo method name would
    fail *silently*: no label, no traceback, nothing to notice. The audit is
    what turns that into a line on the target.
    """

    class FakeContext:
        """Only what a real ``cairo.Context`` has, narrowed to the calls the
        UI makes (plus the ones it must reject)."""

        def move_to(self, *args):
            pass

        def line_to(self, *args):
            pass

        def show_text(self, *args):
            pass

        def text_extents(self, *args):
            return None

        def set_font_size(self, *args):
            pass

        def set_source_rgba(self, *args):
            pass

        def set_line_width(self, *args):
            pass

        def arc(self, *args):
            pass

        def fill(self, *args):
            pass

        def stroke(self, *args):
            pass

        def rectangle(self, *args):
            pass

    def setUp(self):
        self.cairo = types.SimpleNamespace(Context=self.FakeContext)

    def test_flags_a_method_cairo_does_not_have(self):
        problems = audit_cairo({"x.py": "cr.set_font_sixe(9)\n"}, self.cairo)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no method set_font_sixe", problems[0][2])

    def test_accepts_the_calls_the_ui_makes(self):
        src = ("cr.set_font_size(9.0)\n"
               "e = cr.text_extents('1.0%')\n"
               "cr.move_to(1, 2)\n"
               "cr.show_text('1.0%')\n"
               "cr.arc(1, 1, 1, 0, 6)\n"
               "cr.fill()\n"
               "cr.stroke()\n")
        self.assertEqual(audit_cairo({"ok.py": src}, self.cairo), [])

    def test_no_pycairo_is_no_problems_not_a_fake_pass(self):
        # off-box: audit_dir reports SKIP, so this must stay quiet rather
        # than invent a verdict
        self.assertEqual(
            audit_cairo({"x.py": "cr.anything_at_all()\n"}, None), [])

    def test_only_the_cairo_receiver_is_audited(self):
        # a method named in some other variable is not this check's business
        src = ("ctx.something_made_up()\n"
               "self._label.set_text('x')\n")
        self.assertEqual(audit_cairo({"x.py": src}, self.cairo), [])


if __name__ == "__main__":
    unittest.main()
