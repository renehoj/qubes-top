"""Core sample model: unit tests (card t_c2120185, M1 gate).

Covers the card's acceptance list: first sample, counter reset mid-series,
``dt == 0``, deadband step detection (two identical reads -> one point),
mute detection at exact equality, ring eviction at capacity, qube removal,
and formatter rounding. Plus the documented semantics those cases sit on:
idle 0.0 is not a gap, >100% is not clamped, a mute gap does not move the
deadband reference, a reappearing qube starts with a fresh baseline.

Run standalone:

    python3 tests/test_core_sample_model.py

The sys.path shim below makes this module layout-independent: it works no
matter where it is run from, and does not require ``tests/`` to be an
importable package (run_tests.py executes modules as scripts, not via
``unittest discover``).
"""
import pathlib
import sys
import unittest

# sys.path shim: puts the repo root (the parent of tests/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.core import (  # noqa: E402
    DEADBAND_KIB,
    CpuPoint,
    MemPoint,
    MemState,
    SampleModel,
    SampleRecord,
    cpu_delta_pct,
    format_bytes,
    format_gib,
    format_percent,
    mem_report_state,
)

MiB = 1024  # KiB per MiB


def rec(name="app", domid=1, state="Running", cpu=0,
        assigned=1024 * MiB, mem_max=4096 * MiB, used=None, swap=None,
        ts=0.0):
    """A SampleRecord with sane defaults; callers override what they test."""
    return SampleRecord(name, domid, state, cpu, assigned, mem_max,
                       used, swap, ts)


class TestSampleRecord(unittest.TestCase):
    def test_fields_frozen_in_order(self):
        r = rec()
        self.assertEqual(
            (r.name, r.domid, r.state, r.cpu_time_ns, r.mem_assigned_kib,
             r.mem_max_kib, r.mem_used_kib, r.swap_used_kib, r.ts),
            ("app", 1, "Running", 0, 1024 * MiB, 4096 * MiB, None, None, 0.0))

    def test_record_is_frozen(self):
        r = rec()
        with self.assertRaises(Exception):
            r.ts = 1.0  # pyright: ignore[reportAttributeAccessIssue]


class TestCpuDelta(unittest.TestCase):
    def test_first_sample_has_no_value(self):
        m = SampleModel()
        m.ingest(rec(cpu=10 ** 9, ts=0.0))
        pts = m.cpu_series("app")
        self.assertEqual(len(pts), 1)
        self.assertIsNone(pts[0].pct)
        self.assertEqual(pts[0].ts, 0.0)
        self.assertEqual(pts[0], CpuPoint(0.0, None))

    def test_delta_formula_two_seconds(self):
        m = SampleModel()
        m.ingest(rec(cpu=0, ts=0.0))
        m.ingest(rec(cpu=5 * 10 ** 8, ts=2.0))
        pts = m.cpu_series("app")
        self.assertEqual(pts[1].pct, 25.0)  # 5e8 / 2e9 * 100

    def test_may_exceed_100(self):
        self.assertEqual(cpu_delta_pct(0, 2 * 10 ** 9, 0.0, 1.0), 200.0)
        m = SampleModel()
        m.ingest(rec(cpu=0, ts=0.0))
        m.ingest(rec(cpu=2 * 10 ** 9, ts=1.0))
        self.assertEqual(m.cpu_series("app")[1].pct, 200.0)

    def test_idle_is_zero_not_a_gap(self):
        # cur == prev with dt > 0 is a legitimate 0.0 -- not clamped away.
        self.assertEqual(cpu_delta_pct(10 ** 9, 10 ** 9, 0.0, 2.0), 0.0)

    def test_dt_zero_is_a_gap(self):
        self.assertIsNone(cpu_delta_pct(0, 10 ** 9, 2.0, 2.0))
        m = SampleModel()
        m.ingest(rec(cpu=0, ts=2.0))
        m.ingest(rec(cpu=10 ** 9, ts=2.0))  # dt == 0
        self.assertIsNone(m.cpu_series("app")[1].pct)

    def test_clock_regression_is_a_gap(self):
        self.assertIsNone(cpu_delta_pct(0, 10 ** 9, 2.0, 1.0))

    def test_counter_reset_mid_series_is_a_gap(self):
        m = SampleModel()
        m.ingest(rec(cpu=10 ** 9, ts=0.0))
        m.ingest(rec(cpu=2 * 10 ** 9, ts=2.0))   # 50.0
        m.ingest(rec(cpu=10 ** 8, ts=4.0))       # restart: counter fell
        m.ingest(rec(cpu=6 * 10 ** 8, ts=6.0))   # +5e8 ns over 2 s -> 25.0
        pts = m.cpu_series("app")
        self.assertEqual([p.pct for p in pts], [None, 50.0, None, 25.0])
        # The gap is a gap: never a negative, never a clamped zero.
        self.assertIsNone(pts[2].pct)
        # The restarted value became the baseline (25.0 = 5e8 / 2e9 * 100).
        self.assertEqual(pts[3].pct, 25.0)


class TestMemStateMachine(unittest.TestCase):
    def test_reported(self):
        self.assertIs(mem_report_state(100 * MiB, 1024 * MiB),
                      MemState.REPORTED)

    def test_mute_when_meminfo_absent(self):
        self.assertIs(mem_report_state(None, 1024 * MiB), MemState.MUTE)
        self.assertIs(mem_report_state(None, None), MemState.MUTE)

    def test_mute_at_exact_equality(self):
        # used == assigned to the KiB: qubesd's fallback signature.
        self.assertIs(mem_report_state(1024 * MiB, 1024 * MiB), MemState.MUTE)

    def test_off_by_one_kib_is_not_muted(self):
        self.assertIs(mem_report_state(1024 * MiB + 1, 1024 * MiB),
                      MemState.REPORTED)

    def test_unknown_when_assigned_unknown(self):
        self.assertIs(mem_report_state(100 * MiB, None), MemState.UNKNOWN)

    def test_mute_sample_never_emits_a_point(self):
        m = SampleModel()
        m.ingest(rec(used=None, ts=0.0))
        m.ingest(rec(used=1024 * MiB, assigned=1024 * MiB, ts=2.0))  # exact
        self.assertEqual(m.mem_state("app"), MemState.MUTE)
        self.assertEqual(m.mem_series("app"), [])  # no synthesised 100%

    def test_reported_state_follows_samples(self):
        m = SampleModel()
        m.ingest(rec(used=512 * MiB, ts=0.0))
        self.assertIs(m.mem_state("app"), MemState.REPORTED)
        m.ingest(rec(used=None, ts=2.0))
        self.assertIs(m.mem_state("app"), MemState.MUTE)
        m.ingest(rec(used=512 * MiB, ts=4.0))
        self.assertIs(m.mem_state("app"), MemState.REPORTED)


class TestDeadband(unittest.TestCase):
    def test_two_identical_reads_one_point(self):
        m = SampleModel()
        m.ingest(rec(used=1000 * MiB, ts=0.0))
        m.ingest(rec(used=1000 * MiB, ts=2.0))
        pts = m.mem_series("app")
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0].ts, 0.0)  # timestamp of the first observation

    def test_first_reported_sample_always_points(self):
        m = SampleModel()
        m.ingest(rec(used=100 * MiB, ts=0.0))
        self.assertEqual(len(m.mem_series("app")), 1)

    def test_sub_deadband_movement_emits_no_point(self):
        m = SampleModel()
        m.ingest(rec(used=1000 * MiB, ts=0.0))
        m.ingest(rec(used=1029 * MiB, ts=2.0))  # 29 MiB < 30 MiB deadband
        self.assertEqual(len(m.mem_series("app")), 1)

    def test_deadband_step_emits_a_point_at_observed_change_ts(self):
        m = SampleModel()
        m.ingest(rec(used=1000 * MiB, ts=0.0))
        m.ingest(rec(used=1029 * MiB, ts=2.0))  # no point (sub-deadband)
        m.ingest(rec(used=1058 * MiB, ts=4.0))  # 58 MiB from the confirmed
        # level (1000): a step, timestamped when observed
        pts = m.mem_series("app")
        self.assertEqual(len(pts), 2)
        self.assertEqual(pts[1].ts, 4.0)
        self.assertEqual(pts[1].used_kib, 1058 * MiB)

    def test_mute_gap_does_not_move_the_reference(self):
        m = SampleModel()
        m.ingest(rec(used=1000 * MiB, ts=0.0))
        m.ingest(rec(used=None, ts=2.0))               # meminfo absent
        m.ingest(rec(used=1000 * MiB, ts=4.0))        # back to the old level
        self.assertEqual(len(m.mem_series("app")), 1)  # no spurious step

    def test_fallback_value_never_becomes_the_reference(self):
        m = SampleModel()
        m.ingest(rec(used=1000 * MiB, assigned=2000 * MiB, ts=0.0))
        m.ingest(rec(used=2000 * MiB, assigned=2000 * MiB, ts=2.0))  # mute
        m.ingest(rec(used=1000 * MiB, ts=4.0))        # no new point: the
        # fallback value must not have become the deadband reference
        self.assertEqual(len(m.mem_series("app")), 1)

    def test_swap_is_carried_by_the_point(self):
        m = SampleModel()
        m.ingest(rec(used=1000 * MiB, swap=50 * MiB, ts=0.0))
        self.assertEqual(m.mem_series("app")[0],
                         MemPoint(0.0, 1000 * MiB, 50 * MiB))

    def test_deadband_constant_is_30_mib(self):
        self.assertEqual(DEADBAND_KIB, 30 * 1024)


class TestRings(unittest.TestCase):
    def test_eviction_at_capacity_cpu(self):
        m = SampleModel(ring_capacity=4)
        # deltas per 2 s tick: 0, 1e8, 2e8, 3e8, 4e8, 5e8 -> pcts
        # None, 5.0, 10.0, 15.0, 20.0, 25.0
        cpu = 0
        for i, delta in enumerate([0, 1, 2, 3, 4, 5], 1):
            cpu += delta * 10 ** 8
            m.ingest(rec(cpu=cpu, ts=2.0 * i))
        pts = m.cpu_series("app")
        # 6 points pushed into a 4-slot ring: the two oldest are gone
        # (samples land at ts 2,4,6,8,10,12).
        self.assertEqual(len(pts), 4)
        self.assertEqual([p.pct for p in pts], [10.0, 15.0, 20.0, 25.0])
        # oldest first, newest last, in order
        self.assertEqual([p.ts for p in pts], [6.0, 8.0, 10.0, 12.0])

    def test_eviction_at_capacity_mem(self):
        m = SampleModel(ring_capacity=3)
        for i in range(5):
            m.ingest(rec(used=(1000 + 100 * (i + 1)) * MiB, ts=2.0 * i))
        pts = m.mem_series("app")
        self.assertEqual(len(pts), 3)
        self.assertEqual([p.used_kib for p in pts],
                         [1300 * MiB, 1400 * MiB, 1500 * MiB])

    def test_default_capacity_is_150(self):
        m = SampleModel()
        for i in range(160):
            m.ingest(rec(cpu=i * 10 ** 8, ts=2.0 * i))
        self.assertEqual(len(m.cpu_series("app")), 150)

    def test_invalid_capacity_rejected(self):
        with self.assertRaises(ValueError):
            SampleModel(ring_capacity=0)


class TestQubeRemovalAndRetention(unittest.TestCase):
    def test_qube_removal_drops_buffers(self):
        m = SampleModel()
        m.ingest(rec(name="app", ts=0.0))
        m.ingest(rec(name="work", ts=0.0))
        m.synchronize({"work"})
        self.assertFalse(m.has("app"))
        self.assertEqual(m.cpu_series("app"), [])
        self.assertEqual(m.mem_series("app"), [])
        self.assertIsNone(m.mem_state("app"))
        # a still-present qube is untouched
        self.assertTrue(m.has("work"))
        self.assertEqual(len(m.cpu_series("work")), 1)

    def test_reappeared_qube_starts_with_a_fresh_baseline(self):
        m = SampleModel()
        m.ingest(rec(name="app", cpu=5 * 10 ** 9, ts=0.0))  # big counter
        m.synchronize(set())
        m.ingest(rec(name="app", cpu=10 ** 8, ts=100.0))   # restarted
        pts = m.cpu_series("app")
        # first sample again: no value; the stale 5e9 baseline was dropped,
        # so no negative/reset artefact can leak from the old owner
        self.assertEqual(len(pts), 1)
        self.assertIsNone(pts[0].pct)

    def test_buffers_retained_across_selection_changes(self):
        # The model has no selection state: querying any names (or none)
        # must not disturb the buffers.
        m = SampleModel()
        m.ingest(rec(name="app", used=100 * MiB, ts=0.0))
        m.cpu_series("does-not-exist")
        m.mem_series("other")
        m.mem_state("other")
        self.assertEqual(len(m.cpu_series("app")), 1)
        self.assertEqual(len(m.mem_series("app")), 1)


class TestFormatters(unittest.TestCase):
    def test_bytes_mib(self):
        self.assertEqual(format_bytes(1.5 * 2 ** 20), "1.5 MiB")
        self.assertEqual(format_bytes(0), "0.0 MiB")

    def test_bytes_gib_threshold(self):
        self.assertEqual(format_bytes(2 ** 30), "1.0 GiB")
        self.assertEqual(format_bytes(5 * 2 ** 30), "5.0 GiB")
        # just under the threshold: rounds up in display, still MiB
        self.assertEqual(format_bytes(2 ** 30 - 1), "1024.0 MiB")

    def test_gib_always_gib(self):
        # KiB in, a GiB number out, at every magnitude: the row's memory cell
        # shows used / assigned in ONE unit, so a 400 MiB qube reads "0.39",
        # not "400.0 MiB" next to a "1.0 GiB" assigned.
        self.assertEqual(format_gib(1024 * 1024), "1.0")
        self.assertEqual(format_gib(1536 * 1024), "1.5")
        self.assertEqual(format_gib(4 * 1024 * 1024), "4.0")
        self.assertEqual(format_gib(400 * 1024), "0.39")

    def test_gib_never_renders_a_small_value_as_zero(self):
        # one decimal would print a 40 MiB qube as "0.0" — a wrong value.
        self.assertEqual(format_gib(40 * 1024), "0.04")
        self.assertEqual(format_gib(0), "0.00")

    def test_percent_one_decimal(self):
        self.assertEqual(format_percent(42.5), "42.5%")
        self.assertEqual(format_percent(200.0), "200.0%")
        self.assertEqual(format_percent(0.0), "0.0%")

    def test_percent_rounding_is_deterministic(self):
        # binary-exact halves: f-formatting rounds them half-even
        self.assertEqual(format_percent(0.25), "0.2%")
        self.assertEqual(format_percent(0.75), "0.8%")
        self.assertEqual(format_percent(1.25), "1.2%")


if __name__ == "__main__":
    unittest.main(verbosity=2)
