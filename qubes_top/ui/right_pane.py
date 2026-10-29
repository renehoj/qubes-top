"""The right-pane widget (card t_b14eabf7). GTK3, dom0-only.

Renders the pure ``DetailSpec`` / ``ProcRow`` data (built by
``qubes_top.ui.rightspec`` from the core formatters) for the selected
qube. After D-17/D-18 the pane is deliberately small: the history charts
and the info fields live **under the qube list**, and what is left here
is the identity line and the process table.

- **identity line:** label colour, name, state (the table needs to say
  which qube it belongs to);
- **processes panel:** dom0's own table (``/proc``, read every tick) when
  the host is selected; for a guest, the selected qube's own ``top`` read
  through ``qvm-run`` by a background poller (D-15). No title bar and no
  sort hint — the owner removed both: the column titles are themselves
  clickable (D-16), and an absent table still carries its reason rather
  than an empty list;

**In-place contract** (the card's "a selection change must not reset
scroll position, the history window, or the sort order of the process
list"):

- one persistent ``DetailSpec``, updated field by field — the chart
  widgets re-draw the same areas; the history window lives in the
  ``SampleModel`` rings, which carry no selection state, so a selection
  change re-fetches the same retained series (the card's "do not
  compute percentages here; take them from ``qubes_top/core``" — this
  module does only display-bounds + pixel mapping via the core
  geometry, never metric arithmetic);
- the process-table row widgets are keyed by pid and *reused* (a new pid
  is created once, a gone pid's widget is removed, a persistent pid is
  re-painted; repacked only when the order changed);
- the sort (column + direction) is widget state, never touched by a
  refresh or a selection change (only a header click changes it);
- the detail page and the process table are *stack pages that stay
  hidden, not destroyed*: a selection change (or the qube going "gone")
  hides a page, so the process scroll position and the charts' last data
  survive until the data returns.

**A qube that disappears while selected:** the stack shows the "gone"
page — the detail is not painted (no stale chart ticking) and nothing is
cleared; if the qube reappears, the retained spec + series restore the
pane in one frame. No crash path either: the spec is only updated from
a present, non-``None`` record.

**Untrusted strings** (the qube name, the process comm, the template/
netvm names) go through ``set_text`` only, never ``set_markup``; they
are capped (``rightspec.cap``) before they reach a label, and the name
additionally ellipsises visually. No shell, no interpolation, no
``qvm-run``.

**GTK3 pin (both namespaces, Gtk first):** dom0 ships GTK4, so a bare
``Gdk`` import resolves to 4.0 and the later Gtk 3.0 import dies on the
target (a failure a gi-less dev box cannot reproduce). The pin is
repeated at the top of every gi module by contract.

Nothing here has executed anywhere until the owner runs it in dom0.
"""
import math

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, Pango  # noqa: E402  (pin above is intentional)

from qubes_top.core import label_color
from qubes_top.ui.layout import ARROW_WIDTH, PROC_COLUMNS, SPACING
from qubes_top.ui.rightspec import DetailSpec, ProcRow, proc_display
from qubes_top.ui.theme import PANE_CLASS

__all__ = ["RightPane"]

#: The exclusive page names of the main stack (staying hidden, not
#: destroyed — see the module docstring's in-place contract).
_EMPTY, _GONE, _DETAIL = "empty", "gone", "detail"

#: The process-table stack pages.
_TAB_TABLE, _TAB_NA = "table", "na"

def _mono(label: Gtk.Label) -> Gtk.Label:
    label.get_style_context().add_class("mono")
    return label


def _bold(label):
    """Bold a label without markup.

    ``Gtk.Label`` has no ``get_pango_attributes()`` — the target's first GUI
    run raised exactly that. A Label takes a whole ``Pango.AttrList`` through
    ``set_attributes``. Cosmetic only: if the attribute API is missing, the
    label stays plain and the window still opens.
    """
    try:
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
        label.set_attributes(attrs)
    except (AttributeError, TypeError):
        pass


class _ProcRow(Gtk.Box):
    """One process row: one cell per ``PROC_COLUMNS`` entry, left to right.

    Each cell joins the ``Gtk.SizeGroup`` for its column (shared with the
    header and every other row — the same mechanism as the qube list), so a
    value wider than its minimum widens the column everywhere instead of
    shifting what follows it. Reused across ticks: ``apply`` repaints the
    same widget in place (``set_text`` only — never ``set_markup``).
    """

    def __init__(self, groups=None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING)
        self._labels = {}
        groups = groups if groups is not None else {}
        for col in PROC_COLUMNS:
            label = Gtk.Label()
            label.set_xalign(col.xalign)
            label.set_size_request(col.width, -1)
            if col.key != "name":
                _mono(label)  # digits don't jitter under a proportional font
            else:
                # a command name is guest/kernel text: ellipsise, never grow
                label.set_ellipsize(Pango.EllipsizeMode.END)
                if col.max_chars:
                    label.set_max_width_chars(col.max_chars)
            group = groups.get(col.key)
            if group is not None:
                group.add_widget(label)
            self.pack_start(label, False, False, 0)
            self._labels[col.key] = label

    def apply(self, row: ProcRow) -> None:
        self._labels["pid"].set_text(str(row.pid))
        self._labels["name"].set_text(row.name)
        self._labels["cpu"].set_text(row.cpu_text)
        self._labels["rss"].set_text(row.rss_text)


class RightPane(Gtk.Box):
    """The right pane: detail for the selected qube. Pure renderer — the
    app feeds it one ``update()`` per frame (and on selection); it owns
    the widget lifecycle (in-place rows, scroll, sort wiring) and nothing
    metric."""

    def __init__(self, color_for=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._color_for = color_for if color_for is not None else label_color
        self._spec = DetailSpec()
        self._mode = _EMPTY
        # process state — persists across selection changes (the card's
        # "a selection change must not reset ... the sort order of the
        # process list"): the sort is touched only by a header click.
        self._proc_sort_key = "cpu"
        self._proc_sort_dir = -1  # -1 = descending (top's default)
        self._proc_rows = {}      # pid -> _ProcRow (the in-place row widgets)
        self._proc_last_order = []
        self._last_proc_rows: list = []
        # One size group per column, shared by the proc header and every row:
        # the same alignment mechanism the qube list uses.
        self._proc_groups = {c.key: Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
                             for c in PROC_COLUMNS}
        self._proc_header_extra = -1
        # systemlist's inner pane (D-23), the same as the list beside it
        self.get_style_context().add_class(PANE_CLASS)

        # the exclusive pages (they stay hidden, never destroyed)
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.NONE)
        empty = Gtk.Label(label="select a qube")
        empty.get_style_context().add_class("placeholder")
        empty.set_halign(Gtk.Align.CENTER)
        empty.set_valign(Gtk.Align.CENTER)
        gone = Gtk.Label(label="gone")
        gone.get_style_context().add_class("placeholder")
        gone.set_halign(Gtk.Align.CENTER)
        gone.set_valign(Gtk.Align.CENTER)
        self._stack.add_named(empty, _EMPTY)
        self._stack.add_named(gone, _GONE)
        self._stack.add_named(self._build_detail(), _DETAIL)
        self.pack_start(self._stack, True, True, 0)
        self._stack.set_visible_child_name(_EMPTY)

    @property
    def spec(self) -> DetailSpec:
        """The live display spec. The charts under the list render the *same*
        object (refreshed in place), so one selection drives both."""
        return self._spec

    # -- the detail page --------------------------------------------------
    def _build_detail(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        # -- header --------------------------------------------------------
        hdr = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        hdr.get_style_context().add_class("detail-header")
        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._dot = Gtk.DrawingArea()
        self._dot.set_size_request(14, 14)
        self._dot.connect("draw", self._on_dot_draw)
        row1.pack_start(self._dot, False, False, 0)
        self._name_label = Gtk.Label()
        self._name_label.set_xalign(0.0)
        self._name_label.set_ellipsize(Pango.EllipsizeMode.END)
        # bold via a Pango attribute list (set_text stays markup-free)
        _bold(self._name_label)
        row1.pack_start(self._name_label, True, True, 0)
        self._state_label = Gtk.Label()
        self._state_label.set_xalign(0.0)
        self._state_label.get_style_context().add_class("detail-state")
        row1.pack_start(self._state_label, False, False, 0)
        hdr.pack_start(row1, False, False, 0)
        # The two label/value grids that used to sit here (template / netvm
        # / vcpus / mem max, and the memory facts) are the **info box
        # under the list** now (D-18): the owner asked for them beside the
        # list, above the charts. What stays on the right is the identity
        # line above (so the process table still says which qube it is) and
        # the table itself.
        box.pack_start(hdr, False, False, 0)
        # -- processes panel -----------------------------------------------
        # No title bar and no hint: the owner asked for the "processes — guest"
        # line and the "(click a column to re-sort)" text to go, and with both
        # gone the bar had nothing left in it. The column titles carry the
        # sort affordance (they are clickable) and the absence page still
        # explains itself when there is no table.
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._proc_stack = Gtk.Stack()
        self._proc_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self._proc_stack.add_named(self._build_proc_table(), _TAB_TABLE)
        # The absence page carries a *reason* (never reading / no `top` in
        # the guest / qvm-run failed): "empty" and "broken" must not look
        # the same, and a guest is a different process source from dom0.
        self._proc_na = Gtk.Label(label="not available for this qube")
        self._proc_na.get_style_context().add_class("placeholder")
        self._proc_na.set_halign(Gtk.Align.CENTER)
        self._proc_na.set_valign(Gtk.Align.CENTER)
        self._proc_na.set_line_wrap(True)
        self._proc_na.set_justify(Gtk.Justification.CENTER)
        self._proc_na.set_max_width_chars(48)
        self._proc_stack.add_named(self._proc_na, _TAB_NA)
        pbox.pack_start(self._proc_stack, True, True, 0)
        self._proc_stack.set_visible_child_name(_TAB_NA)
        box.pack_start(pbox, True, True, 0)
        return box

    def _build_proc_table(self) -> Gtk.Box:
        """The process table page: a column header over the scrollable,
        in-place-updating row list, both built from ``PROC_COLUMNS`` and
        sharing one size group per column.

        The header lives outside the scroller (so it does not scroll away),
        which means it must reserve the vertical scrollbar's width at its end
        — without that, every column after the first shifts left by ~14 px as
        soon as the bar appears, which on a 490-row table is always. The table
        *widget persists* across selection changes (the stack page stays
        hidden), so the scroll position and the row widgets survive a guest
        selection in between.
        """
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        page.pack_start(self._build_proc_header(), False, False, 0)
        self._proc_scroller = Gtk.ScrolledWindow()
        self._proc_scroller.set_policy(Gtk.PolicyType.AUTOMATIC,
                                      Gtk.PolicyType.AUTOMATIC)
        self._proc_rowbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                   spacing=1)
        self._proc_rowbox.get_style_context().add_class("rows")
        self._proc_scroller.add(self._proc_rowbox)
        self._proc_scroller.connect("size-allocate",
                                    self._on_proc_scroller_allocated)
        page.pack_start(self._proc_scroller, True, True, 0)
        return page

    def _build_proc_header(self) -> Gtk.Box:
        """The process table's column titles, one per ``PROC_COLUMNS`` entry.

        Plain labels in ``Gtk.EventBox``es rather than ``Gtk.Button``s: a
        button's theme padding offsets its text from the values underneath,
        and the whole point is that a title sits over its column. A sortable
        title reserves a fixed slot for its arrow (empty when inactive) on the
        outside of a right-aligned title, so activating a sort cannot change a
        width.
        """
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACING)
        head.get_style_context().add_class("colheader")
        self._proc_header_labels = {}
        self._proc_header_arrows = {}
        for col in PROC_COLUMNS:
            label = Gtk.Label()
            label.set_text(col.title)
            label.set_xalign(col.xalign)
            label.get_style_context().add_class("colhead")
            if col.key != "name":
                _mono(label)
            arrow = None
            if col.sort_key:
                arrow = Gtk.Label()
                arrow.set_xalign(0.5)
                arrow.set_size_request(ARROW_WIDTH, -1)
                arrow.get_style_context().add_class("colhead")
                cell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                               spacing=2)
                if col.xalign >= 1.0:
                    cell.pack_start(arrow, False, False, 0)
                    cell.pack_start(label, True, True, 0)
                else:
                    cell.pack_start(label, True, True, 0)
                    cell.pack_start(arrow, False, False, 0)
            else:
                label.set_size_request(col.width, -1)
                cell = label
            self._proc_groups[col.key].add_widget(cell)
            holder = Gtk.EventBox()
            holder.set_visible_window(False)
            holder.add(cell)
            if col.sort_key:
                holder.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
                holder.connect("button-press-event",
                               self._on_proc_header_click, col.sort_key)
            head.pack_start(holder, False, False, 0)
            self._proc_header_labels[col.key] = label
            self._proc_header_arrows[col.key] = arrow
        # no "(click a column to re-sort)" hint: the owner asked for it gone,
        # and the titles are self-evidently clickable once you use them
        self._proc_header_spacer = Gtk.Box()
        head.pack_start(self._proc_header_spacer, False, False, 0)
        return head

    def _on_proc_scroller_allocated(self, widget, alloc) -> None:
        """Reserve the vertical scrollbar's width at the end of the header
        (the same measurement the qube list uses: the scroller's allocation
        minus its content child's)."""
        child = self._proc_scroller.get_child()
        if child is None:
            return
        content = child.get_allocated_width()
        if alloc.width <= 0 or content <= 0:
            return
        extra = max(0, alloc.width - content)
        if extra != self._proc_header_extra:
            self._proc_header_extra = extra
            self._proc_header_spacer.set_size_request(extra, -1)

    # -- the process panel's labels (set by the app per selection) ---------
    def set_proc_note(self, text: str) -> None:
        """The reason the table is not shown. Plain text (set_text): the text
        can carry a ``qvm-run`` error line, i.e. guest-influenced bytes."""
        self._proc_na.set_text(str(text)[:240])

    # -- the refresh (in place) --------------------------------------------
    def update(self, name, present: bool, record, mem_state,
               cpu_values, mem_points, meta, proc_rows) -> None:
        """One frame's (or one selection's) update for the pane.

        ``present`` is False (or the record ``None``) with a non-``None``
        name: the selected qube is gone — show "gone" (the detail page
        is hidden, not reset). ``name`` is ``None``: show "empty".
        Otherwise: the spec is updated in place and every label redrawn;
        the process panel switches to the table for a host reading and to
        the "not available" page for a guest (the table widget stays
        alive, so the scroll position survives)."""
        if name is None:
            self._set_mode(_EMPTY)
            return
        if not present or record is None or mem_state is None:
            self._set_mode(_GONE)
            return
        s = self._spec
        s.refresh(name, record, mem_state, cpu_values, mem_points, meta,
                  self._color_for)
        # header (set_text only — the name/state are guest-supplied)
        self._name_label.set_text(s.name)
        self._state_label.set_text(s.state)
        self._dot.queue_draw()
        self._set_mode(_DETAIL)
        # the process panel (the table stays a hidden stack page — the
        # scroll position survives the guest selections in between)
        if proc_rows is None:
            self._proc_stack.set_visible_child_name(_TAB_NA)
        else:
            self._proc_stack.set_visible_child_name(_TAB_TABLE)
            self._last_proc_rows = list(proc_rows)
            self._update_proc_table()

    # -- the process table (in place) ---------------------------------------
    def _on_proc_header_click(self, widget, event, key: str) -> bool:
        """A click on a column title (the cell is an ``EventBox``, since a
        Button's padding would offset the title from its values). Returning
        True swallows the event; a non-left button is ignored."""
        if getattr(event, "button", 1) != 1:
            return False
        self._on_sort(key)
        return True

    def _on_sort(self, key: str) -> None:
        """A header click re-sorts: clicking the *active* column flips its
        direction; clicking a *new* column selects it descending (top's
        default). The sort is widget state: a refresh or a selection
        change never touches it (the card's requirement)."""
        if key == self._proc_sort_key:
            self._proc_sort_dir = -self._proc_sort_dir
        else:
            self._proc_sort_key = key
            self._proc_sort_dir = -1
        self._update_proc_table()

    def _update_proc_table(self) -> None:
        """Re-render the process table from the last supplied rows with
        the *current* sort, updating the row widgets in place (the same
        ``_QubeRow`` pattern as the left pane: remove gone, create new
        once, repaint persistent, repack only when the order changed)."""
        vadj = self._proc_scroller.get_vadjustment()
        saved = vadj.get_value()
        display = proc_display(self._last_proc_rows, self._proc_sort_key,
                              self._proc_sort_dir)
        by_pid = {r.pid for r in display}
        order = [r.pid for r in display]
        for pid in list(self._proc_rows):
            if pid not in by_pid:
                self._proc_rowbox.remove(self._proc_rows.pop(pid))
        for r in display:
            if r.pid not in self._proc_rows:
                row = _ProcRow(self._proc_groups)
                self._proc_rows[r.pid] = row
                self._proc_rowbox.pack_start(row, False, False, 0)
                # show_all: the same visibility trap as the qube rows — a
                # bare show() reveals the row and none of its labels.
                row.show_all()
        if order != self._proc_last_order:
            for r in display:
                self._proc_rowbox.remove(self._proc_rows[r.pid])
            for r in display:
                self._proc_rowbox.pack_start(self._proc_rows[r.pid],
                                            False, False, 0)
                # _proc_rows is the {pid: row} map; _proc_rowbox is the
                # container Box. Indexing the container was the dom0 crash.
                self._proc_rows[r.pid].show_all()
            self._proc_last_order = order
        for r in display:
            self._proc_rows[r.pid].apply(r)
        vadj.set_value(saved)
        self._update_proc_header()

    def _update_proc_header(self) -> None:
        """Move the sort arrow to the active column — the arrow lives in its
        own fixed-width slot, so no title ever changes width when the sort
        moves (that was the qube list's first header bug)."""
        arrow = "\u2193" if self._proc_sort_dir == -1 else "\u2191"
        for col in PROC_COLUMNS:
            slot = self._proc_header_arrows.get(col.key)
            if slot is None:
                continue
            slot.set_text(arrow if col.sort_key == self._proc_sort_key else "")

    # -- the exclusive pages ------------------------------------------------
    def _set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self._stack.set_visible_child_name(
            { _EMPTY: _EMPTY, _GONE: _GONE, _DETAIL: _DETAIL }[mode])

    # -- drawing (the only "arithmetic" is display bounds + the core's
    #    geometry — never metric) ---------------------------------------------
    def _on_dot_draw(self, area: Gtk.DrawingArea, cr) -> bool:
        rgba = Gdk.RGBA()
        rgba.parse(self._spec.color)
        cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 1.0)
        cr.arc(7.0, 7.0, 6.0, 0.0, 2.0 * math.pi)
        cr.fill()
        # The same faint ring the row dots carry (D-20): the colour is the
        # real Qubes label, and a ``black`` label (dom0's default) would
        # otherwise vanish on a dark theme.
        cr.set_line_width(1.0)
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.55)
        cr.arc(7.0, 7.0, 6.0, 0.0, 2.0 * math.pi)
        cr.stroke()
        return False
