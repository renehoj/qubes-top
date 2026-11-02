"""Paned position persistence: unit tests (card t_4d9516cd, M3 gate).

``PanedConfig`` loads the ``Gtk.Paned`` divider position (``~/.config/
qubes-top/``) with a best-effort, corrupt-proof contract: a missing, unreadable,
malformed, missing-key, or non-integer file all return ``DEFAULT_POSITION``
(a corrupt config must never break startup); a negative value is clamped to 0;
``save`` is atomic (temp file + ``os.replace``) and best-effort (a failed save
is swallowed — a lost position must never kill the app). The path is injected
so this is testable off-box with a temp file.

Run standalone:

    python3 tests/test_ui_config.py

The sys.path shim below makes this module layout-independent.
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

# sys.path shim: puts the repo root (the parent of tests/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.ui.config import (  # noqa: E402
    DEFAULT_POSITION,
    PanedConfig,
)


class TestPanedConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "cfg", "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, content):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            f.write(content)

    def test_load_default_when_missing(self):
        self.assertEqual(PanedConfig(self.path).load(), DEFAULT_POSITION)

    def test_round_trip(self):
        PanedConfig(self.path).save(700)
        self.assertEqual(PanedConfig(self.path).load(), 700)

    def test_negative_clamped_to_zero(self):
        PanedConfig(self.path).save(-50)
        self.assertEqual(PanedConfig(self.path).load(), 0)

    def test_float_coerced_to_int(self):
        self._write(json.dumps({"paned_position": 3.9}))
        self.assertEqual(PanedConfig(self.path).load(), 3)

    def test_malformed_file_default(self):
        self._write("not json at all")
        self.assertEqual(PanedConfig(self.path).load(), DEFAULT_POSITION)

    def test_missing_key_default(self):
        self._write('{"other": 1}')
        self.assertEqual(PanedConfig(self.path).load(), DEFAULT_POSITION)

    def test_non_integer_string_default(self):
        self._write('{"paned_position": "abc"}')
        self.assertEqual(PanedConfig(self.path).load(), DEFAULT_POSITION)

    def test_save_atomic_no_tmp_leftover(self):
        PanedConfig(self.path).save(100)
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_save_creates_parent_dirs(self):
        nested = os.path.join(self.tmp.name, "a", "b", "c", "config.json")
        PanedConfig(nested).save(50)
        self.assertTrue(os.path.exists(nested))

    def test_save_failure_swallowed(self):
        # a path whose parent cannot be created (mkdir under /proc fails) must
        # not raise (a lost position is a minor inconvenience, not a crash)
        c = PanedConfig("/proc/definitely-not-writable/config.json")
        c.save(100)  # must not raise
        self.assertFalse(
            os.path.exists("/proc/definitely-not-writable/config.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
