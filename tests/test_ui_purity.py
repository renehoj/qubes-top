"""Static purity guards for the new right-pane modules (card t_b14eabf7,
M3 gate).

This box has no GTK (gi absent) and no Qubes (qubesadmin absent), so the
GTK modules cannot execute here; what CAN be checked off-box is the
*structural* contract, pinned here with an AST walk:

1. **No ``set_markup``** in any ``qubes_top/ui`` module — every label is
   set via ``set_text`` (no markup, no injection surface from untrusted
   qube names). The guard bites: a synthetic ``set_markup`` source is
   flagged.
2. **The pure modules import no ``gi`` and no top-level ``qubesadmin``**:
   ``rightspec``/``qubemeta``/``processes`` are the unit-tested layer;
   ``qubesadmin`` in ``qubemeta`` is deferred to first use (a call-time
   ``import qubesadmin`` inside a function is legal; a top-level import
   is not).
3. **Gtk pinned before Gdk** in every gi module (the package ``__init__``
   contract: a bare ``from gi.repository import Gdk`` on a GTK4 box
   resolves Gdk to 4.0 and kills the Gtk 3.0 import on the target — a
   failure a dev box without GTK4 cannot reproduce).

Run standalone:

    python3 tests/test_ui_purity.py

The sys.path shim below makes this module layout-independent.
"""
import ast
import pathlib
import sys
import unittest

# sys.path shim: puts the repo root (the parent of tests/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# The pure (off-box testable) modules: no gi, no top-level qubesadmin.
PURE_MODULES = ("rightspec.py", "qubemeta.py", "processes.py", "guestproc.py",
                "labelcolors.py", "iostats.py")

# The gi modules: pinned Gtk before Gdk.
GI_MODULES = ("left_pane.py", "right_pane.py", "charts.py", "infobox.py",
              "window.py", "app.py", "theme.py")


def _repo_root():
    return pathlib.Path(__file__).resolve().parents[1]


def _ui_dir():
    return _repo_root() / "qubes_top" / "ui"


def _module_path(name):
    return _repo_root() / "qubes_top" / "ui" / name


def top_level_top_names(tree):
    """Top-level import top names (import X / import X.Y; from X import ..).

    Only the *module top level* (``tree.body``) — a call-time ``import
    qubesadmin`` inside a function is the deferred-import contract and is
    legal in the pure modules.
    """
    names = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(stmt, ast.ImportFrom):
            if stmt.module:
                names.add(stmt.module.split(".")[0])
    return names


class TestNoSetMarkup(unittest.TestCase):
    def _offenders(self, source):
        tree = ast.parse(source)
        return [node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set_markup"]

    def test_no_set_markup_in_any_ui_module(self):
        for p in sorted(_ui_dir().glob("*.py")):
            self.assertEqual(self._offenders(p.read_text()), [], p.name)

    def test_guard_bites(self):
        self.assertNotEqual(self._offenders("a.set_markup('<b>x</b>')"), [])


class TestPureModuleImports(unittest.TestCase):
    def test_no_gi_and_no_top_level_qubesadmin(self):
        for name in PURE_MODULES:
            tree = ast.parse(_module_path(name).read_text())
            tops = top_level_top_names(tree)
            self.assertNotIn("gi", tops, name)
            self.assertNotIn("qubesadmin", tops, name)

    def test_deferred_qubesadmin_is_allowed_inside_functions(self):
        src = "def f():\n    import qubesadmin\n    return qubesadmin\n"
        # a call-time import is not a top-level one: the guard must not
        # flag it (the deferred-import contract)
        self.assertNotIn("qubesadmin", top_level_top_names(ast.parse(src)))


def gi_require_pins(tree):
    """lineno -> (namespace, version) of every ``gi.require_version`` call."""
    pins = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "require_version"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "gi"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)):
            pins[node.lineno] = (node.args[0].value, node.args[1].value)
    return pins


def module_uses_gi_repository(tree) -> bool:
    """True when the module pulls a ``gi.repository`` namespace (the card's
    population: *every module importing ``gi.repository``*)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module == "gi.repository"
                    or (node.module or "").startswith("gi.repository.")):
                return True
    return False


class TestGdkPinOrder(unittest.TestCase):
    def test_gtk_pinned_before_gdk_in_every_gi_module(self):
        for name in GI_MODULES:
            tree = ast.parse(_module_path(name).read_text())
            pins = gi_require_pins(tree)
            gtk = [l for l, (ns, _v) in pins.items() if ns == "Gtk"]
            gdk = [l for l, (ns, _v) in pins.items() if ns == "Gdk"]
            self.assertTrue(gtk, name + ": Gtk 3.0 pin missing")
            self.assertTrue(gdk, name + ": Gdk 3.0 pin missing")
            for g in gtk:
                self.assertEqual(pins[g][1], "3.0", name + ": Gtk pin not 3.0")
            for g in gdk:
                self.assertEqual(pins[g][1], "3.0", name + ": Gdk pin not 3.0")
            self.assertLess(min(gtk), min(gdk),
                           name + ": the Gtk pin must come before the Gdk pin")


class TestGiModuleScan(unittest.TestCase):
    """The card's guard 1, self-extending: the pin contract is checked on
    *every* module that imports ``gi.repository``, discovered from the tree
    (a fixed tuple silently stops checking the day a fifth gi module lands).
    The known-four pin (below) stops that from silently *shrinking*."""

    def _gi_modules(self):
        root = _repo_root() / "qubes_top"
        found = []
        for p in sorted(root.rglob("*.py")):
            tree = ast.parse(p.read_text())
            if module_uses_gi_repository(tree):
                found.append(p)
        return found

    def test_every_gi_module_pins_gtk3_and_gdk3_gtk_first(self):
        mods = self._gi_modules()
        self.assertGreaterEqual(len(mods), 4, "no gi modules found to check")
        for p in mods:
            rel = p.name
            tree = ast.parse(p.read_text())
            pins = gi_require_pins(tree)
            gtk = sorted(l for l, (ns, _v) in pins.items() if ns == "Gtk")
            gdk = sorted(l for l, (ns, _v) in pins.items() if ns == "Gdk")
            self.assertTrue(gtk, rel + ": Gtk pin missing (gi module)")
            self.assertTrue(gdk, rel + ": Gdk pin missing (gi module)")
            for l in gtk:
                self.assertEqual(pins[l][1], "3.0", rel + ": Gtk pin not 3.0")
            for l in gdk:
                self.assertEqual(pins[l][1], "3.0", rel + ": Gdk pin not 3.0")
            self.assertLess(min(gtk), min(gdk),
                           rel + ": the Gtk pin must come before the Gdk pin")

    def test_scan_covers_the_known_gi_modules(self):
        # Coverage pin: the discovery must still find the four shipped gi
        # modules (a refactor that hides one from the scan is a red, not a
        # silent green).
        found = {p.name for p in self._gi_modules()}
        self.assertTrue(set(GI_MODULES) <= found,
                        "scan lost a known gi module: %s"
                        % sorted(set(GI_MODULES) - found))


if __name__ == "__main__":
    unittest.main(verbosity=2)
