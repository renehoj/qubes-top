"""dom0 host stats source: unit tests (card t_4d9516cd, M3 gate).

``host_to_record`` does only unit conversion (jiffies -> ns via CLK_TCK) and
the trivial ``used = total - available`` — the *metric* arithmetic (the CPU
delta, the step quantisation) is the core model's, and is tested where it
lives. ``ProcHostProvider`` is defensive: a missing or malformed file returns
``None`` (the app keeps the last known dom0 row) rather than raising into the
tick (the design's "no silent crash" rule). Tests drive it with fake ``/proc``
files (the injected paths + clock) plus a real-``/proc`` smoke when available.

Run standalone:

    python3 tests/test_ui_hostprovider.py

The sys.path shim below makes this module layout-independent.
"""
import os
import pathlib
import sys
import tempfile
import unittest

# sys.path shim: puts the repo root (the parent of tests/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.ui.hostprovider import (  # noqa: E402
    HostRaw,
    ProcHostProvider,
    host_to_record,
)


class TestHostToRecord(unittest.TestCase):
    def test_unit_conversion_and_fields(self):
        raw = HostRaw(cpu_jiffies=305, ts=1.5, mem_total_kib=1000000,
                     mem_available_kib=500000)
        r = host_to_record(raw, hz=100)
        self.assertEqual(r.cpu_time_ns, 305 * 1_000_000_000 // 100)
        self.assertEqual(r.mem_used_kib, 500000)
        self.assertEqual(r.mem_assigned_kib, 1000000)
        self.assertEqual(r.mem_max_kib, 1000000)
        self.assertEqual(r.state, "Running")
        self.assertEqual(r.domid, 0)
        self.assertEqual(r.name, "dom0")
        self.assertEqual(r.ts, 1.5)

    def test_used_clamped_non_negative(self):
        raw = HostRaw(cpu_jiffies=10, ts=1.0, mem_total_kib=1000,
                     mem_available_kib=2000)
        self.assertEqual(host_to_record(raw, 100).mem_used_kib, 0)

    def test_hz_nonpositive_fallback(self):
        raw = HostRaw(cpu_jiffies=100, ts=1.0, mem_total_kib=1000,
                     mem_available_kib=500)
        self.assertEqual(host_to_record(raw, 0).cpu_time_ns,
                         100 * 1_000_000_000 // 100)

    def test_name_override(self):
        raw = HostRaw(cpu_jiffies=1, ts=1.0, mem_total_kib=10,
                     mem_available_kib=5)
        self.assertEqual(host_to_record(raw, 100, name="host").name, "host")


class TestProcHostProvider(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stat = os.path.join(self.tmp.name, "stat")
        self.meminfo = os.path.join(self.tmp.name, "meminfo")
        self.provider = ProcHostProvider(clock=lambda: 123.0,
                                        stat_path=self.stat,
                                        meminfo_path=self.meminfo)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, path, content):
        with open(path, "w") as f:
            f.write(content)

    def test_read(self):
        # user=100 nice=0 system=200 idle=500 iowait=10 irq=0 softirq=5 steal=0
        # total=815, busy = total - idle - iowait = 305
        self._write(self.stat, "cpu  100 0 200 500 10 0 5 0 0 0\n")
        self._write(self.meminfo, "MemTotal:       1000000 kB\n"
                                  "MemAvailable:     500000 kB\n")
        raw = self.provider.read()
        self.assertIsNotNone(raw)
        self.assertEqual(raw.cpu_jiffies, 305)
        self.assertEqual(raw.mem_total_kib, 1000000)
        self.assertEqual(raw.mem_available_kib, 500000)
        self.assertEqual(raw.ts, 123.0)

    def test_missing_stat_is_none(self):
        self._write(self.meminfo, "MemTotal: 1000 kB\n")
        self.assertIsNone(self.provider.read())

    def test_malformed_stat_too_few_fields(self):
        self._write(self.stat, "cpu  100 0\n")  # < 4 fields -> None
        self._write(self.meminfo, "MemTotal: 1000 kB\n")
        self.assertIsNone(self.provider.read())

    def test_missing_memtotal_is_none(self):
        self._write(self.stat, "cpu  100 0 200 500 10 0 5 0 0 0\n")
        self._write(self.meminfo, "MemFree: 1000 kB\n")  # no MemTotal
        self.assertIsNone(self.provider.read())

    def test_no_memavailable_defaults_zero(self):
        self._write(self.stat, "cpu  100 0 200 500 10 0 5 0 0 0\n")
        self._write(self.meminfo, "MemTotal:       1000 kB\n")  # no MemAvailable
        raw = self.provider.read()
        self.assertIsNotNone(raw)
        self.assertEqual(raw.mem_available_kib, 0)

    def test_real_proc_smoke(self):
        if os.path.exists("/proc/stat") and os.path.exists("/proc/meminfo"):
            raw = ProcHostProvider().read()
            self.assertIsNotNone(raw)
            self.assertGreaterEqual(raw.cpu_jiffies, 0)
            self.assertGreater(raw.mem_total_kib, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
