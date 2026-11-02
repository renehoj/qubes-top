"""Static invariants that no off-box run can exercise (card t_2e8dda49).

Parses the AST, not substrings — docstrings legitimately mention the
forbidden names (``qvm-run`` is named in a docstring as what this app
never does), so text matching would either false-positive or need
per-file holes.

Guards (each has a ``test_guard_*_bites`` mutation-proof):

3. **show_all on the window itself, after the populate call.** GTK3
   visibility is not inherited: a child's ``show_all`` leaves the window
   hidden and the window opens blank (the previous project's live
   failure, recorded in ``qubes_top/ui/window.py``'s docstring). The guard
   requires a ``self.show_all()`` call (value is ``self`` — a child's
   ``self._left.show_all()`` does not count) inside
   ``QubesTopWindow.__init__``, positioned *after* the ``self._populate()``
   call.
4. **No xenstore write call anywhere in the app tree.** ``xs_write``,
   ``xs_rm`` calls and the ``xenstore-write`` command string: zero
   matches by AST over ``qubes_top/**``. Read-only must be provable by the
   scan, not asserted in a README. (The whole-tree *grep* additionally
   hits the test double in ``tests/test_xenstore_adapter.py`` — a fake
   the app cannot reach; the spy test asserts the app never calls it.
   The app tree itself is the invariant, and it is clean.)
5. **No ``shell=True``, no network imports, no ``qvm-run``** in the app
   tree (``qubes_top/**``). Network is a pinned import set (a network
   import is a network import wherever it sits in a function); the
   ``qvm-run`` check skips docstrings (the name is legitimately mentioned
   as what the app does *not* do) but flags a live string constant or a
   ``qvm_run`` import.

Run standalone:

    python3 tests/test_invariants.py

The sys.path shim below makes this module layout-independent.
"""
import ast
import pathlib
import sys
import unittest

# sys.path shim: puts the repo root (the parent of tests/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

#: The network import set (guard 5): a top-level module name in this set
#: anywhere in an app module is a network call surface. ctypes is
#: deliberately NOT here — the libxenstore load is a local library, not
#: a network.
NETWORK_TOPS = {
    "socket", "socketserver", "urllib", "http", "requests", "httpx",
    "aiohttp", "ftplib", "smtplib", "telnetlib", "imaplib", "poplib",
    "websockets",
}

#: Guard 4: the xenstore write surface.
XENSTORE_WRITE_ATTRS = {"xs_write", "xs_rm"}
XENSTORE_WRITE_STRINGS = {"xenstore-write"}

#: Guard 5: the qvm-run surface (command name + module name).
QVM_RUN_STRINGS = {"qvm-run", "qvm_run"}

#: Guard 5's one documented exception (DESIGN.md D-15, D-27): the guest
#: process table and the netvm's vif counters run a fixed argv via qvm-run,
#: and both build it on exactly one no-shell prefix. The scan still fails on
#: a second call site, on any occurrence in another module, and on importing
#: the qubesadmin tool.
QVM_RUN_ALLOWED_MODULE = "guestproc.py"
QVM_RUN_ALLOWED_FUNCTION = "qvm_run_prefix"


def _app_tree():
    """Every .py under ``qubes_top/`` (the app, adapters + probe included)."""
    root = pathlib.Path(__file__).resolve().parents[1] / "qubes_top"
    return sorted(root.rglob("*.py"))


def _docstring_ids(tree):
    """id() of every docstring Constant (so the string scan can skip
    them — a docstring may legitimately name a forbidden thing)."""
    ids = set()
    for node in ast.walk(tree):
        if (isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef))
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)):
            ids.add(id(node.body[0].value))
    return ids


def _strings(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, (str, bytes))
            and id(n) not in _docstring_ids(tree)]


def _imports(tree):
    """(lineno, top_name) for every import statement (any depth)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name.split(".")[0]))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.append((node.lineno, node.module.split(".")[0]))
    return out


def _attribute_calls(tree, attrs):
    """(lineno, attr) for every call whose target is an attribute in
    ``attrs`` (``xs.xs_write(...)``)."""
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in attrs):
            out.append((node.lineno, node.func.attr))
    return out


class TestWindowShowAllAfterPopulate(unittest.TestCase):
    """Guard 3: the top-level window's ``show_all()`` — on itself, after
    the call that populates rows."""

    def _window_init(self):
        path = (pathlib.Path(__file__).resolve().parents[1]
                / "qubes_top" / "ui" / "window.py")
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.ClassDef) and node.name ==
                    "QubesTopWindow"):
                for sub in node.body:
                    if (isinstance(sub, ast.FunctionDef)
                            and sub.name == "__init__"):
                        return sub
        self.fail("QubesTopWindow.__init__ not found")

    def test_show_all_on_self_after_populate(self):
        init = self._window_init()
        self_all = []
        populate = []
        for node in ast.walk(init):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"):
                # value is the Name ``self`` — a child's show_all
                # (``self._left.show_all()``: value is an Attribute) does
                # not count.
                if node.func.attr == "show_all":
                    self_all.append(node.lineno)
                elif node.func.attr == "_populate":
                    populate.append(node.lineno)
        self.assertTrue(populate, "the _populate() call is missing")
        self.assertTrue(self_all,
                        "no self.show_all() in __init__ (a child's "
                        "show_all does not count)")
        self.assertGreater(min(self_all), max(populate),
                          "self.show_all() must come AFTER the call that "
                          "populates rows")

    def test_guard_bites_child_show_all(self):
        # A child's show_all must not satisfy the guard.
        src = (
            "class W(object):\n"
            "    def __init__(self):\n"
            "        self._populate()\n"
            "        self._left.show_all()\n"
        )
        tree = ast.parse(src)
        self_all = [n.lineno for n in ast.walk(tree)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "self"
                    and n.func.attr == "show_all"]
        self.assertEqual(self_all, [])

    def test_guard_bites_order(self):
        # show_all before the populate call must not satisfy the guard.
        src = (
            "class W(object):\n"
            "    def __init__(self):\n"
            "        self.show_all()\n"
            "        self._populate()\n"
        )
        tree = ast.parse(src)
        self_all = []
        populate = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"):
                if node.func.attr == "show_all":
                    self_all.append(node.lineno)
                elif node.func.attr == "_populate":
                    populate.append(node.lineno)
        self.assertLessEqual(min(self_all), max(populate),
                            "the order check must fail when show_all is "
                            "first")


class TestNoXenstoreWrites(unittest.TestCase):
    """Guard 4: no xenstore write call anywhere in the app tree."""

    def test_no_write_calls_in_app_tree(self):
        bad = {}
        for p in _app_tree():
            tree = ast.parse(p.read_text())
            hits = _attribute_calls(tree, XENSTORE_WRITE_ATTRS)
            for lineno, attr in hits:
                bad.setdefault(p.name, []).append(
                    "line %d: %s(...)" % (lineno, attr))
            for lineno, top in _imports(tree):
                if top in XENSTORE_WRITE_ATTRS:
                    bad.setdefault(p.name, []).append(
                        "line %d: import %s" % (lineno, top))
            for s in _strings(tree):
                # a string/bytes constant in the tree — includes a
                # ``subprocess`` argv entry for the command.
                val = (s.value if isinstance(s.value, str)
                       else s.value.decode("utf-8", "replace")
                       if isinstance(s.value, bytes) else None)
                if val in XENSTORE_WRITE_STRINGS:
                    bad.setdefault(p.name, []).append(
                        "line %d: the string %r" % (s.lineno, val))
        self.assertEqual(bad, {}, "xenstore write surface found: %s" % bad)

    def test_guard_bites(self):
        src = "def f(xs, h):\n    xs.xs_write(h, b'k', b'v', 1)\n    " \
              "xs.xs_rm(h, b'k')\n"
        tree = ast.parse(src)
        hits = _attribute_calls(tree, XENSTORE_WRITE_ATTRS)
        self.assertEqual(len(hits), 2, "the call check must flag both "
                                       "xs_write and xs_rm")
        src2 = 'CMD = "xenstore-write"\n'
        tree2 = ast.parse(src2)
        found = [s for s in _strings(tree2)
                 if isinstance(s.value, (str, bytes))
                 and (s.value if isinstance(s.value, str)
                      else s.value.decode("utf-8", "replace"))
                 in XENSTORE_WRITE_STRINGS]
        self.assertTrue(found, "the string check must flag the command "
                               "name")


class TestNoShellNetworkOrQvmRun(unittest.TestCase):
    """Guard 5: no ``shell=True``, no network, no ``qvm-run`` in the app
    tree."""

    def test_no_shell_true(self):
        bad = {}
        for p in _app_tree():
            tree = ast.parse(p.read_text())
            hits = [n.lineno for n in ast.walk(tree)
                   if isinstance(n, ast.Call)
                   and any(k.arg == "shell"
                           and isinstance(k.value, ast.Constant)
                           and k.value.value is True
                           for k in n.keywords)]
            if hits:
                bad[p.name] = hits
        self.assertEqual(bad, {}, "shell=True found: %s" % bad)

    def test_guard_bites_shell_true(self):
        src = "import subprocess\n" \
              "def f():\n    subprocess.run('id', shell=True)\n"
        tree = ast.parse(src)
        hits = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Call)
               and any(k.arg == "shell"
                       and isinstance(k.value, ast.Constant)
                       and k.value.value is True
                       for k in n.keywords)]
        self.assertTrue(hits)

    def test_no_network_imports(self):
        bad = {}
        for p in _app_tree():
            tree = ast.parse(p.read_text())
            hits = [(lineno, top) for lineno, top in _imports(tree)
                    if top in NETWORK_TOPS]
            if hits:
                bad[p.name] = hits
        self.assertEqual(bad, {}, "network imports found: %s" % bad)

    def test_guard_bites_network(self):
        src = "def f():\n    import socket\n    return socket\n"
        tops = [top for _l, top in _imports(ast.parse(src))]
        self.assertIn("socket", tops)

    def test_no_qvm_run_outside_the_one_guest_process_argv(self):
        """D-15: ``qvm-run`` exists in exactly one place — the argv builder
        in ``guestproc.py`` that reads a guest's process table. Anywhere else
        (another call site, another module, an import of the qubesadmin
        tool) is still a design violation."""
        bad = {}
        allowed = []
        for p in _app_tree():
            tree = ast.parse(p.read_text())
            spans = [(n.lineno, n.end_lineno or n.lineno)
                     for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)
                     and n.name == QVM_RUN_ALLOWED_FUNCTION]
            hits = [("import", lineno) for lineno, top in _imports(tree)
                    if top.endswith("qvm_run")]
            for s in _strings(tree):
                val = (s.value if isinstance(s.value, str)
                       else s.value.decode("utf-8", "replace")
                       if isinstance(s.value, bytes) else None)
                if val in QVM_RUN_STRINGS:
                    hits.append(("string", s.lineno))
            for kind, lineno in hits:
                permitted = (kind == "string"
                             and p.name == QVM_RUN_ALLOWED_MODULE
                             and any(a <= lineno <= b for a, b in spans))
                if permitted:
                    allowed.append(lineno)
                else:
                    bad.setdefault(p.name, []).append(lineno)
        self.assertEqual(bad, {}, "qvm-run surface found: %s" % bad)
        self.assertEqual(
            len(allowed), 1,
            "the single permitted qvm-run occurrence is gone or duplicated: %s"
            % allowed)

    def test_guard_bites_qvm_run(self):
        # a live (non-docstring) string constant must be flagged, while
        # a docstring mention must not.
        src = 'CMD = "qvm-run"\n'
        found = [s for s in _strings(ast.parse(src))
                 if isinstance(s.value, (str, bytes))
                 and (s.value if isinstance(s.value, str)
                      else s.value.decode("utf-8", "replace"))
                 in QVM_RUN_STRINGS]
        self.assertTrue(found)
        doc = '"""never shells out to qvm-run.\n"""'
        doc_found = [s for s in _strings(ast.parse(doc))
                    if isinstance(s.value, (str, bytes))
                    and (s.value if isinstance(s.value, str)
                         else s.value.decode("utf-8", "replace"))
                    in QVM_RUN_STRINGS]
        self.assertEqual(doc_found, [])

    def test_docstring_mention_in_a_real_module_is_not_flagged(self):
        # processes.py names qvm-run in its module docstring (what the
        # app does NOT do); the guard must pass it.
        p = (pathlib.Path(__file__).resolve().parents[1]
             / "qubes_top" / "ui" / "processes.py")
        tree = ast.parse(p.read_text())
        hits = [s for s in _strings(tree)
                if isinstance(s.value, str) and s.value in QVM_RUN_STRINGS]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
