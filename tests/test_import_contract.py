"""Import contract: ``qubes_top`` imports cleanly on a box without
gi/qubesadmin.

This is the runtime behaviour that is verifiable on the dev box (``ai`` has
no gi, no qubesadmin, no /etc/qubes-release, no xl/xenstore). Run
standalone:

    python3 tests/test_import_contract.py

The sys.path shim below makes this module layout-independent: it works no
matter where it is run from, and does not require ``tests/`` to be an
importable package — which is why run_tests.py executes modules as scripts
instead of using ``unittest discover``.
"""
import pathlib
import sys
import unittest

# sys.path shim: puts the repo root (the parent of tests/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class TestImportContract(unittest.TestCase):
    def test_top_level_import(self):
        import qubes_top
        self.assertTrue(qubes_top.__file__)

    def test_import_side_effects(self):
        # Fresh process (run_tests.py runs each module as its own script), so
        # anything in sys.modules here arrived via this module. With gi and
        # qubesadmin absent from the box, this proves the top-level import
        # pulled neither in — the core import contract. The reference below is
        # deliberate: it keeps pyflakes honest without a noqa (pyflakes does
        # not honour noqa), and the M1 gate requires a clean run.
        import qubes_top
        self.assertIsNotNone(qubes_top.__file__)
        self.assertNotIn("gi", sys.modules)
        self.assertNotIn("qubesadmin", sys.modules)

    def test_core_imports_clean(self):
        import qubes_top.core
        self.assertIsNotNone(qubes_top.core.__name__)
        self.assertNotIn("gi", sys.modules)
        self.assertNotIn("qubesadmin", sys.modules)

    def test_layout_present(self):
        import qubes_top
        root = pathlib.Path(qubes_top.__file__).resolve().parent
        for sub in ("core", "dom0", "ui"):
            self.assertTrue((root / sub / "__init__.py").is_file(),
                            f"missing {sub}/__init__.py")
        self.assertTrue((root.parent / "run_tests.py").is_file())
        self.assertTrue((root.parent / "DESIGN.md").is_file())
        self.assertTrue((root.parent / "packaging").is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
