"""Row data / row spec: unit tests (card t_4d9516cd, M3 gate).

``make_row_spec`` / ``RowSpec.refresh_from`` build the display strings from the
core formatters (never recomputing metric arithmetic here) and the per-qube
bounds from ``own_bounds``. The card's "a gap shows —, never a negative or a
clamped zero" and "D-4: never synthesise 100%" are pinned here. ``dom0`` is
always ``REPORTED`` and rendered like any other row.

Run standalone:

    python3 tests/test_ui_rowspec.py

The sys.path shim below makes this module layout-independent.
"""
import pathlib
import sys
import unittest
from typing import Optional

# sys.path shim: puts the repo root (the parent of tests/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.core import MemState, SampleRecord  # noqa: E402
from qubes_top.ui.rowspec import (  # noqa: E402
    NO_VALUE,
    RowData,
    _mem_text,
    dom0_rowdata,
    make_row_spec,
)

MiB = 1024  # KiB per MiB


def rec(name="test-vm", state="Running", used: Optional[int] = None,
        assigned: int = 4 * MiB, max_kib: Optional[int] = None,
        ts=1.0) -> SampleRecord:
    """A record. ``assigned`` is the LIVE allocation, ``max_kib`` the qube's
    configured memory (defaults to ``assigned``, as for a qube that is not
    ballooned)."""
    mem_max = assigned if max_kib is None else max_kib
    return SampleRecord(
        name=name, domid=1, state=state, cpu_time_ns=100,
        mem_assigned_kib=assigned, mem_max_kib=mem_max,
        mem_used_kib=used, swap_used_kib=None, ts=ts)


def rd(name, record, state=MemState.REPORTED,
      cpu=(None, 12.0), mem=((1.0, 100), (2.0, 200))) -> RowData:
    return RowData(name=name, record=record, mem_state=state,
                  cpu_values=tuple(cpu), mem_points=tuple(mem))


class TestMakeRowSpec(unittest.TestCase):
    def test_state_and_color(self):
        s = make_row_spec(rd("a", rec()), lambda n: "#aabbcc")
        self.assertEqual(s.state, "Running")
        self.assertEqual(s.color, "#aabbcc")

    def test_cpu_text_is_latest_non_none(self):
        s = make_row_spec(rd("a", rec(), cpu=(None, 5.0, 7.5)), lambda n: "#000")
        self.assertEqual(s.cpu_text, "7.5%")

    def test_cpu_text_gap_is_placeholder(self):
        s = make_row_spec(rd("a", rec(), cpu=(None, None)), lambda n: "#000")
        self.assertEqual(s.cpu_text, NO_VALUE)

    def test_trailing_gap_uses_prior_value(self):
        # a trailing gap (last sample a reset) shows the prior value, not "—"
        s = make_row_spec(rd("a", rec(), cpu=(10.0, None)), lambda n: "#000")
        self.assertEqual(s.cpu_text, "10.0%")

    def test_mem_text_reported(self):
        # GiB always, one unit for the pair: a 400 MiB used / 1 GiB assigned
        # qube reads "0.39 / 1.0 GiB", not "400.0 MiB / 1.0 GiB".
        s = make_row_spec(rd("a", rec(used=400 * MiB, assigned=1024 * MiB)),
                         lambda n: "#000")
        self.assertEqual(s.mem_text, "0.39 / 1.0 GiB")

    def test_mem_text_mute_not_reported(self):
        # a dash, not the prose: the detail pane spells the state out, the
        # numeric cell stays narrow (the rows share one column width)
        s = make_row_spec(rd("a", rec(used=None, assigned=1024 * MiB),
                             MemState.MUTE), lambda n: "#000")
        self.assertEqual(s.mem_text, "\u2014 / 1.0 GiB")

    def test_mem_text_unknown(self):
        s = make_row_spec(rd("a", rec(used=None, assigned=1024 * MiB),
                             MemState.UNKNOWN), lambda n: "#000")
        self.assertEqual(s.mem_text, "? / 1.0 GiB")

    def test_mem_text_uses_the_configured_max_not_the_live_balloon(self):
        # The owner's report, 2026-10-18: a qube configured at 4000 MiB,
        # currently ballooned to 1000 MiB, guest reporting 1.1 GiB used. The
        # cell must read 1.1 / 3.9 GiB — dividing by the live allocation made
        # it read "1.1 / 1.0", i.e. more than full, and looked like a units bug.
        s = make_row_spec(
            rd("browser-streaming",
               rec(used=1126 * MiB, assigned=1000 * MiB, max_kib=4000 * MiB)),
            lambda n: "#000")
        self.assertEqual(s.mem_text, "1.1 / 3.9 GiB")

    def test_mem_text_unknown_assigned(self):
        # an absent assigned value is "?", never a synthesised number. The row
        # cell receives the two halves directly here: a SampleRecord always
        # carries an assigned value (D-2), so this stays a formatter test.
        self.assertEqual(_mem_text(400 * MiB, None, MemState.REPORTED),
                         "0.39 / ? GiB")

    def test_cpu_bounds_from_own_bounds(self):
        s = make_row_spec(rd("a", rec(), cpu=(0.0, 50.0),
                            mem=((1.0, 100), (2.0, 400))), lambda n: "#000")
        self.assertEqual(s.cpu_bounds, (0.0, 50.0))

    def test_mem_bounds_use_the_qube_ceiling(self):
        # 4 MiB assigned/max: the sparkline's range is 0..4 MiB, so a steady
        # 1 MiB qube draws low, not pinned to the top, and the scale does not
        # jump when a new maximum appears.
        s = make_row_spec(rd("a", rec(used=1 * MiB, assigned=4 * MiB),
                            mem=((1.0, 1 * MiB), (2.0, 1 * MiB))),
                         lambda n: "#000")
        self.assertEqual(s.mem_bounds, (0.0, 4.0 * MiB))

    def test_mem_bounds_fall_back_to_the_observed_max(self):
        # no max/assigned reported: the series itself is the only ceiling
        s = make_row_spec(rd("a", rec(used=1 * MiB, assigned=0),
                            mem=((1.0, 100), (2.0, 400))), lambda n: "#000")
        self.assertEqual(s.mem_bounds, (0.0, 400.0))

    def test_series_stored(self):
        s = make_row_spec(rd("a", rec(), cpu=(1.0, 2.0),
                            mem=((1.0, 10), (2.0, 20))), lambda n: "#000")
        self.assertEqual(s.cpu_values, [1.0, 2.0])
        self.assertEqual(s.mem_points, [(1.0, 10), (2.0, 20)])


class TestGraphTopLabels(unittest.TestCase):
    """The scale label inside each sparkline (D-21).

    Owner: *"if there was a center text with the highest value from the data
    set ... if it says 10% it would mean the graph currently has 0-10 as the
    interval"*. The label is the **bound the graph is drawn against**, which is
    what makes the per-row auto-scaling legible: the same squiggle means a
    different thing on every row, and only this number says which.
    """

    def test_the_cpu_label_is_the_series_maximum(self):
        s = make_row_spec(rd("a", rec(), cpu=(None, 2.0, 4.0)),
                         lambda n: "#000")
        self.assertEqual(s.cpu_bounds, (0.0, 4.0))
        self.assertEqual(s.cpu_top_text, "4.0%")

    def test_the_cpu_label_follows_a_new_maximum(self):
        # the point of the label: the interval moves, and it says so
        s = make_row_spec(rd("a", rec(), cpu=(1.0, 2.0)), lambda n: "#000")
        self.assertEqual(s.cpu_top_text, "2.0%")
        s.refresh_from(rd("a", rec(), cpu=(1.0, 2.0, 400.0)), lambda n: "#000")
        self.assertEqual(s.cpu_top_text, "400.0%")

    def test_no_cpu_data_means_no_label(self):
        # an empty graph has no interval to describe
        for cpu in ((), (None,), (None, None)):
            s = make_row_spec(rd("a", rec(), cpu=cpu), lambda n: "#000")
            self.assertEqual(s.cpu_top_text, "", cpu)

    def test_a_flat_zero_series_states_the_interval_it_is_actually_drawn_on(self):
        # own_bounds widens a degenerate range to 0..1 so the geometry never
        # divides by zero; the label says *that*, because that is the interval
        # on screen (a flat line at the bottom of a 0-1 range)
        s = make_row_spec(rd("a", rec(), cpu=(0.0, 0.0)), lambda n: "#000")
        self.assertEqual(s.cpu_bounds, (0.0, 1.0))
        self.assertEqual(s.cpu_top_text, "1.0%")

    def test_the_memory_label_is_the_ceiling_not_the_observed_maximum(self):
        # the step series tops out at 1 MiB in a 4 MiB qube: labelling "1.0
        # MiB" over a graph drawn 0..4 MiB would describe an interval that is
        # not on screen
        s = make_row_spec(rd("a", rec(used=1 * MiB, assigned=4 * MiB),
                            mem=((1.0, 1 * MiB), (2.0, 1 * MiB))),
                         lambda n: "#000")
        self.assertEqual(s.mem_top_text, "4.0 MiB")

    def test_the_memory_label_uses_gib_above_a_gib(self):
        s = make_row_spec(rd("a", rec(used=1 * MiB, assigned=4000 * MiB),
                            mem=((1.0, 1 * MiB),)),
                         lambda n: "#000")
        self.assertEqual(s.mem_top_text, "3.9 GiB")

    def test_no_memory_points_means_no_label(self):
        s = make_row_spec(rd("a", rec(), mem=()), lambda n: "#000")
        self.assertEqual(s.mem_top_text, "")

    def test_the_memory_ceiling_falls_back_to_the_observed_maximum(self):
        # no max and no allocation: the series itself is the only ceiling, and
        # the label reports it in the same units as everywhere else (the
        # series is in KiB — 400 KiB is 0.4 MiB, not "400.0 MiB")
        s = make_row_spec(rd("a", rec(used=1 * MiB, assigned=0),
                            mem=((1.0, 100), (2.0, 400))), lambda n: "#000")
        self.assertEqual(s.mem_bounds, (0.0, 400.0))
        self.assertEqual(s.mem_top_text, "0.4 MiB")


class TestRefreshInPlace(unittest.TestCase):
    def test_refresh_updates_same_object(self):
        s = make_row_spec(rd("a", rec(state="Running"),
                            cpu=(1.0, 2.0)), lambda n: "#000")
        first = s
        s.refresh_from(rd("a", rec(state="Halted"),
                          cpu=(3.0, 4.0)), lambda n: "#000")
        self.assertIs(first, s)  # in place, never recreated
        self.assertEqual(s.state, "Halted")
        self.assertEqual(s.cpu_text, "4.0%")


class TestDom0Rowdata(unittest.TestCase):
    def test_reported_and_name(self):
        d = dom0_rowdata(rec("dom0"), [None, 3.0], [(1.0, 100)])
        self.assertEqual(d.name, "dom0")
        self.assertIs(d.mem_state, MemState.REPORTED)
        self.assertEqual(d.cpu_values, (None, 3.0))
        self.assertEqual(d.mem_points, ((1.0, 100),))


if __name__ == "__main__":
    unittest.main(verbosity=2)
