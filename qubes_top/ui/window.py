"""The top-level window (card t_4d9516cd; the right pane landed in
card t_b14eabf7). GTK3, dom0-only.

A ``Gtk.Paned`` (horizontal) with the left pane (the qube list) and the
**right pane** (the detail for the selected qube: header, the larger
history charts, the facts block, the process panel — ``RightPane``). The
divider position is persisted to ``~/.config/qubes-top/`` via the pure
``PanedConfig``.

**The look and the keys are qubes-systemlist's (D-23).** The window is
undecorated and themed by ``theme.py`` (the stock Qubes menu's colours, the
launcher red selection, translucency on a compositing screen); both columns
sit on systemlist's rounded inner pane, with the outer background showing in
the margin and the divider. Without a title bar the window is moved by
dragging that background and resized by dragging its edge (``edge_at``). It
stays an ordinary window otherwise — in the taskbar, not kept above, not
hidden on focus loss — because it is a monitor you leave open (D-8).

The keys, as systemlist's list: typing goes into the search field, Up/Down and
Page Up/Page Down move the selection, Ctrl+H shows halted qubes, Escape clears
the search and a second Escape closes the window, Ctrl+Q closes it.

**The hard requirement (recorded by the card, from the previous project's live
failure):** a GTK3 window whose *children* are shown but whose *ancestors* are
not opens **blank**, with a green suite — GTK3 visibility is not inherited, so
showing a child never reveals its parent. The fix is ``show_all()`` on the
*window itself*, called **after** the call that populates the rows. That is
``_populate()`` (which seeds the left pane) followed by ``self.show_all()`` in
``__init__``. A child's ``show_all`` does not count; only the window's does —
and it covers the whole tree, including the right pane's widgets.
"""
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402  (pin above is intentional)

from qubes_top.ui.charts import HistoryCharts
from qubes_top.ui.config import PanedConfig
from qubes_top.ui import theme
from qubes_top.ui.infobox import InfoBox
from qubes_top.ui.layout import (WINDOW_HEIGHT, WINDOW_MARGIN, WINDOW_WIDTH,
                               edge_at)
from qubes_top.ui.left_pane import PAGE_ROWS, LeftPane
from qubes_top.ui.model import LeftPaneModel
from qubes_top.ui.right_pane import RightPane

# The debounce window for persisting the divider position during a drag.
_PANED_SAVE_MS = 250

#: ``edge_at``'s answer -> the resize drag's edge and the pointer shape over it.
_EDGES = {
    "n": (Gdk.WindowEdge.NORTH, "n-resize"),
    "s": (Gdk.WindowEdge.SOUTH, "s-resize"),
    "e": (Gdk.WindowEdge.EAST, "e-resize"),
    "w": (Gdk.WindowEdge.WEST, "w-resize"),
    "ne": (Gdk.WindowEdge.NORTH_EAST, "ne-resize"),
    "nw": (Gdk.WindowEdge.NORTH_WEST, "nw-resize"),
    "se": (Gdk.WindowEdge.SOUTH_EAST, "se-resize"),
    "sw": (Gdk.WindowEdge.SOUTH_WEST, "sw-resize"),
}

#: The selection keys and how far each one moves.
_STEPS = {
    Gdk.KEY_Up: -1,
    Gdk.KEY_Down: 1,
    Gdk.KEY_Page_Up: -PAGE_ROWS,
    Gdk.KEY_Page_Down: PAGE_ROWS,
}


class QubesTopWindow(Gtk.Window):
    def __init__(self, model: LeftPaneModel, on_select=None,
                 color_for=None) -> None:
        super().__init__(title="qubes-top")
        theme.set_chrome(self)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_default_size(WINDOW_WIDTH, self._default_height())
        self.set_border_width(WINDOW_MARGIN)
        # before the first realise, or the window stays opaque for good
        self._translucent = theme.prepare_transparency(self)
        self._config = PanedConfig()
        self._paned_save_pending = None
        self._edge = None  # the edge under the pointer (its cursor is set)

        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._left = LeftPane(model, on_select=on_select)
        # The large history charts sit *under the qube list* (D-17: the owner
        # asked for "the large graphs ... below the VM list"), with the info
        # box above them (D-18: "before the bottom graphs make a info group
        # box"). So the left column is the list, the info box, then the
        # charts; the right pane is left to the identity line and the process
        # table.
        self._info = InfoBox()
        self._charts = HistoryCharts()
        self._left_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._left_col.get_style_context().add_class(theme.PANE_CLASS)
        self._left_col.pack_start(self._left, True, True, 0)
        self._left_col.pack_start(self._info, False, False, 0)
        self._left_col.pack_start(self._charts, False, False, 0)
        # The right pane (card t_b14eabf7): the detail for the selected
        # qube (header, facts, process panel). Its widgets become visible
        # through the window's show_all() below — the hard requirement.
        # ``color_for`` is the same label-colour source the rows use (D-20),
        # so the identity dot here and the row dot cannot disagree.
        self._right = RightPane(color_for=color_for)

        self._paned.pack1(self._left_col, True, False)  # left: resize, not shrink
        self._paned.pack2(self._right, True, True)      # right: resize + shrink
        self._paned.set_position(self._config.load())
        self._paned.connect("notify::position", self._on_paned_position)
        self.add(self._paned)

        self.connect("destroy", self._on_destroy)
        self.connect("delete-event", self._on_delete)
        self.connect("key-press-event", self._on_key_press)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("button-press-event", self._on_button_press)
        self.connect("motion-notify-event", self._on_motion)
        theme.apply(self, self._translucent)

        # Populate the rows *first* ...
        self._populate()
        # ... then show the WINDOW itself (the hard requirement; a child's
        # show_all would leave the window hidden and the window would open
        # blank).
        self.show_all()
        # show_all() has just revealed the info box and the charts; with
        # nothing selected they must not take room from the list (the app
        # re-shows them on the first selection).
        self._info.show_for(False)
        self._charts.show_for(False)
        # the search field holds the focus, so typing always searches
        self._left.focus_search()

    @staticmethod
    def _default_height() -> int:
        """``WINDOW_HEIGHT``, or less on a screen whose work area (the screen
        minus panels) is shorter, so the window never opens past its edge."""
        display = Gdk.Display.get_default()
        if display is None:
            return WINDOW_HEIGHT
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is None:
            return WINDOW_HEIGHT
        area = monitor.get_workarea()
        return max(480, min(WINDOW_HEIGHT, area.height - 40))

    def _populate(self) -> None:
        """Seed the left pane with the current model state so the window opens
        with rows (not an empty window). The window's ``show_all()`` follows
        this call — never before it."""
        self._left.refresh()

    def refresh(self) -> None:
        """Public forwarder so the composition root (the app, which owns the
        refresh loop) does not reach into ``_left``. Re-renders the left pane
        from the model in place (the in-place contract)."""
        self._left.refresh()

    def set_status(self, text: str, error: bool = False) -> None:
        """Forward the collector's health to the left pane's footer."""
        self._left.set_status(text, error)

    @property
    def right_pane(self) -> RightPane:
        """The right pane (the composition root feeds it one ``update()``
        per frame / per selection). Public by the same reasoning as
        ``refresh``: the app must not reach into ``_right``."""
        return self._right

    @property
    def charts(self) -> HistoryCharts:
        """The large charts under the list. The app points them at the pane's
        spec and says whether anything is selected."""
        return self._charts

    @property
    def info(self) -> InfoBox:
        """The info box above the charts (D-18): the same spec, the same
        visibility contract."""
        return self._info

    # -- the keyboard (systemlist's list keys, D-23) ------------------------
    def _on_key_press(self, widget, event) -> bool:
        """Runs before the focused widget sees the key, so the arrows move the
        selection even though the focus is in the search field."""
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and event.keyval in (Gdk.KEY_q, Gdk.KEY_Q):
            self.close()
            return True
        if ctrl and event.keyval in (Gdk.KEY_h, Gdk.KEY_H):
            self._left.toggle_halted()
            return True
        if event.keyval == Gdk.KEY_Escape:
            if not self._left.clear_search():
                self.close()
            return True
        step = _STEPS.get(event.keyval)
        if step is not None:
            self._left.move_selection(step)
            return True
        # Anything printable belongs to the search field, wherever the focus
        # wandered (a click on the divider can take it). Returning False lets
        # the window hand the key to the newly focused field.
        if Gdk.keyval_to_unicode(event.keyval) and not ctrl:
            self._left.focus_search()
        return False

    # -- moving and resizing without a title bar ---------------------------
    def _own_edge(self, event):
        """The edge under a pointer event on the window's *own* surface, or
        None. Events from a child's surface carry that child's coordinates."""
        if event.window != self.get_window():
            return None
        return edge_at(event.x, event.y, self.get_allocated_width(),
                       self.get_allocated_height())

    def _on_button_press(self, widget, event) -> bool:
        # Only presses nothing else took: rows and column titles consume
        # theirs, so this is the margin, the gaps and the pane backgrounds.
        if event.button != 1 or event.window != self.get_window():
            return False
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False  # the second half of a double click
        edge = self._own_edge(event)
        x, y = int(event.x_root), int(event.y_root)
        if edge is None:
            self.begin_move_drag(event.button, x, y, event.time)
        else:
            self.begin_resize_drag(_EDGES[edge][0], event.button, x, y,
                                   event.time)
        return True

    def _on_motion(self, widget, event) -> bool:
        edge = self._own_edge(event)
        if edge != self._edge:
            self._edge = edge
            cursor = None
            if edge is not None:
                cursor = Gdk.Cursor.new_from_name(self.get_display(),
                                                  _EDGES[edge][1])
            self.get_window().set_cursor(cursor)
        return False

    # -- paned position persistence (debounced during a drag) --------------
    def _on_paned_position(self, paned: Gtk.Paned, param) -> None:
        if self._paned_save_pending is None:
            self._paned_save_pending = GLib.timeout_add(
                _PANED_SAVE_MS, self._save_paned_position)

    def _save_paned_position(self) -> bool:
        self._paned_save_pending = None
        self._config.save(self._paned.get_position())
        return False  # one-shot timeout; do not reschedule

    # -- shutdown ---------------------------------------------------------
    def _on_destroy(self, widget) -> None:
        self._save_paned_position_now()
        Gtk.main_quit()

    def _on_delete(self, widget, event) -> bool:
        self._save_paned_position_now()
        Gtk.main_quit()
        return True  # the window is gone; do not propagate a second delete

    def _save_paned_position_now(self) -> None:
        if self._paned_save_pending is not None:
            GLib.source_remove(self._paned_save_pending)
            self._paned_save_pending = None
        self._config.save(self._paned.get_position())
