"""The left-pane widget (card t_4d9516cd). GTK3, dom0-only.

Consumes the pure ``LeftPaneModel`` and renders its ``RowSpec``s as a **table**:
one row widget per qube, in six columns (name, status, CPU, CPU graph, memory,
memory graph), *updated in place* each tick — never discarded and recreated, so
selection and scroll survive refresh and qube add/remove. This module does
**no** metric arithmetic: the text comes from the ``RowSpec`` (built by the
core formatters) and the sparkline pixels come from the ``qubes_top.core``
geometry functions given the spec's values + per-qube bounds. It only maps data
to pixels and manages the widget lifecycle (selection, scroll, sort/filter/
toggle wiring).

**The columns are described once**, in ``qubes_top.ui.layout.COLUMNS``, and both
the row widget and the header build from that table — so a column cannot be
added to one and not the other, and a width cannot drift between the heading
and the data under it. Three things keep the columns aligned:

* every fixed column's cells (header + every row) are in one ``Gtk.SizeGroup``,
  so a wider value widens that column everywhere instead of shifting the
  columns after it — a per-row layout would line up only by coincidence;
* the header reserves the vertical scrollbar's width at its end, because the
  scroller's content is narrower than the pane by exactly that much (a
  right-aligned number column makes the offset obvious);
* the name column's header is indented by the colour dot + its gap, so "Name"
  sits over the names rather than over the dots.

**GTK3 pin (both namespaces, Gtk first):** dom0 ships GTK4, so a bare
``Gdk`` import resolves to 4.0 and the later Gtk 3.0 import dies on the target
(a failure a gi-less dev box cannot reproduce). The pin is repeated at the top
of every gi module by contract.

Only ``qubes_top.ui.__init__`` stays inert (the import contract); this module is
imported explicitly by ``app.py`` and never by the package ``__init__``.
"""
import math
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, Pango  # noqa: E402  (pin above is intentional)

from qubes_top.core import window_segments, window_steps
from qubes_top.ui.charts import trace
from qubes_top.ui.layout import (ARROW_WIDTH, COLUMNS, DOT_GAP, DOT_SIZE,
                               GRAPH_HEIGHT, ROW_SPACING, SPACING,
                               fixed_columns, list_min_height)
from qubes_top.ui.model import LeftPaneModel  # noqa: F401  (type seam, documented)
from qubes_top.ui.rowspec import RowSpec
from qubes_top.ui.theme import CPU_RGBA, MEM_RGBA

#: The columns whose values are numbers: monospaced, so digits don't jitter as
#: the values change (the column width is now fixed, but proportional digits
#: still dance inside it).
_MONO_COLUMNS = ("cpu", "mem")

#: How far Page Up / Page Down move the selection, in rows.
PAGE_ROWS = 10

#: The row sparklines: a line a little heavier than a hairline over a faint
#: wash of its colour (D-24) — a 1 px line on a 28 px graph read as noise.
_SPARK_LINE_PX = 1.5
_SPARK_FILL = 0.18

#: The scale label drawn inside each sparkline (D-21). Small — the graph is
#: 28 px tall — near-white, with a **dark halo**: the label sits directly over
#: the sparkline, and a single grey pass competes with a bright line on a dark
#: theme (it read as "hard to see" in the row mock). The halo needs no theme
#: knowledge, unlike a background-filled backing box, and keeps the text legible
#: on a light theme too.
_GRAPH_LABEL_FONT_PX = 9.0
_GRAPH_LABEL_RGBA = (0.88, 0.88, 0.88, 0.95)
_GRAPH_LABEL_HALO_RGBA = (0.0, 0.0, 0.0, 0.55)
_GRAPH_LABEL_HALO_OFFSETS = ((-1.0, 0.0), (1.0, 0.0), (0.0, -1.0), (0.0, 1.0))


def draw_graph_top_label(cr, width: float, height: float, text: str) -> None:
    """Centre ``text`` in a sparkline: "this graph spans 0 .. text" (D-21).

    The owner's ask: *"if there was a center text with the highest value from
    the data set ... if it says 10% it would mean the graph currently has 0-10
    as the interval"*. That makes the per-row auto-scaling legible instead of
    invisible, which is what the CPU sparkline was missing: a qube idling at
    3% and one pegged at 400% draw the same shape, and the number is what tells
    them apart.

    Centred on the glyphs' *ink*, not on the baseline: ``text_extents`` gives
    the box relative to the pen origin, so the origin is offset by the bearing
    and half the ink — dropping the text at ``(w/2, h/2)`` would sit it visibly
    high and right.

    Cosmetic and guarded: the sparkline is already drawn when this runs, so a
    cairo surprise costs the label, never the row. An empty string draws
    nothing (a series with no points has no interval to describe).
    """
    if not text:
        return
    try:
        cr.set_font_size(_GRAPH_LABEL_FONT_PX)
        ext = cr.text_extents(text)
        x = width / 2.0 - ext.x_bearing - ext.width / 2.0
        y = height / 2.0 - ext.y_bearing - ext.height / 2.0
        # halo first, so the label stays readable where a bright polyline
        # crosses it; then the text itself on top
        cr.set_source_rgba(*_GRAPH_LABEL_HALO_RGBA)
        for dx, dy in _GRAPH_LABEL_HALO_OFFSETS:
            cr.move_to(x + dx, y + dy)
            cr.show_text(text)
        cr.set_source_rgba(*_GRAPH_LABEL_RGBA)
        cr.move_to(x, y)
        cr.show_text(text)
    except Exception:
        pass


class _QubeRow(Gtk.EventBox):
    """One left-pane row: six cells, in the ``COLUMNS`` order.

    A ``Gtk.EventBox``, not a ``Gtk.Box``: a plain container has no
    ``GdkWindow``, so GDK never delivers a button press to it and the row
    cannot be clicked at all. The EventBox owns the window; the visual content
    lives in an inner Box, so the layout is unchanged.

    ``groups`` maps a column key to the ``Gtk.SizeGroup`` shared with the
    header and the other rows; each fixed-width cell joins it.
    """

    def __init__(self, spec: RowSpec, groups=None) -> None:
        super().__init__()
        # A *visible* window is what lets the .selected CSS background paint;
        # above_child puts that window over the children so a press over a
        # sparkline window still lands here instead of being swallowed.
        self.set_visible_window(True)
        self.set_above_child(True)
        # The EventBox owns a window; ask for press events explicitly rather
        # than relying on the default mask.
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self._box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                            spacing=SPACING)
        self.add(self._box)
        self._spec = spec
        self._color = spec.color
        self._labels = {}
        groups = groups if groups is not None else {}
        for col in COLUMNS:
            cell = self._build_cell(col)
            group = groups.get(col.key)
            if group is not None:
                group.add_widget(cell)
            self._box.pack_start(cell, col.expand, col.expand, 0)
        self.apply(spec)

    def _build_cell(self, col):
        """One column's widget: a drawing area for a graph, a dot + label box
        for the name, a label otherwise."""
        if col.key == "cpu_graph":
            self._cpu_line = self._make_graph(col, self._on_cpu_draw)
            return self._cpu_line
        if col.key == "mem_graph":
            self._mem_line = self._make_graph(col, self._on_mem_draw)
            return self._mem_line
        label = self._make_label(col)
        self._labels[col.key] = label
        if col.key != "name":
            return label
        # The name cell: the label-colour dot, then the name. The label's
        # width is the column's minus the dot and its gap, and the cell's
        # minimum is the column width, so the dot never eats the names and
        # the column never widens by a pixel.
        self._name_label = label
        label.set_size_request(col.width - DOT_SIZE - DOT_GAP, -1)
        cell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=DOT_GAP)
        self._dot = Gtk.DrawingArea()
        self._dot.set_size_request(DOT_SIZE, DOT_SIZE)
        self._dot.connect("draw", self._on_dot_draw)
        cell.pack_start(self._dot, False, False, 0)
        cell.pack_start(label, True, True, 0)
        cell.set_size_request(col.width, -1)
        return cell

    @staticmethod
    def _make_label(col):
        label = Gtk.Label()
        label.set_xalign(col.xalign)
        # guest names are untrusted: ellipsise rather than grow, and never
        # set_markup (set_text is the only writer in this module)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_size_request(col.width, -1)
        if col.max_chars:
            label.set_max_width_chars(col.max_chars)
        if col.key in _MONO_COLUMNS:
            label.get_style_context().add_class("mono")
        return label

    @staticmethod
    def _make_graph(col, on_draw):
        area = Gtk.DrawingArea()
        area.set_size_request(col.width, GRAPH_HEIGHT)
        area.connect("draw", on_draw)
        return area

    # -- in-place update (repaint the same widget, never recreate it) --------
    def apply(self, spec: RowSpec) -> None:
        self._spec = spec
        self._color = spec.color
        # set_text, never set_markup: the name/state are guest-supplied
        # (untrusted) — a markup injection vector is not worth a fancy label.
        self._labels["name"].set_text(spec.name)
        self._labels["status"].set_text(spec.state)
        self._labels["cpu"].set_text(spec.cpu_text)
        self._labels["mem"].set_text(spec.mem_text)
        self._dot.queue_draw()
        self._cpu_line.queue_draw()
        self._mem_line.queue_draw()

    # -- drawing (the only "arithmetic" is the core geometry, not metric) ----
    def _on_dot_draw(self, area: Gtk.DrawingArea, cr) -> bool:
        rgba = Gdk.RGBA()
        rgba.parse(self._color)
        cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 1.0)
        cr.arc(DOT_SIZE / 2.0, DOT_SIZE / 2.0, DOT_SIZE / 2.0 - 1.0,
               0.0, 2.0 * math.pi)
        cr.fill()
        # A faint grey ring, because the colour is now the *real* label
        # (D-20) and ``black`` is one of Qubes' labels — dom0's default. A
        # pure-black disc on a dark theme is an invisible dot, i.e. "no label
        # at all", which is a different statement. The ring is theme-neutral
        # (mid grey, partly transparent) so it reads as an edge on any theme.
        cr.set_line_width(1.0)
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.55)
        cr.arc(DOT_SIZE / 2.0, DOT_SIZE / 2.0, DOT_SIZE / 2.0 - 1.0,
               0.0, 2.0 * math.pi)
        cr.stroke()
        return False

    def _on_cpu_draw(self, area: Gtk.DrawingArea, cr) -> bool:
        spec = self._spec
        w = area.get_allocated_width()
        h = area.get_allocated_height()
        lo, hi = spec.cpu_bounds
        # the same fixed five-minute window as the large charts (D-24): now at
        # the right edge, so every row's graph shares one time scale
        segs = window_segments(spec.cpu_values, w, h, lo, hi)
        for seg in segs:
            trace(cr, seg, h - 2.0, CPU_RGBA, _SPARK_LINE_PX, _SPARK_FILL)
        # ...and the interval it is drawn against, in the middle (D-21)
        draw_graph_top_label(cr, w, h, spec.cpu_top_text)
        return False

    def _on_mem_draw(self, area: Gtk.DrawingArea, cr) -> bool:
        spec = self._spec
        w = area.get_allocated_width()
        h = area.get_allocated_height()
        lo, hi = spec.mem_bounds
        # A memory series that has not moved past the 30 MiB deadband (most
        # qubes, most of the time) is one point: window_steps draws it as a
        # flat line across the window, never a lone dot.
        verts = window_steps(spec.mem_points, time.monotonic(), w, h, lo, hi)
        trace(cr, verts, h - 2.0, MEM_RGBA, _SPARK_LINE_PX, _SPARK_FILL)
        # the ceiling the step is drawn against, in the middle (D-21)
        draw_graph_top_label(cr, w, h, spec.mem_top_text)
        return False


class LeftPane(Gtk.Box):
    """The left pane: a search field, an "All qubes" check, a re-sortable
    six-column header, the scrollable, in-place-updating row list and the
    footer — qubes-systemlist's arrangement (D-23). Wires the pure
    ``LeftPaneModel`` to the widgets; the model owns the logic
    (sort/filter/halt/selection), this owns the rendering and the in-place
    update."""

    def __init__(self, model: LeftPaneModel, on_select=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._model = model
        self._on_select = on_select
        self._rows = {}       # name -> _QubeRow (the in-place row widgets)
        self._last_order = []
        self._header_extra = -1
        # One size group per fixed column, shared by the header and every row:
        # this is what makes the columns a table rather than six independent
        # rows that happen to have the same widget order.
        self._groups = {c.key: Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
                        for c in fixed_columns()}
        # top controls: systemlist's search field + "All qubes" check. The
        # check never takes focus: the search field keeps it, so typing always
        # searches and the arrow keys always move the selection (window.py).
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._filter = Gtk.SearchEntry()
        self._filter.get_style_context().add_class("search_entry")
        self._filter.set_placeholder_text("search")
        self._filter.connect("changed", self._on_filter_changed)
        top.pack_start(self._filter, True, True, 0)
        self._halt = Gtk.CheckButton(label="All qubes")
        self._halt.set_tooltip_text("Show halted qubes too (Ctrl+H)")
        self._halt.set_can_focus(False)
        self._halt.get_style_context().add_class("show-all")
        self._halt.set_active(model.show_halted())
        self._halt.connect("toggled", self._on_halt_toggled)
        top.pack_start(self._halt, False, False, 0)
        self.pack_start(top, False, False, 0)
        # the column header (a title per column, click to re-sort)
        self.pack_start(self._build_header(), False, False, 0)
        # the scrollable row list
        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER,
                                  Gtk.PolicyType.AUTOMATIC)
        # at least LIST_MIN_ROWS rows, however much the info box and the
        # charts under the list take (D-25)
        self._scroller.set_min_content_height(list_min_height())
        self._rowbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                               spacing=ROW_SPACING)
        self._rowbox.get_style_context().add_class("rows")
        self._scroller.add(self._rowbox)
        # The scroller's content is narrower than the pane by the vertical
        # scrollbar's width whenever the bar is visible; reserve exactly that
        # at the end of the header (see _on_scroller_allocated).
        self._scroller.connect("size-allocate", self._on_scroller_allocated)
        self.pack_start(self._scroller, True, True, 0)
        # Status line: the collector's health, in systemlist's footer. A
        # monitoring window that shows an empty list with no reason is
        # indistinguishable from a broken one — which is exactly how the
        # row-visibility bug presented.
        self._status = Gtk.Label(label="waiting for the first sample…")
        self._status.set_xalign(0.0)
        self._status.set_ellipsize(Pango.EllipsizeMode.END)
        self._status.get_style_context().add_class("footer")
        self.pack_start(self._status, False, False, 0)

    def _build_header(self) -> Gtk.Box:
        """The header row, built from ``COLUMNS`` exactly as the rows are.

        The cells are plain labels inside ``Gtk.EventBox``es rather than
        ``Gtk.Button``s: a button's theme padding would shift every title a
        few pixels off its column, and there is no border to want. A sortable
        column reserves a fixed slot for its direction arrow (empty unless it
        is the active sort) so activating a sort cannot change any width, and
        the arrow sits on the *outside* of a right-aligned title so the title's
        text edge stays over the numbers' edge.
        """
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                         spacing=SPACING)
        header.get_style_context().add_class("colheader")
        self._header_labels = {}
        self._header_arrows = {}
        for col in COLUMNS:
            cell, label, arrow = self._header_cell(col)
            group = self._groups.get(col.key)
            if group is not None:
                group.add_widget(cell)
            holder = Gtk.EventBox()
            holder.set_visible_window(False)
            holder.add(cell)
            if col.sort_key:
                holder.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
                holder.connect("button-press-event", self._on_header_click,
                               col.sort_key)
            header.pack_start(holder, col.expand, col.expand, 0)
            self._header_labels[col.key] = label
            self._header_arrows[col.key] = arrow
        self._header_spacer = Gtk.Box()
        header.pack_start(self._header_spacer, False, False, 0)
        return header

    def _header_cell(self, col):
        """(cell widget, title label, arrow label or None) for one column."""
        label = Gtk.Label()
        label.set_text(col.title)
        label.set_xalign(col.xalign)
        label.get_style_context().add_class("colhead")
        if col.key in _MONO_COLUMNS:
            label.get_style_context().add_class("mono")
        arrow = None
        if col.sort_key:
            arrow = Gtk.Label()
            arrow.set_xalign(0.5)
            arrow.set_size_request(ARROW_WIDTH, -1)
            arrow.get_style_context().add_class("colhead")
            cell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
            if col.xalign >= 1.0:
                # right-aligned numbers: the arrow goes left of the title so
                # the title's right edge stays over the values' right edge
                cell.pack_start(arrow, False, False, 0)
                cell.pack_start(label, True, True, 0)
            else:
                cell.pack_start(label, True, True, 0)
                cell.pack_start(arrow, False, False, 0)
        else:
            label.set_size_request(col.width, -1)
            cell = label
        if col.key == "name":
            # indent past the dot so the title sits over the names
            outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                            spacing=DOT_GAP)
            spacer = Gtk.Box()
            spacer.set_size_request(DOT_SIZE, -1)
            outer.pack_start(spacer, False, False, 0)
            outer.pack_start(cell, True, True, 0)
            cell = outer
        return cell, label, arrow

    def set_status(self, text: str, error: bool = False) -> None:
        """One line of plain text (set_text: markup is never interpreted);
        ``error`` paints it in the launcher red, as systemlist's footer."""
        self._status.set_text(str(text))
        ctx = self._status.get_style_context()
        if error:
            ctx.add_class("error")
        else:
            ctx.remove_class("error")

    # -- the keyboard (window.py routes the keys here; D-23) ----------------
    def focus_search(self) -> None:
        """Give the search field the focus without selecting its text."""
        self._filter.grab_focus_without_selecting()

    def clear_search(self) -> bool:
        """Empty the search field; False when it was already empty (the
        window then closes on that Escape, systemlist's second Escape)."""
        if not self._filter.get_text():
            return False
        self._filter.set_text("")  # "changed" re-filters and refreshes
        return True

    def toggle_halted(self) -> None:
        """Ctrl+H: flip the "All qubes" check ("toggled" does the rest)."""
        self._halt.set_active(not self._halt.get_active())

    def move_selection(self, delta: int) -> None:
        """Up/Down/Page Up/Page Down: step the selection through the visible
        rows, report it like a click, and scroll it into view."""
        name = self._model.step_selection(delta)
        if name is None:
            return
        self._update_selection()
        self._scroll_to(name)
        if self._on_select is not None:
            self._on_select(name)

    def _scroll_to(self, name: str) -> None:
        row = self._rows.get(name)
        if row is None:
            return
        alloc = row.get_allocation()
        self._scroller.get_vadjustment().clamp_page(alloc.y,
                                                    alloc.y + alloc.height)

    # -- the refresh (in place) --------------------------------------------
    def refresh(self) -> None:
        """Re-render from the model, updating the row widgets in place. The
        scroll position is captured and restored; row *widgets* are reused (a
        gone qube's widget is removed, a new qube's is created once, a
        persistent qube's is repainted), so selection and scroll survive
        refresh and qube add/remove."""
        vadj = self._scroller.get_vadjustment()
        saved = vadj.get_value()
        visible = self._model.visible_rows()
        by_name = {s.name: s for s in visible}
        order = [s.name for s in visible]
        # remove rows whose qube is gone (or filtered/halted out)
        for name in list(self._rows):
            if name not in by_name:
                self._rowbox.remove(self._rows.pop(name))
        # create any missing row widgets (packed in order, so a brand-new list
        # is already sorted)
        for name in order:
            if name not in self._rows:
                row = _QubeRow(by_name[name], self._groups)
                row.connect("button-press-event", self._on_row_click, name)
                self._rows[name] = row
                self._rowbox.pack_start(row, False, False, 0)
                # show_ALL, not show(): GTK3 visibility is not inherited, so
                # show() reveals the row box but none of its labels/graphs —
                # every row created after the window's show_all() rendered as
                # an empty strip (the "window opens but lists no VMs" bug).
                row.show_all()
        # re-order only when the visible order changed (a GTK box has no move
        # API; remove + re-pack reuses the same widgets — not a rebuild)
        if order != self._last_order:
            for name in order:
                self._rowbox.remove(self._rows[name])
            for name in order:
                self._rowbox.pack_start(self._rows[name], False, False, 0)
                self._rows[name].show_all()
            self._last_order = order
        # update every visible row in place (repaint the same widget)
        for name in order:
            self._rows[name].apply(by_name[name])
        # restore the scroll position, then the selection highlight + header
        vadj.set_value(saved)
        self._update_selection()
        self._update_header()

    # -- widget callbacks --------------------------------------------------
    def _on_scroller_allocated(self, widget, alloc) -> None:
        """Reserve the vertical scrollbar's width at the end of the header.

        The header lives outside the scroller (so it does not scroll away),
        but the rows live inside: when the scrollbar is visible the rows are
        narrower than the pane by exactly the bar's width. Right-aligned
        columns make that visible as a constant offset. The difference between
        the scroller's allocation and its content child's is that width, with
        no guessing at scrollbar metrics.
        """
        child = self._scroller.get_child()
        if child is None:
            return
        content = child.get_allocated_width()
        if alloc.width <= 0 or content <= 0:
            return
        extra = max(0, alloc.width - content)
        if extra != self._header_extra:
            self._header_extra = extra
            self._header_spacer.set_size_request(extra, -1)

    def _on_row_click(self, widget, event, name: str) -> bool:
        # Only the primary button selects; anything else is left alone. TRUE
        # consumes the press so it cannot also travel on to the scroller.
        if getattr(event, "button", 1) != 1:
            return False
        self._model.select(name)
        self._update_selection()
        if self._on_select is not None:
            self._on_select(name)
        return True

    def _on_header_click(self, widget, event, key: str) -> bool:
        # A header click re-sorts that column (same rule as any table: the
        # active column flips, a new one takes its default direction).
        if getattr(event, "button", 1) != 1:
            return False
        self._model.toggle_sort(key)
        self.refresh()
        return True

    def _on_filter_changed(self, entry: Gtk.SearchEntry) -> None:
        self._model.set_filter(entry.get_text())
        self.refresh()

    def _on_halt_toggled(self, btn: Gtk.CheckButton) -> None:
        self._model.set_show_halted(btn.get_active())
        self.refresh()

    def _update_selection(self) -> None:
        sel = self._model.selected()
        for name, row in self._rows.items():
            ctx = row.get_style_context()
            if name == sel:
                ctx.add_class("selected")
            else:
                ctx.remove_class("selected")

    def _update_header(self) -> None:
        key = self._model.sort_key()
        direction = self._model.sort_dir()
        arrow = "\u2193" if direction == -1 else "\u2191"
        for col in COLUMNS:
            slot = self._header_arrows.get(col.key)
            if slot is None:
                continue
            slot.set_text(arrow if col.sort_key == key else "")
