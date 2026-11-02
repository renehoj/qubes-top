"""Dom0 process provider (the ProcessProvider seam): unit tests
(card t_b14eabf7, M3 gate).

Pinned here, against a fake ``/proc`` root (never the real one): the
stateful baseline (the first reading has ``cpu_pct=None`` — the "—",
never a number; the card's no-fabrication contract), the jiffies-delta
math (top-like ``% of one CPU``; a busy multi-threaded process may
exceed 100, D-3), a reused pid resets the baseline (0.0, never a
negative), a clock rollback clamps to 0.0 (no fabricated delta), the rss
page->KiB conversion (no silent zero: a page-less read is ``None``),
the comm parsing (the name is the text between the first ``(`` and the
last ``)`` — a comm may itself contain parens; an unparseable line is
skipped, never a crash), the guest qube -> ``None`` contract (v1: dom0
only; the pane renders the explicit "not available for this qube"), a
missing pid directory is skipped, a missing proc root degrades to ``[]``
(a read failure, never a crash), the baseline cleanup (a vanished pid
drops its baseline, so a later pid reuse does not borrow an old one),
and the injectables (``proc_root``/``clock``/``hz``/``page_size``/
``host_name`` — the test never relies on the real ``/proc`` or
``time.monotonic``).

Run standalone:

    python3 tests/test_ui_processes.py

The sys.path shim below makes this module layout-independent.
"""
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

# sys.path shim: puts the repo root (the parent of tests/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.ui.processes import Dom0ProcProvider  # noqa: E402

MiB = 1024  # KiB per MiB


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def write_stat(root, pid, name, utime, stime, rss_pages=1024):
    """Write a minimal but valid /proc/<pid>/stat (the index-21 rss)."""
    d = os.path.join(root, str(pid))
    os.makedirs(d, exist_ok=True)
    # exactly 22 fields after the comm: utime=11, stime=12, rss=21 (the
    # real /proc/<pid>/stat layout)
    after = ["S", "0", "1", "1", "0", "-1", "4194562", "100", "0", "0", "0",
             str(utime), str(stime), "1", "0", "20", "0", "1", "1234",
             "50000000", "1000", str(rss_pages)]
    with open(os.path.join(d, "stat"), "w") as f:
        f.write("%d (%s) %s\n" % (pid, name, " ".join(after)))


class TestDom0ProcProvider(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="qubes_top_test_proc_")
        self.clock = FakeClock()
        self.p = Dom0ProcProvider(proc_root=self.root, clock=self.clock,
                                 hz=100, page_size=4096, host_name="dom0")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_guest_qube_is_none_not_crash(self):
        self.assertIsNone(self.p.list("work"))

    def test_empty_root_is_empty(self):
        self.assertEqual(self.p.list("dom0"), [])

    def test_missing_root_degrades_to_empty(self):
        p = Dom0ProcProvider(proc_root=self.root + "/nope", clock=self.clock,
                            hz=100, page_size=4096, host_name="dom0")
        self.assertEqual(p.list("dom0"), [])

    def test_first_reading_has_none_cpu_and_kib_rss(self):
        write_stat(self.root, 1, "systemd", utime=5, stime=0, rss_pages=1024)
        rows = self.p.list("dom0")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.pid, 1)
        self.assertEqual(r.name, "systemd")
        self.assertIsNone(r.cpu_pct)  # no baseline yet: "—", never a number
        # 1024 pages * 4096 B = 4 MiB = 4096 KiB (the page->KiB conversion)
        self.assertEqual(r.rss_kib, 4096)

    def test_second_reading_computes_the_delta(self):
        write_stat(self.root, 1, "systemd", utime=5, stime=0)
        self.p.list("dom0")
        self.clock.advance(1.0)  # 1 s later, hz=100 -> 100 jiffies in the window
        write_stat(self.root, 1, "systemd", utime=105, stime=0)
        rows = self.p.list("dom0")
        # (105-5) jiffies / (1 s * 100 hz) * 100 = 100.0% of one CPU
        self.assertEqual(rows[0].cpu_pct, 100.0)

    def test_delta_can_exceed_100(self):
        # D-3: a multi-threaded busy process is >100% of one CPU — never
        # clamped
        write_stat(self.root, 2, "mt", utime=0, stime=0)
        self.p.list("dom0")
        self.clock.advance(1.0)
        write_stat(self.root, 2, "mt", utime=250, stime=0)
        rows = self.p.list("dom0")
        self.assertEqual(rows[0].cpu_pct, 250.0)

    def test_idle_is_zero(self):
        write_stat(self.root, 3, "idle", utime=10, stime=0)
        self.p.list("dom0")
        self.clock.advance(1.0)
        rows = self.p.list("dom0")
        self.assertEqual(rows[0].cpu_pct, 0.0)

    def test_pid_reuse_resets_baseline(self):
        write_stat(self.root, 4, "first", utime=100, stime=0)
        self.p.list("dom0")
        self.clock.advance(1.0)
        # the pid came back with a *lower* total (a new process reused
        # the number): the baseline is invalid -> 0.0, never negative
        write_stat(self.root, 4, "second", utime=1, stime=0)
        rows = self.p.list("dom0")
        self.assertEqual(rows[0].cpu_pct, 0.0)
        # and the baseline is now the new process's, so the next delta is
        # computed from it (the old baseline did not leak through):
        # 1 -> 101 jiffies over 1 s at hz=100 = 100.0%
        self.clock.advance(1.0)
        write_stat(self.root, 4, "second", utime=101, stime=0)
        rows = self.p.list("dom0")
        self.assertEqual(rows[0].cpu_pct, 100.0)

    def test_clock_rollback_clamps_to_zero(self):
        write_stat(self.root, 5, "a", utime=5, stime=0)
        self.p.list("dom0")
        self.clock.advance(-2.0)  # the monotonic clock never goes back; a
        # test simulates the degenerate case: dt <= 0 -> 0.0, no delta
        write_stat(self.root, 5, "a", utime=205, stime=0)
        rows = self.p.list("dom0")
        self.assertEqual(rows[0].cpu_pct, 0.0)

    def test_vanished_pid_drops_its_baseline(self):
        write_stat(self.root, 6, "ephemeral", utime=5, stime=0)
        self.p.list("dom0")
        os.remove(os.path.join(self.root, "6", "stat"))
        self.clock.advance(1.0)
        rows = self.p.list("dom0")
        self.assertEqual(rows, [])  # the pid is gone: no row, no row for others
        # the baseline is gone too: a later pid-6 reuse starts fresh
        write_stat(self.root, 6, "ephemeral", utime=5, stime=0)
        rows = self.p.list("dom0")
        self.assertIsNone(rows[0].cpu_pct)  # None, not the old process's math

    def test_missing_pid_directory_is_skipped(self):
        os.makedirs(os.path.join(self.root, "7"), exist_ok=True)
        # a directory without a stat file: skipped, never a crash
        rows = self.p.list("dom0")
        self.assertEqual(rows, [])

    def test_malformed_stat_is_skipped(self):
        d = os.path.join(self.root, "8")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("8 (broken\n")  # no closing paren: unparseable
        rows = self.p.list("dom0")
        self.assertEqual(rows, [])

    def test_stat_short_line_is_skipped(self):
        d = os.path.join(self.root, "9")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("9 (short) S 0 1\n")  # missing the rss field (index 21)
        rows = self.p.list("dom0")
        self.assertEqual(rows, [])

    def test_comm_may_contain_parens(self):
        # the name is the text between the FIRST '(' and the LAST ')'; a
        # comm with parens must survive the split
        d = os.path.join(self.root, "10")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "stat"), "w") as f:
            after = ["S", "0", "1", "1", "0", "-1", "4194562", "100", "0",
                     "0", "0", "5", "0", "1", "0", "20", "0", "1", "1234",
                     "50000000", "1000", "18446744073709551615", "0", "1"]
            f.write("10 (my (proc)) %s\n" % " ".join(after))
        rows = self.p.list("dom0")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "my (proc)")

    def test_rss_zero_pages_is_none(self):
        write_stat(self.root, 11, "nors", utime=0, stime=0, rss_pages=0)
        rows = self.p.list("dom0")
        self.assertIsNone(rows[0].rss_kib)  # no value: None, not 0 (D-4)

    def test_host_name_mismatch_is_none(self):
        write_stat(self.root, 1, "x", utime=1, stime=0)
        p = Dom0ProcProvider(proc_root=self.root, clock=self.clock, hz=100,
                            page_size=4096, host_name="other-host")
        self.assertIsNone(p.list("dom0"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
