"""The left pane's table layout: the column table is the single source.

The bug this pins: the list used to be a ``Gtk.Box`` per row with a header
holding two buttons (CPU before Name) that lined up with nothing, and no
memory or graph titles at all. Columns are now described once, in
``qubes_top.ui.layout.COLUMNS``, and both the rows and the header build from it.

The width budget test is the one that would have caught the *visible* problem
on dom0: the six columns' minimums plus the gaps must fit the default divider
position minus a scrollbar, or the list opens with a horizontal scrollbar and
the graphs are off-screen (the old layout needed ~704 px for a ~546 px
viewport). Pure — no gi required.

Run standalone:

    python3 tests/test_ui_layout.py
"""
import pathlib
import sys
import unittest

# sys.path shim: puts the repo root (the parent of tests/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.ui.config import DEFAULT_POSITION  # noqa: E402
from qubes_top.ui.layout import (  # noqa: E402
    ARROW_WIDTH,
    COLUMNS,
    DOT_GAP,
    DOT_SIZE,
    PROC_COLUMNS,
    SPACING,
    GRAPH_HEIGHT,
    LIST_MIN_ROWS,
    RESIZE_GRIP,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
    column,
    edge_at,
    fixed_columns,
    list_min_height,
)

#: A vertical scrollbar plus the scroller's own frame, in pixels. The header
#: reserves the real width at run time; this is the estimate the width budget
#: allows for.
_SCROLLBAR_ALLOWANCE = 20

#: The window's default width (``window.set_default_size``); the right pane
#: gets whatever the divider leaves.
_WINDOW_WIDTH = WINDOW_WIDTH


class TestColumnTable(unittest.TestCase):
    def test_the_six_columns_in_order(self):
        # The owner's list: name, status, cpu, cpu graph, memory, memory graph.
        self.assertEqual([c.key for c in COLUMNS],
                         ["name", "status", "cpu", "cpu_graph", "mem",
                          "mem_graph"])

    def test_keys_are_unique(self):
        keys = [c.key for c in COLUMNS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_column_has_a_title_and_a_width(self):
        for c in COLUMNS:
            with self.subTest(column=c.key):
                self.assertTrue(c.title.strip())
                self.assertGreater(c.width, 0)

    def test_lookup_by_key(self):
        self.assertEqual(column("mem").xalign, 1.0)
        with self.assertRaises(KeyError):
            column("nope")

    def test_numeric_columns_are_right_aligned(self):
        # a number column that is left-aligned cannot be scanned; the header
        # uses the same xalign or the title sits off its values
        self.assertEqual(column("cpu").xalign, 1.0)
        self.assertEqual(column("mem").xalign, 1.0)

    def test_only_the_graphs_expand(self):
        self.assertEqual([c.key for c in COLUMNS if c.expand],
                         ["cpu_graph", "mem_graph"])

    def test_sort_keys_are_the_models_keys(self):
        # LeftPaneModel sorts by exactly these; a header pointing at another
        # key would silently never sort.
        self.assertEqual({c.sort_key for c in COLUMNS if c.sort_key},
                         {"name", "cpu"})

    def test_fixed_columns_are_the_label_columns(self):
        self.assertEqual([c.key for c in fixed_columns()],
                         ["name", "status", "cpu", "mem"])

    def test_name_width_covers_the_dot_plus_a_readable_name(self):
        # the row's name cell is dot + gap + label, and the label must still
        # have room for a typical qube name (12+ characters)
        name = column("name")
        self.assertGreater(name.width - DOT_SIZE - DOT_GAP, 80)


class TestColumnBudget(unittest.TestCase):
    """The minimum widths must fit the pane at the default divider position."""

    def test_fits_the_default_pane(self):
        total = sum(c.width for c in COLUMNS) + SPACING * (len(COLUMNS) - 1)
        budget = DEFAULT_POSITION - _SCROLLBAR_ALLOWANCE
        self.assertLessEqual(
            total, budget,
            "the columns need %d px but the default left pane offers %d: the "
            "list would open with a horizontal scrollbar and the graphs "
            "off-screen" % (total, budget))


class TestProcColumnTable(unittest.TestCase):
    """The process table is described once, in ``PROC_COLUMNS``.

    The bug this pins: the table's header was four hand-built labels with
    hardcoded widths and the sort was driven by three ``Gtk.Button``s in a
    toolbar *above* the table, sitting over no column at all. Columns are now
    described once and the header and the rows both build from it.
    """

    def test_the_four_columns_in_order(self):
        self.assertEqual([c.key for c in PROC_COLUMNS],
                         ["pid", "name", "cpu", "rss"])

    def test_titles_are_top_isms(self):
        self.assertEqual([c.title for c in PROC_COLUMNS],
                         ["PID", "NAME", "%CPU", "RSS"])

    def test_numeric_columns_right_align_and_sort(self):
        by_key = {c.key: c for c in PROC_COLUMNS}
        for key in ("pid", "cpu", "rss"):
            with self.subTest(column=key):
                self.assertEqual(by_key[key].xalign, 1.0)
                # the header and the rows read the same xalign, so a title
                # cannot sit off its values
                self.assertEqual(by_key[key].sort_key, key)
        self.assertEqual(by_key["name"].xalign, 0.0)

    def test_the_name_column_is_capped(self):
        # a 30-character kernel thread must ellipsise, not widen the table
        name = {c.key: c for c in PROC_COLUMNS}["name"]
        self.assertGreater(name.max_chars, 0)

    def test_no_column_expands(self):
        # nothing here can shrink without hiding a value, so the table never
        # stretches its columns to fill a wide pane
        self.assertFalse([c.key for c in PROC_COLUMNS if c.expand])

    def test_keys_are_unique(self):
        keys = [c.key for c in PROC_COLUMNS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_fits_the_default_right_pane(self):
        # the right pane is what the divider leaves, minus the scrollbar and
        # the arrow slots each sortable title reserves
        cells = sum(c.width for c in PROC_COLUMNS) + SPACING * (len(PROC_COLUMNS) - 1)
        sortable = len([c for c in PROC_COLUMNS if c.sort_key])
        header = cells + sortable * (ARROW_WIDTH + 2)
        budget = _WINDOW_WIDTH - DEFAULT_POSITION - _SCROLLBAR_ALLOWANCE
        self.assertLessEqual(
            header, budget,
            "the process table needs %d px but the default right pane offers "
            "%d: it would open with a horizontal scrollbar" % (header, budget))


class TestListHeight(unittest.TestCase):
    """D-25: the owner saw 2.5 qubes in the list and asked for 5-6."""

    def test_the_minimum_shows_at_least_five_whole_rows(self):
        self.assertGreaterEqual(LIST_MIN_ROWS, 5)
        self.assertGreaterEqual(list_min_height(), 5 * GRAPH_HEIGHT)

    def test_more_rows_need_more_height(self):
        self.assertGreater(list_min_height(6), list_min_height(5))

    def test_the_default_window_is_taller_than_the_old_one(self):
        # 640 was the height that showed two and a half rows
        self.assertGreaterEqual(WINDOW_HEIGHT - 640,
                                list_min_height(3.5) - list_min_height(0))


class TestEdgeAt(unittest.TestCase):
    """The undecorated window's resize grip (D-23)."""

    W, H = 400, 300

    def test_the_inside_is_a_move(self):
        self.assertIsNone(edge_at(200, 150, self.W, self.H))
        self.assertIsNone(edge_at(RESIZE_GRIP, RESIZE_GRIP, self.W, self.H))

    def test_sides(self):
        self.assertEqual(edge_at(200, 0, self.W, self.H), "n")
        self.assertEqual(edge_at(200, self.H - 1, self.W, self.H), "s")
        self.assertEqual(edge_at(0, 150, self.W, self.H), "w")
        self.assertEqual(edge_at(self.W - 1, 150, self.W, self.H), "e")

    def test_corners(self):
        self.assertEqual(edge_at(0, 0, self.W, self.H), "nw")
        self.assertEqual(edge_at(self.W - 1, 0, self.W, self.H), "ne")
        self.assertEqual(edge_at(0, self.H - 1, self.W, self.H), "sw")
        self.assertEqual(edge_at(self.W - 1, self.H - 1, self.W, self.H),
                         "se")

    def test_the_grip_is_exactly_its_width(self):
        self.assertEqual(edge_at(RESIZE_GRIP - 1, 150, self.W, self.H), "w")
        self.assertIsNone(edge_at(RESIZE_GRIP, 150, self.W, self.H))
        self.assertEqual(edge_at(self.W - RESIZE_GRIP, 150, self.W, self.H),
                         "e")
        self.assertIsNone(edge_at(self.W - RESIZE_GRIP - 1, 150,
                                  self.W, self.H))


if __name__ == "__main__":
    unittest.main(verbosity=2)
