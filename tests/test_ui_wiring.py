"""Wiring test for the left-pane table, against a narrow fake ``gi``.

**What this proves and what it does not.** It executes ``LeftPane`` and
``_QubeRow`` for real — the ``COLUMNS`` loops, the cell/heading construction,
the size-group registration, the in-place refresh, the scrollbar reserve and the
header arrows — so a logic error (a missing label key, a header built from a
different list, a row recreated instead of repainted, a spacer never sized)
fails here. It does **not** prove any GTK behaviour, and it cannot catch an
invented API name: the fake is written from the same assumptions as the code,
which is exactly how the three real target defects (``qubesadmin.app.Qubes``,
``get_cputime``, ``QStyleContext.get_default``) got past green suites. API names
are checked on the target by ``python3 -m qubes_top --api-audit``; the fake here
is deliberately *narrow* — it offers no method the code does not call, and none
that GTK3 does not have (checked against the gtk-3-24 headers).

Run standalone:

    python3 tests/test_ui_wiring.py
"""
import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# A narrow fake gi: only the surface left_pane.py touches.
# ---------------------------------------------------------------------------
class _Allocation:
    def __init__(self, width=0, height=0, x=0, y=0):
        self.width = width
        self.height = height
        self.x = x
        self.y = y


class _Adjustment:
    def __init__(self):
        self._value = 0.0

    def get_value(self):
        return self._value

    def set_value(self, value):
        self._value = value

    def clamp_page(self, lower, upper):
        self.clamped = (lower, upper)


class _StyleContext:
    def __init__(self):
        self.classes = set()

    def add_class(self, name):
        self.classes.add(name)

    def remove_class(self, name):
        self.classes.discard(name)

    def get_color(self, state):
        # the theme's text colour (charts draw their text in it)
        return types.SimpleNamespace(red=0.9, green=0.9, blue=0.9, alpha=1.0)


class _Widget:
    def __init__(self, *args, **kwargs):
        self.orientation = kwargs.get("orientation")
        self.spacing = kwargs.get("spacing")
        self.label = kwargs.get("label")
        self.children = []
        self.size_request = (-1, -1)
        self.style = _StyleContext()
        self.calls = []
        self.signals = []
        self.visible = False
        self.alloc_width = 0
        self.alloc_height = 0
        self.text = self.label
        # a real Gtk.Label already has these two properties (default 0/absent
        # behaviour); modelling them keeps a missing call a clean assertion
        # failure instead of an AttributeError
        self.max_width_chars = 0
        self.xalign = 0.0
        self.ellipsize = None

    # container
    def add(self, child):
        self.children.append(child)

    def pack_start(self, child, expand, fill, padding):
        self.children.append(child)

    def remove(self, child):
        self.children.remove(child)

    def get_child(self):
        return self.children[0] if self.children else None

    # widget
    def set_size_request(self, width, height):
        self.size_request = (width, height)

    def get_size_request(self):
        return self.size_request

    def connect(self, signal, callback, *args):
        self.signals.append((signal, callback, args))
        return len(self.signals)

    def add_events(self, mask):
        self.event_mask = mask

    def show_all(self):
        self.visible = True
        for child in self.children:
            child.show_all()

    def show(self):
        self.visible = True

    def queue_draw(self):
        self.calls.append("queue_draw")

    def get_style_context(self):
        return self.style

    def get_allocated_width(self):
        return self.alloc_width

    def get_allocation(self):
        return _Allocation(self.alloc_width, self.alloc_height,
                           0, getattr(self, "alloc_y", 0))

    def set_tooltip_text(self, text):
        self.tooltip = text

    def set_can_focus(self, value):
        self.can_focus = value

    def grab_focus_without_selecting(self):
        self.focused = True

    def get_allocated_height(self):
        return self.alloc_height

    def set_visible_window(self, value):
        self.visible_window = value

    def set_above_child(self, value):
        self.above_child = value

    def set_xalign(self, value):
        self.xalign = value

    def set_ellipsize(self, value):
        self.ellipsize = value

    def set_max_width_chars(self, value):
        self.max_width_chars = value

    def set_text(self, text):
        changed = text != self.text
        self.text = text
        # an entry emits "changed" on a programmatic set_text, as GTK does
        if changed:
            for signal, callback, args in list(self.signals):
                if signal == "changed":
                    callback(self, *args)

    def get_text(self):
        return self.text or ""

    # specific widget calls used by the pane
    def set_policy(self, hpolicy, vpolicy):
        self.policy = (hpolicy, vpolicy)

    def set_min_content_height(self, height):
        self.min_content_height = height

    def get_vadjustment(self):
        self._vadj = getattr(self, "_vadj", _Adjustment())
        return self._vadj

    def set_placeholder_text(self, text):
        self.placeholder = text

    def set_active(self, value):
        self.active = value

    def get_active(self):
        return getattr(self, "active", False)

    def set_relief(self, value):
        self.relief = value

    def attach(self, child, col, row, width, height):
        self.children.append(child)

    def set_label_widget(self, widget):
        self.label_widget = widget

    def set_label_align(self, x, y):
        self.label_align = (x, y)

    def hide(self):
        self.visible = False

    def get_visible(self):
        return self.visible

    def load_from_data(self, data):
        self.css = data


class _SizeGroup:
    def __init__(self, mode=None, **kwargs):
        self.mode = mode
        self.widgets = []

    def add_widget(self, widget):
        if widget in self.widgets:
            raise AssertionError("widget added to a size group twice")
        self.widgets.append(widget)


class _StyleContextClass:
    pass


FakeGtk = types.SimpleNamespace(
    Box=_Widget,
    Label=_Widget,
    Frame=_Widget,
    Grid=_Widget,
    EventBox=_Widget,
    DrawingArea=_Widget,
    SearchEntry=_Widget,
    CheckButton=_Widget,
    # annotations only (theme.py's signatures)
    Widget=_Widget,
    Window=_Widget,
    ScrolledWindow=_Widget,
    SizeGroup=_SizeGroup,
    CssProvider=_Widget,
    Orientation=types.SimpleNamespace(HORIZONTAL=0, VERTICAL=1),
    PolicyType=types.SimpleNamespace(NEVER=0, AUTOMATIC=1, ALWAYS=2),
    SizeGroupMode=types.SimpleNamespace(NONE=0, HORIZONTAL=1, VERTICAL=2),
    ReliefStyle=types.SimpleNamespace(NONE=0),
    StateFlags=types.SimpleNamespace(NORMAL=0),
    STYLE_PROVIDER_PRIORITY_APPLICATION=600,
)
# A simple namespace cannot host a class with a staticmethod cleanly; the
# pane calls Gtk.StyleContext.add_provider_for_screen, so give it one.
FakeGtk.StyleContext = types.SimpleNamespace(
    add_provider_for_screen=lambda *args: None)

FakeGdk = types.SimpleNamespace(
    EventMask=types.SimpleNamespace(BUTTON_PRESS_MASK=1 << 8),
    Screen=types.SimpleNamespace(get_default=lambda: None),
)
FakePango = types.SimpleNamespace(
    EllipsizeMode=types.SimpleNamespace(END=3))

gi_mod = types.ModuleType("gi")
gi_mod.require_version = lambda *args: None
repo = types.ModuleType("gi.repository")
repo.Gtk = FakeGtk
repo.Gdk = FakeGdk
repo.Pango = FakePango
gi_mod.repository = repo
sys.modules["gi"] = gi_mod
sys.modules["gi.repository"] = repo

from qubes_top.core import MemState, SampleRecord  # noqa: E402
from qubes_top.ui.layout import (  # noqa: E402
    list_min_height,
    ARROW_WIDTH,
    COLUMNS,
    DOT_GAP,
    DOT_SIZE,
    PROC_COLUMNS,
)
from qubes_top.ui.left_pane import (  # noqa: E402
    _GRAPH_LABEL_FONT_PX, LeftPane, _QubeRow,
)
from qubes_top.ui.model import LeftPaneModel  # noqa: E402
from qubes_top.ui.charts import CHART_H, HistoryCharts  # noqa: E402
from qubes_top.ui.infobox import FIELDS, InfoBox  # noqa: E402
from qubes_top.ui.iostats import IoHistory  # noqa: E402
from qubes_top.ui.right_pane import _ProcRow  # noqa: E402
from qubes_top.ui.rightspec import DetailSpec, ProcRow  # noqa: E402
from qubes_top.ui.rowspec import RowData, make_row_spec  # noqa: E402
from qubes_top.ui import theme  # noqa: E402

MiB = 1024  # KiB per MiB


def _record(name, state="Running"):
    return SampleRecord(name=name, domid=1, state=state, cpu_time_ns=100,
                        mem_assigned_kib=1024 * MiB,
                        mem_max_kib=1024 * MiB,
                        mem_used_kib=400 * MiB, swap_used_kib=None, ts=1.0)


def _rowdata(name, state="Running"):
    """The model's input for one qube."""
    return RowData(name=name, record=_record(name, state),
                   mem_state=MemState.REPORTED,
                   cpu_values=(None, 12.0),
                   mem_points=((1.0, 400 * MiB),))


def _spec(name, state="Running"):
    """The row widget's input: the model's RowSpec."""
    return make_row_spec(_rowdata(name, state), lambda n: "#123456")


def _rowbox(pane):
    """The vertical box the rows are packed into (the scroller's child)."""
    return pane._scroller.get_child()


class TestRowCells(unittest.TestCase):
    def test_one_cell_per_column_in_order(self):
        row = _QubeRow(_spec("alpha"))
        self.assertEqual(len(row._box.children), len(COLUMNS))
        # children in COLUMNS order: name cell, status, cpu, cpu graph, mem,
        # mem graph
        name_cell = row._box.children[0]
        self.assertEqual(len(name_cell.children), 2)  # dot, label
        self.assertEqual(name_cell.children[0].size_request,
                         (DOT_SIZE, DOT_SIZE))
        self.assertEqual(row._box.children[1], row._labels["status"])
        self.assertEqual(row._box.children[2], row._labels["cpu"])
        self.assertEqual(row._box.children[3], row._cpu_line)
        self.assertEqual(row._box.children[4], row._labels["mem"])
        self.assertEqual(row._box.children[5], row._mem_line)

    def test_apply_writes_all_four_labels(self):
        row = _QubeRow(_spec("alpha"))
        row.apply(_spec("bravo"))
        self.assertEqual(row._labels["name"].get_text(), "bravo")
        self.assertEqual(row._labels["status"].get_text(), "Running")
        self.assertEqual(row._labels["cpu"].get_text(), "12.0%")
        self.assertEqual(row._labels["mem"].get_text(), "0.39 / 1.0 GiB")

    def test_the_name_label_is_squeezed_past_the_dot(self):
        row = _QubeRow(_spec("alpha"))
        self.assertEqual(row._labels["name"].size_request[0],
                         COLUMNS[0].width - DOT_SIZE - DOT_GAP)

    def test_the_graphs_are_drawing_areas_with_a_fixed_height(self):
        row = _QubeRow(_spec("alpha"))
        self.assertEqual(row._box.children[3], row._cpu_line)
        self.assertEqual(row._box.children[5], row._mem_line)
        self.assertEqual(row._cpu_line.size_request[0], COLUMNS[3].width)
        self.assertEqual(row._cpu_line.size_request[1],
                         row._mem_line.size_request[1])


class _RecordingCr:
    """A cairo context that records drawn text, and reports real ink metrics.

    ``text_extents`` returns the box relative to the pen origin
    (``x_bearing``/``y_bearing``/``width``/``height``) — the shape the
    centring arithmetic in ``draw_graph_top_label`` relies on, and the one
    thing a catch-all no-op fake cannot stand in for.
    """

    def __init__(self, width=20.0, height=8.0, y_bearing=-8.0):
        self.texts = []
        self.font_sizes = []
        self._at = (0.0, 0.0)
        self._ext = types.SimpleNamespace(
            x_bearing=0.0, y_bearing=y_bearing, width=width, height=height,
            x_advance=width, y_advance=0.0)

    def set_font_size(self, size):
        self.font_sizes.append(size)

    def text_extents(self, text):
        return self._ext

    def move_to(self, x, y):
        self._at = (x, y)

    def show_text(self, text):
        self.texts.append((text, self._at))

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class TestGraphLabelsAreDrawn(unittest.TestCase):
    """D-21, executed rather than merely computed.

    The CPU sparkline auto-scales to each row's own maximum, so the same
    squiggle means something different on every row; the label is what makes
    that legible ("12.0%" = this graph spans 0–12%). Pinned here: the text is
    actually drawn, it is the spec's bound, it is centred on the glyphs' ink
    (not dropped at the baseline origin), and an empty series draws nothing.
    """

    def _draw(self, row, which, width=76, height=28):
        area = row._cpu_line if which == "cpu" else row._mem_line
        area.alloc_width, area.alloc_height = width, height
        handler = row._on_cpu_draw if which == "cpu" else row._on_mem_draw
        cr = _RecordingCr()
        handler(area, cr)
        return cr, width, height

    def test_the_cpu_graph_draws_its_maximum_in_the_middle(self):
        row = _QubeRow(_spec("alpha"))          # cpu_values (None, 12.0)
        cr, width, height = self._draw(row, "cpu")
        # four halo passes then the text itself, all the same string
        self.assertEqual({t for t, _at in cr.texts}, {"12.0%"})
        self.assertEqual(len(cr.texts), 5, "expected halo + text")
        _text, (x, y) = cr.texts[-1]            # the un-haloed pass
        # the 20 x 8 ink box sits on the baseline (y_bearing -8): its centre
        # must be the graph's centre, in both axes
        self.assertAlmostEqual((x + 0.0) + 20.0 / 2.0, width / 2.0, places=3)
        self.assertAlmostEqual(y - 8.0 + 8.0 / 2.0, height / 2.0, places=3)

    def test_the_label_is_drawn_small_enough_for_a_28px_graph(self):
        # 9 px in a 28 px box: the vision check on the row mock called the
        # default cairo size too big for the graph
        row = _QubeRow(_spec("alpha"))
        cr, _w, _h = self._draw(row, "cpu")
        self.assertEqual(cr.font_sizes, [_GRAPH_LABEL_FONT_PX])
        self.assertLess(_GRAPH_LABEL_FONT_PX, 28 / 2.0)

    def test_the_halo_is_drawn_before_the_text(self):
        # the label goes on top of its own outline: halo passes are offset,
        # the last pass sits exactly on the centred origin
        row = _QubeRow(_spec("alpha"))
        cr, _w, _h = self._draw(row, "cpu")
        origins = [at for _t, at in cr.texts]
        self.assertEqual(len(set(origins[:-1])), 4, "halo needs 4 offsets")
        self.assertNotIn(origins[-1], origins[:-1])

    def test_the_memory_graph_draws_its_ceiling_in_the_middle(self):
        row = _QubeRow(_spec("alpha"))          # max = 1024 MiB
        cr, _width, _height = self._draw(row, "mem")
        self.assertEqual({t for t, _at in cr.texts}, {"1.0 GiB"})

    def test_the_label_follows_the_row_it_belongs_to(self):
        row = _QubeRow(_spec("alpha"))
        other = make_row_spec(
            RowData(name="bravo", record=_record("bravo"),
                    mem_state=MemState.REPORTED,
                    cpu_values=(None, 0.5, 3.0),
                    mem_points=((1.0, 400 * MiB),)),
            lambda n: "#123456")
        row.apply(other)
        cr, _w, _h = self._draw(row, "cpu")
        self.assertEqual({t for t, _at in cr.texts}, {"3.0%"})

    def test_an_empty_series_draws_no_label(self):
        empty = make_row_spec(
            RowData(name="alpha", record=_record("alpha"),
                    mem_state=MemState.REPORTED,
                    cpu_values=(), mem_points=()),
            lambda n: "#123456")
        row = _QubeRow(empty)
        for which in ("cpu", "mem"):
            cr, _w, _h = self._draw(row, which)
            self.assertEqual(cr.texts, [], which)

    def test_a_context_without_metrics_costs_the_label_not_the_row(self):
        # the label is drawn last, so a cairo surprise cannot take the
        # sparkline down with it
        class NoMetrics(_RecordingCr):
            def text_extents(self, text):
                raise RuntimeError("no metrics (test)")

        row = _QubeRow(_spec("alpha"))
        area = row._cpu_line
        area.alloc_width, area.alloc_height = 76, 28
        cr = NoMetrics()
        self.assertFalse(row._on_cpu_draw(area, cr))
        self.assertEqual(cr.texts, [])


class TestHeader(unittest.TestCase):
    def setUp(self):
        self.model = LeftPaneModel()
        self.model.refresh([self._d("b"), self._d("a")])
        self.pane = LeftPane(self.model)
        self.pane.refresh()
        # children: filter row, header, scroller, status
        self.header = self.pane.children[1]

    @staticmethod
    def _d(name):
        return _rowdata(name)

    def test_one_title_per_column_plus_the_spacer(self):
        self.assertEqual(len(self.header.children), len(COLUMNS) + 1)
        self.assertEqual(self.header.children[-1], self.pane._header_spacer)

    def test_titles_are_in_column_order(self):
        for col in COLUMNS:
            self.assertEqual(self.pane._header_labels[col.key].get_text(),
                             col.title, col.key)

    def test_sorted_column_carries_the_arrow(self):
        # default: name ascending
        self.assertEqual(self.pane._header_arrows["name"].get_text(),
                         "\u2191")
        self.assertEqual(self.pane._header_arrows["cpu"].get_text(), "")

    def test_the_arrow_slot_exists_for_every_sortable_column(self):
        # a fixed-width slot that is always allocated: activating a sort can
        # change the glyph, never the layout
        for key in ("name", "cpu"):
            self.assertEqual(self.pane._header_arrows[key].size_request,
                             (ARROW_WIDTH, -1))
        for key in ("status", "cpu_graph", "mem", "mem_graph"):
            self.assertIsNone(self.pane._header_arrows[key])

    def test_clicking_a_header_re_sorts(self):
        # the handler is wired to the sort key, not to a column index
        handlers = [(sig, args) for holder in self.header.children[:-1]
                    for (sig, _cb, args) in getattr(holder, "signals", [])
                    if sig == "button-press-event"]
        self.assertEqual([args[0] for _sig, args in handlers], ["name", "cpu"])

    def test_sort_keys_match_the_model(self):
        for key in (c.sort_key for c in COLUMNS if c.sort_key):
            self.model.toggle_sort(key)
            self.assertEqual(self.model.sort_key(), key)


class TestRefreshInPlace(unittest.TestCase):
    def setUp(self):
        self.model = LeftPaneModel()
        self.model.refresh([_rowdata("b"), _rowdata("a")])
        self.pane = LeftPane(self.model)
        self.pane.refresh()

    def test_rows_are_created_once_and_reused(self):
        first = dict(self.pane._rows)
        self.pane._model.refresh([_rowdata("a"), _rowdata("b")])
        self.pane.refresh()
        self.assertEqual(self.pane._rows["a"], first["a"])
        self.assertEqual(self.pane._rows["b"], first["b"])

    def test_rows_are_packed_in_the_models_order(self):
        self.assertEqual(self.pane._last_order, ["a", "b"])
        self.assertEqual([r for r in _rowbox(self.pane).children],
                         [self.pane._rows["a"], self.pane._rows["b"]])

    def test_a_gone_row_is_removed(self):
        self.pane._model.refresh([_rowdata("a")])
        self.pane.refresh()
        self.assertNotIn("b", self.pane._rows)
        self.assertEqual(len(_rowbox(self.pane).children), 1)

    def test_rows_are_visible(self):
        for row in self.pane._rows.values():
            self.assertTrue(row.visible)


class TestSystemlistControls(unittest.TestCase):
    """The top row, the footer and the keyboard helpers (D-23)."""

    def setUp(self):
        self.selected = []
        self.model = LeftPaneModel()
        self.model.refresh([_rowdata("b"), _rowdata("a"), _rowdata("c")])
        self.pane = LeftPane(self.model, on_select=self.selected.append)
        self.pane.refresh()

    def test_search_field_and_check_wear_systemlists_classes(self):
        self.assertIn("search_entry", self.pane._filter.style.classes)
        self.assertEqual(self.pane._filter.placeholder, "search")
        self.assertIn("show-all", self.pane._halt.style.classes)
        self.assertEqual(self.pane._halt.label, "All qubes")
        # the check never takes the focus away from the search field
        self.assertIs(self.pane._halt.can_focus, False)

    def test_footer_turns_red_on_error_and_back(self):
        status = self.pane._status
        self.assertIn("footer", status.style.classes)
        self.pane.set_status("degraded", error=True)
        self.assertIn("error", status.style.classes)
        self.pane.set_status("fine")
        self.assertNotIn("error", status.style.classes)
        self.assertEqual(status.text, "fine")

    def test_clear_search_clears_then_reports_empty(self):
        self.pane._filter.set_text("a")
        self.assertEqual(self.pane._last_order, ["a"])
        self.assertTrue(self.pane.clear_search())
        self.assertEqual(self.pane._last_order, ["a", "b", "c"])
        # already empty: the window closes on this Escape
        self.assertFalse(self.pane.clear_search())

    def test_the_list_keeps_room_for_five_rows(self):
        # D-25: the info box and the charts must not squeeze the list
        self.assertEqual(self.pane._scroller.min_content_height,
                         list_min_height())

    def test_toggle_halted_flips_the_check(self):
        self.pane.toggle_halted()
        self.assertTrue(self.pane._halt.get_active())
        self.pane.toggle_halted()
        self.assertFalse(self.pane._halt.get_active())

    def test_move_selection_selects_reports_and_scrolls(self):
        self.pane._rows["b"].alloc_y = 40
        self.pane._rows["b"].alloc_height = 30
        self.pane.move_selection(1)   # nothing selected -> the first row
        self.pane.move_selection(1)
        self.assertEqual(self.model.selected(), "b")
        self.assertEqual(self.selected, ["a", "b"])
        self.assertIn("selected", self.pane._rows["b"].style.classes)
        self.assertNotIn("selected", self.pane._rows["a"].style.classes)
        self.assertEqual(self.pane._scroller.get_vadjustment().clamped,
                         (40, 70))


class TestThemeFiles(unittest.TestCase):
    """qubes-top.css is written against the stock menu's colour names; the
    fallback must define every one it uses, or GTK drops the rule."""

    def _css(self, name):
        return (pathlib.Path(theme.__file__).with_name(name)).read_text()

    def test_every_colour_name_used_is_defined(self):
        import re
        own = self._css("qubes-top.css")
        defined = set(re.findall(r"@define-color\s+([\w-]+)",
                                 self._css("fallback-theme.css") + own))
        used = set(re.findall(r"@([a-z][\w-]*)", own)) - {"define-color",
                                                         "ALPHA"}
        self.assertEqual(used - defined, set())

    def test_alpha_placeholder_present(self):
        self.assertIn(theme.ALPHA_PLACEHOLDER, self._css("qubes-top.css"))

    def test_rgba(self):
        self.assertEqual(theme.rgba("#ff0000", 0.5), (1.0, 0.0, 0.0, 0.5))
        self.assertEqual(theme.rgba("#000000"), (0.0, 0.0, 0.0, 1.0))


class TestScrollbarReserve(unittest.TestCase):
    def setUp(self):
        self.model = LeftPaneModel()
        self.pane = LeftPane(self.model)

    def test_spacer_matches_the_missing_call(self):
        # the scroller is 560 wide, its content 540: a 20 px bar is showing
        self.pane._scroller.alloc_width = 560
        _rowbox(self.pane).alloc_width = 540
        self.pane._on_scroller_allocated(
            self.pane._scroller, _Allocation(560, 400))
        self.assertEqual(self.pane._header_spacer.get_size_request()[0], 20)

    def test_no_bar_means_no_spacer(self):
        self.pane._scroller.alloc_width = 560
        _rowbox(self.pane).alloc_width = 560
        self.pane._on_scroller_allocated(
            self.pane._scroller, _Allocation(560, 400))
        self.assertEqual(self.pane._header_spacer.get_size_request()[0], 0)

    def test_unallocated_widgets_do_not_resize_the_header(self):
        # first allocation can report zeros; the spacer must stay untouched
        self.pane._scroller.alloc_width = 0
        _rowbox(self.pane).alloc_width = 0
        before = self.pane._header_spacer.get_size_request()
        self.pane._on_scroller_allocated(
            self.pane._scroller, _Allocation(0, 0))
        self.assertEqual(self.pane._header_spacer.get_size_request(), before)


class TestSizeGroups(unittest.TestCase):
    def test_every_fixed_column_has_a_group_including_the_header(self):
        model = LeftPaneModel()
        model.refresh([_rowdata("a")])
        pane = LeftPane(model)
        pane.refresh()
        for key in ("name", "status", "cpu", "mem"):
            group = pane._groups[key]
            # one header cell + one row cell per row
            self.assertEqual(len(group.widgets), 2, key)
        self.assertNotIn("cpu_graph", pane._groups)
        self.assertNotIn("mem_graph", pane._groups)

    def test_a_new_row_joins_the_groups(self):
        model = LeftPaneModel()
        model.refresh([_rowdata("a")])
        pane = LeftPane(model)
        pane.refresh()
        model.refresh([_rowdata("a"), _rowdata("b")])
        pane.refresh()
        self.assertEqual(len(pane._groups["name"].widgets), 3)


class TestProcRowCells(unittest.TestCase):
    """``_ProcRow`` — the process table's row, executed against a fake gi.

    The process table had the same three alignment defects as the qube list
    before it was rebuilt from ``PROC_COLUMNS`` (D-16): hand-built cells, no
    shared widths, and a header that could not reserve the scrollbar. These
    tests execute the row itself, so a cell order, an alignment or a missing
    size-group registration is a failure here rather than a shift on dom0.
    """

    def _groups(self):
        return {c.key: _SizeGroup() for c in PROC_COLUMNS}

    def test_one_cell_per_column_in_order(self):
        row = _ProcRow({})
        self.assertEqual(len(row.children), len(PROC_COLUMNS))
        self.assertEqual([c.get_size_request()[0] for c in row.children],
                         [c.width for c in PROC_COLUMNS],
                         "the cells must be in PROC_COLUMNS order at their "
                         "declared minimum widths")

    def test_numbers_align_right_and_the_name_left(self):
        row = _ProcRow({})
        for cell, col in zip(row.children, PROC_COLUMNS):
            with self.subTest(column=col.key):
                self.assertEqual(cell.xalign, col.xalign)

    def test_every_cell_joins_its_column_group(self):
        groups = self._groups()
        row = _ProcRow(groups)
        for cell, col in zip(row.children, PROC_COLUMNS):
            with self.subTest(column=col.key):
                self.assertIn(cell, groups[col.key].widgets,
                              "without the group a wide value shifts the "
                              "columns to its right")

    def test_the_name_ellipsises_and_is_capped(self):
        row = _ProcRow({})
        name = row.children[1]
        self.assertEqual(name.ellipsize, 3)  # Pango.EllipsizeMode.END
        self.assertGreater(name.max_width_chars, 0,
                           "one long kernel thread must not widen the table")

    def test_apply_writes_the_four_values_in_place(self):
        row = _ProcRow({})
        row.apply(ProcRow(pid=4210, name="kworker/0:1", cpu_text="12.5%",
                          rss_text="40.0 MiB"))
        self.assertEqual([c.get_text() for c in row.children],
                         ["4210", "kworker/0:1", "12.5%", "40.0 MiB"])
        # the same widget is reused: a second tick repaints it, it does not
        # grow the row
        row.apply(ProcRow(pid=4210, name="kworker/0:1", cpu_text="0.3%",
                          rss_text="41.0 MiB"))
        self.assertEqual([c.get_text() for c in row.children],
                         ["4210", "kworker/0:1", "0.3%", "41.0 MiB"])
        self.assertEqual(len(row.children), len(PROC_COLUMNS))

    def test_the_header_and_the_rows_read_the_same_alignment(self):
        # one description, so a title cannot sit off its values: PROC_COLUMNS
        # is the only place a width or an alignment is written down, so no
        # column number may appear in the pane at all
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "qubes_top" / "ui" / "right_pane.py").read_text()
        for width in ("64", "160", "72", "90"):
            self.assertNotIn("set_size_request(%s" % width, src,
                             "a column width must come from PROC_COLUMNS")


class TestHistoryCharts(unittest.TestCase):
    """``HistoryCharts`` — the large charts under the list (D-17), executed.

    They moved out of the detail pane, where they pushed the process table
    down, to a fixed-height block under the qube list. Both charts render the
    *same* in-place-refreshed spec the pane owns, so one selection drives
    both; they are hidden while nothing is selected.
    """

    def test_four_charts_at_the_fixed_height(self):
        charts = HistoryCharts()
        frames = charts._grid.children
        self.assertEqual(len(frames), 4, "CPU, memory, disk, network")
        for frame in frames:
            self.assertEqual(len(frame.children), 1)
            self.assertEqual(frame.children[0].get_size_request(),
                             (-1, CHART_H))

    def test_each_chart_has_a_frame_title(self):
        charts = HistoryCharts()
        titles = [f.label_widget.get_text() for f in charts._grid.children]
        self.assertTrue(titles[0].startswith("CPU"))
        self.assertTrue(titles[1].startswith("memory"))
        self.assertTrue(titles[2].startswith("disk"))
        self.assertTrue(titles[3].startswith("network"))

    def test_update_adopts_the_panes_spec_and_redraws(self):
        charts = HistoryCharts()
        spec = DetailSpec()
        io = IoHistory()
        areas = (charts._cpu_area, charts._mem_area, charts._disk_area,
                 charts._net_area)
        for area in areas:
            area.calls.clear()
        charts.update(spec, io)
        self.assertIs(charts._spec, spec)
        self.assertIs(charts._io, io)
        for area in areas:
            self.assertEqual(area.calls, ["queue_draw"])

    def test_nothing_selected_hides_the_block(self):
        charts = HistoryCharts()
        charts.show_for(True)
        self.assertTrue(charts.visible)
        charts.show_for(False)
        self.assertFalse(charts.visible,
                         "with no selection the list must get the room back")
        charts.show_for(True)
        self.assertTrue(charts.visible)

    def test_a_draw_before_any_update_is_harmless(self):
        # the window shows the charts only after a selection, but a draw can
        # arrive first (a resize); it must not raise
        charts = HistoryCharts()
        self.assertFalse(charts._on_cpu_draw(charts._cpu_area, _FakeCr()))
        self.assertFalse(charts._on_mem_draw(charts._mem_area, _FakeCr()))
        self.assertFalse(charts._on_disk_draw(charts._disk_area, _FakeCr()))
        self.assertFalse(charts._on_net_draw(charts._net_area, _FakeCr()))


class TestChartsAreReadable(unittest.TestCase):
    """D-24, executed: the large charts label their axes, state the newest
    value, and name the memory chart's three lines."""

    def _texts(self, handler, area, spec, charts):
        charts.update(spec)
        area.alloc_width, area.alloc_height = 560, CHART_H
        cr = _RecordingCr()
        handler(area, cr)
        return [t for t, _at in cr.texts]

    def _spec(self):
        spec = DetailSpec()
        spec.cpu_values = [None, 4.0, 150.5, 12.3]
        spec.cpu_hi = 150.5
        now = __import__("time").monotonic()
        spec.mem_points = [(now - 100.0, 900 * MiB), (now - 10.0, 1200 * MiB)]
        spec.mem_hi = float(4096 * MiB)
        spec.assigned_kib = 2400 * MiB
        spec.max_kib = 4096 * MiB
        return spec

    def test_cpu_axes_and_newest_value(self):
        charts = HistoryCharts()
        texts = self._texts(charts._on_cpu_draw, charts._cpu_area,
                            self._spec(), charts)
        for label in ("0%", "100%", "200%", "now", "\u22125 min", "12.3%"):
            self.assertIn(label, texts)

    def test_memory_axes_legend_and_newest_value(self):
        charts = HistoryCharts()
        texts = self._texts(charts._on_mem_draw, charts._mem_area,
                            self._spec(), charts)
        for label in ("0 GiB", "4 GiB", "used", "allocated", "max", "1.2 GiB",
                      "now"):
            self.assertIn(label, texts)
        # legend order: the series, then the live allocation, then the max
        self.assertLess(texts.index("used"), texts.index("allocated"))
        self.assertLess(texts.index("allocated"), texts.index("max"))

    def test_small_memory_is_labelled_in_mib(self):
        charts = HistoryCharts()
        spec = self._spec()
        spec.mem_hi = float(400 * MiB)
        spec.max_kib = 400 * MiB
        spec.assigned_kib = 0
        spec.mem_points = [(0.0, 300 * MiB)]
        texts = self._texts(charts._on_mem_draw, charts._mem_area, spec,
                            charts)
        self.assertIn("400 MiB", texts)
        self.assertNotIn("allocated", texts)  # unset: no line, no key

    def test_no_memory_data_says_so(self):
        charts = HistoryCharts()
        spec = self._spec()
        spec.mem_points = []
        texts = self._texts(charts._on_mem_draw, charts._mem_area, spec,
                            charts)
        self.assertIn("no reported memory data", texts)

    def _io_texts(self, handler, area, io, charts):
        charts.update(DetailSpec(), io)
        area.alloc_width, area.alloc_height = 280, CHART_H
        cr = _RecordingCr()
        handler(area, cr)
        return [t for t, _at in cr.texts]

    def test_disk_axes_and_newest_rates_in_the_legend(self):
        charts = HistoryCharts()
        io = IoHistory()
        io.push("a", None, None)
        io.push("a", (300 * 1024.0, 1536.0), None)
        texts = self._io_texts(charts._on_disk_draw, charts._disk_area, io,
                               charts)
        for label in ("0", "300K", "read 300.0 KiB/s", "written 1.5 KiB/s",
                      "now"):
            self.assertIn(label, texts)

    def test_an_empty_io_chart_says_why(self):
        charts = HistoryCharts()
        io = IoHistory()
        io.net_note = "no netvm: no network to measure"
        texts = self._io_texts(charts._on_net_draw, charts._net_area, io,
                               charts)
        self.assertIn("no netvm: no network to measure", texts)
        self.assertIn("in", texts)
        self.assertIn("4K", texts, "an idle axis tops out at the floor")


class _FakeCr:
    """Just enough cairo for the draw callbacks' early return."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class TestInfoBox(unittest.TestCase):
    """``InfoBox`` — the group box under the list (D-18), executed.

    The detail pane's two label/value grids moved here, above the charts. The
    box renders the same in-place-refreshed ``DetailSpec`` the pane owns, so
    the numbers cannot disagree with the charts beside them.
    """

    def test_one_label_and_one_value_per_field(self):
        info = InfoBox()
        grid = info.children[0].children[0]
        self.assertEqual(len(grid.children), 2 * len(FIELDS))

    def test_the_fields_are_the_ones_the_pane_used_to_show(self):
        self.assertEqual([attr for attr, _t in FIELDS],
                         ["template", "netvm", "vcpus", "mem_max_text",
                          "allocated_text", "used_text", "swap_text",
                          "report_text", "domid_text"])

    def test_update_copies_every_field_from_the_spec(self):
        info = InfoBox()
        spec = DetailSpec()
        spec.template = "fedora-40"
        spec.netvm = "sys-firewall"
        spec.vcpus = "4"
        spec.mem_max_text = "4.0 GiB"
        spec.allocated_text = "1000 MiB"
        spec.used_text = "1.1 GiB"
        spec.swap_text = "0 B"
        spec.report_text = "reported"
        spec.domid_text = "7"
        info.update(spec)
        self.assertEqual([info._values[a].get_text() for a, _t in FIELDS],
                         ["fedora-40", "sys-firewall", "4", "4.0 GiB",
                          "1000 MiB", "1.1 GiB", "0 B", "reported", "7"])

    def test_an_unknown_field_keeps_the_specs_placeholder(self):
        # a field the provider could not read shows as such — never blanked
        info = InfoBox()
        spec = DetailSpec()
        spec.netvm = "n/a"
        info.update(spec)
        self.assertEqual(info._values["netvm"].get_text(), "n/a")
        self.assertEqual(info._values["template"].get_text(), "n/a")

    def test_it_hides_until_something_is_selected(self):
        info = InfoBox()
        info.show_for(True)
        self.assertTrue(info.visible)
        info.show_for(False)
        self.assertFalse(info.visible,
                         "with no selection the list must get the room back")


if __name__ == "__main__":
    unittest.main(verbosity=2)
