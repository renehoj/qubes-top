"""The info group box under the qube list (D-18).

Owner: *"Before the bottom graphs make a info group box, and move the info
fields from the right side to the box"*. The detail pane's two label/value
grids — the qube's template / netvm / vcpus / mem max, and the memory facts
(allocated now, used, swap, report state, domid) — now live in one titled frame
under the list, above the charts. The detail pane keeps the identity line (dot,
name, state) and the process table, so the right side is the table.

Like the charts, this is a pure renderer of the pane's in-place-refreshed
``DetailSpec``: ``update(spec)`` copies the display strings the core already
formatted. Nothing is computed here, and nothing is inferred: a field the
provider could not read arrives as the spec's ``NA`` placeholder and is shown
as such.
"""
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Pango  # noqa: E402  (pin above is intentional)

from qubes_top.ui.rightspec import DetailSpec

__all__ = ["InfoBox", "FIELDS"]

#: ``(spec attribute, label)`` in reading order: what the qube *is* first, then
#: its memory figures, then the report state. Two per row.
FIELDS = (
    ("template", "template"),
    ("netvm", "netvm"),
    ("vcpus", "vcpus"),
    ("mem_max_text", "mem max"),
    ("allocated_text", "allocated now"),
    ("used_text", "used"),
    ("swap_text", "swap"),
    ("report_text", "report state"),
    ("domid_text", "domid"),
)


class InfoBox(Gtk.Box):
    """A titled frame of label/value pairs, fed from a ``DetailSpec``."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        frame = Gtk.Frame()
        frame.set_label_align(0.0, 0.5)
        frame.get_style_context().add_class("section")
        title = Gtk.Label(label="info")
        title.set_xalign(0.0)
        title.get_style_context().add_class("section-title")
        frame.set_label_widget(title)
        grid = Gtk.Grid(column_spacing=10, row_spacing=2)
        self._values = {}
        for i, (attr, text) in enumerate(FIELDS):
            col = (i % 2) * 2
            row = i // 2
            label = Gtk.Label(label=text)
            label.set_xalign(0.0)
            label.get_style_context().add_class("field-name")
            value = Gtk.Label()
            value.set_xalign(0.0)
            # a netvm name or a report-state sentence can be long; the pane can
            # be narrowed by dragging the divider, so the value ellipsises
            # rather than forcing the window wider
            value.set_ellipsize(Pango.EllipsizeMode.END)
            value.get_style_context().add_class("mono")
            grid.attach(label, col, row, 1, 1)
            grid.attach(value, col + 1, row, 1, 1)
            self._values[attr] = value
        frame.add(grid)
        self.pack_start(frame, False, False, 0)

    # -- the app's seam -----------------------------------------------------
    def update(self, spec: DetailSpec) -> None:
        """Write the spec's display strings (``set_text`` only — the values
        include guest- and provider-supplied text)."""
        for attr, _text in FIELDS:
            self._values[attr].set_text(getattr(spec, attr))

    def show_for(self, selected: bool) -> None:
        """Show while something is selected, hide otherwise. ``show_all``
        rather than ``show``: GTK3 visibility is not inherited, so showing the
        box alone would reveal an empty frame."""
        if selected == bool(self.get_visible()):
            return
        if selected:
            self.show_all()
        else:
            self.hide()
