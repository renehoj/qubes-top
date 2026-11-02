"""Disk and network I/O of the selected qube (D-27).

Pinned off-box: the blkback file layout and the sector unit, the vif read's
argv (no shell) and its rx/tx swap to the qube's side, the D-6 check on the
netvm's numbers, the counter-to-rate rules (a reset or a new domid is a gap),
and the one-value-per-tick history the charts draw.

Run standalone:

    python3 tests/test_ui_iostats.py
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.core import format_rate, rate_unit  # noqa: E402
from qubes_top.ui.guestproc import GuestProcPoller  # noqa: E402
from qubes_top.ui.iostats import (  # noqa: E402
    VBD_ROOT,
    IoHistory,
    NetIoProvider,
    RateMeter,
    parse_vif,
    read_vbd_bytes,
    vif_argv,
)


def _sysfs(files):
    """A fake ``listdir`` / ``read`` pair over ``{path: text}``."""
    def listdir(root):
        names = set()
        for path in files:
            if path.startswith(root + "/"):
                names.add(path[len(root) + 1:].split("/")[0])
        return sorted(names)

    def read(path):
        return files.get(path)
    return listdir, read


def _vbd(domid, devid, rd, wr):
    stats = "%s/vbd-%d-%d/statistics/" % (VBD_ROOT, domid, devid)
    return {stats + "rd_sect": "%d\n" % rd, stats + "wr_sect": "%d\n" % wr}


class TestReadVbd(unittest.TestCase):
    def test_sums_every_disk_of_the_domain_in_bytes(self):
        files = {}
        files.update(_vbd(5, 51712, 100, 10))
        files.update(_vbd(5, 51728, 20, 2))
        files.update(_vbd(55, 51712, 9999, 9999))  # vbd-55-… is not vbd-5-…
        listdir, read = _sysfs(files)
        self.assertEqual(read_vbd_bytes(5, listdir=listdir, read=read),
                         (120 * 512, 12 * 512))

    def test_no_disk_is_none(self):
        listdir, read = _sysfs(_vbd(7, 51712, 1, 1))
        self.assertIsNone(read_vbd_bytes(5, listdir=listdir, read=read))
        self.assertIsNone(read_vbd_bytes(-1, listdir=listdir, read=read))

    def test_a_partial_sum_is_refused(self):
        files = {}
        files.update(_vbd(5, 51712, 100, 10))
        files.update(_vbd(5, 51728, 20, 2))
        del files["%s/vbd-5-51728/statistics/wr_sect" % VBD_ROOT]
        listdir, read = _sysfs(files)
        self.assertIsNone(read_vbd_bytes(5, listdir=listdir, read=read))

    def test_a_missing_bus_is_none(self):
        def listdir(root):
            raise FileNotFoundError(root)
        self.assertIsNone(read_vbd_bytes(5, listdir=listdir,
                                         read=lambda p: None))


class TestVif(unittest.TestCase):
    def test_argv_has_no_shell_and_names_the_domids_vif(self):
        argv = vif_argv("sys-firewall", 12)
        self.assertEqual(argv[:6], ["qvm-run", "-q", "-p", "--no-shell",
                                    "sys-firewall", "cat"])
        self.assertEqual(argv[6:], [
            "/sys/class/net/vif12.0/statistics/rx_bytes",
            "/sys/class/net/vif12.0/statistics/tx_bytes"])

    def test_parse_swaps_to_the_qubes_side(self):
        # backend rx = what the qube sent; backend tx = what it received
        self.assertEqual(parse_vif("1000\n50000\n"), (50000, 1000))

    def test_parse_rejects_anything_but_two_counters(self):
        for text in ("", "1\n", "1\n2\n3\n", "1\n-2\n", "1\n2x\n",
                     "1\n" + "9" * 21 + "\n", None):
            self.assertIsNone(parse_vif(text), repr(text))

    def test_provider_reads_and_notes_failures(self):
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return 0, "10\n20\n", ""
        provider = NetIoProvider(runner=runner, clock=lambda: 42.0)
        reading = provider.read(("sys-firewall", 3))
        self.assertEqual((reading.counters, reading.ts), ((20, 10), 42.0))
        self.assertEqual(calls, [vif_argv("sys-firewall", 3)])

        failing = NetIoProvider(
            runner=lambda a, t: (1, "", "cat: no such file\n"))
        reading = failing.read(("sys-firewall", 3))
        self.assertIsNone(reading.counters)
        self.assertIn("no such file", reading.note)

    def test_provider_refuses_a_bad_target_without_running(self):
        def runner(argv, timeout):
            raise AssertionError("must not run")
        provider = NetIoProvider(runner=runner)
        for target in (("-x", 3), ("sys-firewall", -1), ("a b", 3)):
            self.assertIsNone(provider.read(target).counters)

    def test_the_poller_takes_a_netvm_target(self):
        posted = []
        provider = NetIoProvider(runner=lambda a, t: (0, "1\n2\n", ""))
        poller = GuestProcPoller(provider, emit=None,
                                 post=lambda f, *a: posted.append(a),
                                 interval=2.0)
        poller.set_target(("sys-firewall", 4))
        self.assertTrue(poller.poll_once(10.0))
        self.assertFalse(poller.poll_once(11.0), "not before the interval")
        self.assertEqual(posted[0][0], ("sys-firewall", 4))


class TestRateMeter(unittest.TestCase):
    def test_rate_since_the_previous_reading(self):
        m = RateMeter()
        self.assertIsNone(m.feed("a", 0.0, (0, 0)), "no baseline yet")
        self.assertEqual(m.feed("a", 2.0, (2048, 100)), (1024.0, 50.0))

    def test_a_decrease_a_new_key_or_no_reading_is_a_gap(self):
        m = RateMeter()
        m.feed("a", 0.0, (100, 100))
        self.assertIsNone(m.feed("a", 2.0, (50, 200)), "counter reset")
        self.assertEqual(m.feed("a", 4.0, (60, 200)), (5.0, 0.0))
        self.assertIsNone(m.feed("b", 6.0, (70, 200)), "another qube/domid")
        self.assertIsNone(m.feed("b", 8.0, None))
        self.assertIsNone(m.feed("b", 10.0, (80, 200)),
                          "an unreadable tick drops the baseline")


class TestIoHistory(unittest.TestCase):
    def test_one_value_per_push_none_for_a_gap(self):
        h = IoHistory()
        h.push("a", (1.0, 2.0), None)
        h.push("a", None, (3.0, 4.0))
        self.assertEqual(h.disk_read, [1.0, None])
        self.assertEqual(h.disk_write, [2.0, None])
        self.assertEqual(h.net_in, [None, 3.0])
        self.assertEqual(h.net_out, [None, 4.0])

    def test_a_new_key_starts_over(self):
        h = IoHistory()
        h.push("a", (1.0, 2.0), (3.0, 4.0))
        h.disk_note = "x"
        h.push("b", (5.0, 6.0), None)
        self.assertEqual(h.disk_read, [5.0])
        self.assertEqual(h.disk_note, "")

    def test_bounded_to_the_window(self):
        h = IoHistory(capacity=3)
        for i in range(5):
            h.push("a", (float(i), 0.0), None)
        self.assertEqual(h.disk_read, [2.0, 3.0, 4.0])


class TestRateFormatting(unittest.TestCase):
    def test_format_rate_goes_down_to_bytes(self):
        self.assertEqual(format_rate(300), "300 B/s")
        self.assertEqual(format_rate(1536), "1.5 KiB/s")
        self.assertEqual(format_rate(12 * 2 ** 20), "12.0 MiB/s")
        self.assertEqual(format_rate(2 ** 30), "1.0 GiB/s")

    def test_rate_unit(self):
        self.assertEqual(rate_unit(500), (1.0, "B"))
        self.assertEqual(rate_unit(4096), (1024.0, "K"))
        self.assertEqual(rate_unit(3 * 2 ** 20), (2.0 ** 20, "M"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
