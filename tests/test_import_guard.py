"""Source-level import guard (AST, not text): nothing in ``qubes_top/core/`` or
in ``qubes_top/__init__.py`` may import ``gi`` or ``qubesadmin``, and the
top-level ``__init__.py`` may import nothing at all — one subpackage import
there and the whole package stops importing on a gi-less box.

Docstrings may legitimately mention ``gi``/``qubesadmin``, which is why this
parses the AST instead of matching substrings. Run standalone:

    python3 tests/test_import_guard.py
"""
import ast
import pathlib
import sys
import tempfile
import unittest

# sys.path shim (see test_import_contract.py).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

BANNED = {"gi", "qubesadmin"}


def imported_top_names(source: str, filename: str) -> set:
    """Top-level module names referenced by any import in the file."""
    tree = ast.parse(source, filename=filename)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
            elif node.level > 0:
                names.add("<relative>")
    return names


def core_targets(root: pathlib.Path) -> list:
    """Every .py in ``qubes_top/__init__.py`` + ``qubes_top/core/``, **recursive**
    (card t_2e8dda49, orchestrator gap note): the non-recursive ``glob`` the
    guard shipped with silently stopped covering ``core/<subpkg>/module.py``
    while ``assertGreaterEqual(len(targets), 2)`` stayed green on the two
    ``__init__.py`` files. ``rglob`` closes that. ``dom0/`` and ``ui/`` stay
    out on purpose — they legitimately import ``gi``/``qubesadmin``."""
    return [root / "__init__.py"] + sorted((root / "core").rglob("*.py"))


def banned_imports(targets) -> dict:
    """{path: sorted banned top names} for every target that imports a
    banned name ({} when clean). One mechanism, reused by the real-tree,
    synthetic, and subpackage-coverage tests."""
    result = {}
    for target in targets:
        names = imported_top_names(target.read_text(), str(target))
        bad = names & BANNED
        if bad:
            result[target] = sorted(bad)
    return result


class TestImportGuard(unittest.TestCase):
    def test_no_banned_imports_in_core_and_init(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "qubes_top"
        targets = core_targets(root)
        self.assertGreaterEqual(len(targets), 2, "guard has nothing to check")
        self.assertEqual(banned_imports(targets), {},
                         f"banned imports: {banned_imports(targets)}")

    def test_top_init_imports_nothing(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "qubes_top"
        names = imported_top_names((root / "__init__.py").read_text(),
                                  str(root / "__init__.py"))
        self.assertEqual(names, set(),
                         f"qubes_top/__init__.py imports {sorted(names)}")

    def test_guard_bites(self):
        # Prove the guard is not decoration: a synthetic module that imports
        # the banned names must be flagged by the same machinery.
        bad = imported_top_names("import gi\nfrom qubesadmin import app\n",
                                "synthetic")
        self.assertTrue(BANNED <= bad)

    def test_guard_covers_subpackages(self):
        # Pin the coverage itself (card t_2e8dda49 gap note): a module that
        # lands in ``qubes_top/core/<subpkg>/`` must be in the scan set and
        # must be flagged by the same ``banned_imports`` machinery. Built in
        # a throwaway dir — never the real tree.
        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp) / "qubes_top"
            sub = fake / "core" / "subpkg"
            sub.mkdir(parents=True)
            (fake / "__init__.py").write_text("clean\n")
            (fake / "core" / "__init__.py").write_text("clean\n")
            (sub / "leak.py").write_text("import gi\n")
            targets = core_targets(fake)
            self.assertIn(sub / "leak.py", targets,
                         "the recursive scan missed a core subpackage")
            self.assertIn(sub / "leak.py", banned_imports(targets),
                         "the scan found the file but the check did not flag "
                         "its 'import gi'")


if __name__ == "__main__":
    unittest.main(verbosity=2)
