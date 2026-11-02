"""Core pure geometry + colour: unit tests (card t_4d9516cd, M3 gate).

The left-pane widget does no metric arithmetic — the core owns the layout
arithmetic. These tests pin the core geometry the widget renders from:
``normalize`` (clamp + degenerate range), ``own_bounds`` (per-qube bounds with
a floor), the fixed history window every graph draws against (D-24:
``window_x``, ``window_segments`` split on None gaps, ``window_steps`` for the
memory step path), the y-axis ticks (``axis_ticks``, ``format_tick``), and
``label_color`` (deterministic stable hex).

Run standalone:

    python3 tests/test_ui_core_geometry.py

The sys.path shim below makes this module layout-independent.
"""
import pathlib
import sys
import unittest

# sys.path shim: puts the repo root (the parent of tests/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.core import (  # noqa: E402
    HISTORY_WINDOW_S,
    TICK_S,
    axis_ticks,
    format_tick,
    label_color,
    normalize,
    own_bounds,
    window_segments,
    window_steps,
    window_x,
)


class TestNormalize(unittest.TestCase):
    def test_midpoint(self):
        self.assertEqual(normalize(5.0, 0.0, 10.0), 0.5)

    def test_clamps_high(self):
        self.assertEqual(normalize(15.0, 0.0, 10.0), 1.0)

    def test_clamps_low(self):
        self.assertEqual(normalize(-5.0, 0.0, 10.0), 0.0)

    def test_degenerate_range_is_zero(self):
        self.assertEqual(normalize(5.0, 5.0, 5.0), 0.0)

    def test_zero_to_one(self):
        self.assertEqual(normalize(0.0, 0.0, 1.0), 0.0)
        self.assertEqual(normalize(1.0, 0.0, 1.0), 1.0)


class TestOwnBounds(unittest.TestCase):
    def test_zero_floor(self):
        self.assertEqual(own_bounds([370.0, 400.0]), (0.0, 400.0))

    def test_single(self):
        self.assertEqual(own_bounds([0.0]), (0.0, 1.0))

    def test_empty(self):
        self.assertEqual(own_bounds([]), (0.0, 1.0))

    def test_all_none(self):
        self.assertEqual(own_bounds([None, None]), (0.0, 1.0))

    def test_floor_clamps_below(self):
        self.assertEqual(own_bounds([5.0, 8.0], floor=3.0), (3.0, 8.0))

    def test_floor_above_max_is_clamped_and_widened(self):
        # floor above every value: hi is clamped to lo (never hi < lo), and the
        # degenerate range is widened by 1.0 so the geometry never divides by
        # zero. The *normal* case (floor below the max) is what keeps a 30 MiB
        # step on a 400 MiB qube visible (the card's "own maximum" requirement).
        self.assertEqual(own_bounds([10.0, 20.0], floor=100.0), (100.0, 101.0))

    def test_ignores_none_in_max(self):
        self.assertEqual(own_bounds([None, 12.0, 8.0]), (0.0, 12.0))


class TestWindowX(unittest.TestCase):
    def test_now_is_the_right_edge(self):
        self.assertAlmostEqual(window_x(0.0, 100, 2), 98.0)

    def test_the_window_start_is_the_left_edge(self):
        self.assertAlmostEqual(window_x(HISTORY_WINDOW_S, 100, 2), 2.0)

    def test_half_way(self):
        self.assertAlmostEqual(window_x(HISTORY_WINDOW_S / 2, 100, 2), 50.0)

    def test_older_than_the_window_clamps(self):
        self.assertAlmostEqual(window_x(10 * HISTORY_WINDOW_S, 100, 2), 2.0)


class TestWindowSegments(unittest.TestCase):
    def test_split_on_none(self):
        segs = window_segments([None, 10.0, 20.0, None, 30.0], 100, 40,
                               0.0, 40.0)
        self.assertEqual(len(segs), 2)
        self.assertEqual(len(segs[0]), 2)  # 10 -> 20
        self.assertEqual(len(segs[1]), 1)  # 30 alone

    def test_newest_value_is_at_now(self):
        segs = window_segments([1.0, 2.0, 3.0], 100, 40, 0.0, 3.0)
        self.assertAlmostEqual(segs[-1][-1][0], 98.0)

    def test_fixed_scale_does_not_stretch_a_short_series(self):
        # three ticks are 4 s of history: they sit at the right edge, not
        # spread over the width (the stretch is what made a filling graph
        # unreadable)
        segs = window_segments([1.0, 2.0, 3.0], 100, 40, 0.0, 3.0)
        first_x = segs[0][0][0]
        self.assertAlmostEqual(first_x, window_x(2 * TICK_S, 100, 2))
        self.assertGreater(first_x, 90.0)

    def test_values_older_than_the_window_are_dropped(self):
        n = int(HISTORY_WINDOW_S / TICK_S) + 10
        segs = window_segments([1.0] * n, 100, 40, 0.0, 1.0)
        self.assertEqual(sum(len(s) for s in segs),
                         int(HISTORY_WINDOW_S / TICK_S) + 1)

    def test_empty(self):
        self.assertEqual(window_segments([], 100, 40, 0.0, 1.0), [])

    def test_y_is_inverted(self):
        segs = window_segments([0.0, 5.0], 100, 40, 0.0, 5.0)
        self.assertAlmostEqual(segs[0][0][1], 38.0)  # lo -> bottom
        self.assertAlmostEqual(segs[0][1][1], 2.0)   # hi -> top


class TestWindowSteps(unittest.TestCase):
    NOW = 1000.0

    def test_empty(self):
        self.assertEqual(window_steps([], self.NOW, 100, 40, 0.0, 1.0), [])

    def test_single_point_is_a_flat_line_across_the_width(self):
        # One point is a qube whose memory has not moved: a level, drawn
        # across the whole window, never a dot (the owner saw the dot).
        for ts in (self.NOW - 10.0, self.NOW - 2 * HISTORY_WINDOW_S):
            verts = window_steps([(ts, 0.5)], self.NOW, 100, 40, 0.0, 1.0)
            self.assertAlmostEqual(verts[0][0], 2.0)
            self.assertAlmostEqual(verts[-1][0], 98.0)
            self.assertEqual({round(y, 6) for _x, y in verts}, {20.0})

    def test_a_change_jumps_at_its_time(self):
        verts = window_steps([(self.NOW - 200.0, 0.0),
                              (self.NOW - 60.0, 1.0)],
                             self.NOW, 100, 40, 0.0, 1.0)
        x_change = window_x(60.0, 100, 2)
        self.assertIn((x_change, 38.0), verts)  # held to the change
        self.assertIn((x_change, 2.0), verts)   # then jumped
        self.assertEqual(verts[-1], (98.0, 2.0))  # the new level holds to now

    def test_level_before_the_window_enters_at_the_left(self):
        verts = window_steps([(self.NOW - 900.0, 1.0),
                              (self.NOW - 600.0, 0.0),
                              (self.NOW - 30.0, 1.0)],
                             self.NOW, 100, 40, 0.0, 1.0)
        self.assertEqual(verts[0], (2.0, 38.0))  # the 0.0 level at t0

    def test_x_within_bounds(self):
        verts = window_steps([(0.0, 0.0), (self.NOW, 1.0)], self.NOW,
                             100, 40, 0.0, 1.0)
        for x, _y in verts:
            self.assertGreaterEqual(x, 2.0)
            self.assertLessEqual(x, 98.0)


class TestAxisTicks(unittest.TestCase):
    def test_one_hundred_percent(self):
        self.assertEqual(axis_ticks(100.0),
                         (100.0, [0.0, 25.0, 50.0, 75.0, 100.0]))

    def test_top_rounds_up_to_a_whole_step(self):
        top, ticks = axis_ticks(150.5)
        self.assertEqual(top, 200.0)
        self.assertEqual(ticks, [0.0, 50.0, 100.0, 150.0, 200.0])

    def test_never_more_than_target_intervals(self):
        for hi in (0.05, 0.39, 1.0, 3.7, 12.0, 99.0, 101.0, 777.0, 4096.0):
            top, ticks = axis_ticks(hi)
            self.assertGreaterEqual(top, hi)
            self.assertLessEqual(len(ticks) - 1, 4, hi)
            self.assertEqual(ticks[0], 0.0)

    def test_non_positive_is_one(self):
        self.assertEqual(axis_ticks(0.0)[0], 1.0)

    def test_format_tick(self):
        self.assertEqual(format_tick(100.0), "100")
        self.assertEqual(format_tick(2.5), "2.5")
        self.assertEqual(format_tick(0.30000000000000004), "0.3")
        self.assertEqual(format_tick(0.0), "0")


class TestLabelColor(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(label_color("sys-firewall"), label_color("sys-firewall"))

    def test_stable_known_values(self):
        # pinned so a change to the hash/derivation is caught, not silent
        self.assertEqual(label_color("a"), "#f1c40f")
        self.assertEqual(label_color("b"), "#e67e22")
        self.assertEqual(label_color("dom0"), "#2980b9")

    def test_hex_format(self):
        c = label_color("anything")
        self.assertRegex(c, r"^#[0-9a-f]{6}$")

    def test_different_names_differ(self):
        # not a hard contract (crc32 can collide), but for these two names the
        # derivation is stable and distinct — pin the pair.
        self.assertNotEqual(label_color("a"), label_color("b"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
