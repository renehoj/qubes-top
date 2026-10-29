"""The large history charts — now under the qube list (D-17).

Owner: *"move the right side memory and cpu graphs ... so when a VM is selected
the large graphs are shown below the VM list"*. They used to sit at the top of
the detail pane, above the facts block and the process table, and pushed both
down; under the list they are still beside the selection they describe, and the
detail pane is left to what changes every tick.

The widget is a pure renderer of a ``DetailSpec``: the right pane refreshes the
spec in place each frame, and ``update(spec)`` points the charts at it and
queues a redraw. Nothing here computes a metric — the geometry helpers in
``qubes_top.core`` do that.

Visibility follows the selection: hidden while nothing is selected (the list
gets the room back), frozen rather than cleared when the selected qube
disappears — the same contract as the detail page, which shows "gone" without
dropping its data.

**Drawn to be read (D-24).** Both charts share one fixed five-minute time axis
with "now" at the right edge, labelled underneath, so a line does not stretch
while the history fills and the CPU and memory charts line up second for
second. The y-axis has rounded gridlines with their values in a left gutter
(``core.axis_ticks``). The series is a 2 px line over a faint wash of its own
colour, with a dot on the newest value; the newest value is also written in
the top strip, beside a legend for the memory chart's three lines. Text is
drawn in the theme's own text colour, never in a series colour.
"""

import math
import time
from typing import Optional

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Pango  # noqa: E402  (pin above is intentional)

from qubes_top.core import (axis_ticks, format_bytes, format_percent,
                          format_tick, normalize, window_segments,
                          window_steps, window_x)
from qubes_top.ui.rightspec import DetailSpec
from qubes_top.ui.theme import ALLOC_RGBA, CPU_RGBA, MAX_RGBA, MEM_RGBA

__all__ = ["HistoryCharts", "CHART_H", "trace"]

#: The height of each chart, in pixels, **fixed** (a predictable list height;
#: D-17). It includes the top strip (legend, newest value) and the time axis.
CHART_H = 120

#: The plot's margins inside a chart: the y labels' gutter on the left, the
#: legend strip on top, the time labels underneath.
_GUTTER_L, _PAD_R, _STRIP_T, _AXIS_B = 46.0, 10.0, 16.0, 16.0
_AXIS_FONT_PX, _VALUE_FONT_PX = 9.0, 11.0

#: cairo's ROUND for line joins and caps (``cairo.LINE_JOIN_ROUND`` /
#: ``LINE_CAP_ROUND``); integers so this module imports without pycairo.
_ROUND = 1

#: The wash under a line: the series colour at this opacity.
_FILL_ALPHA = 0.14

#: KiB per GiB / MiB: the memory axis is labelled in GiB from 1 GiB up.
_GIB_KIB, _MIB_KIB = 1024.0 * 1024.0, 1024.0

_MINUS = "\u2212"


def trace(cr, points, base_y: float, rgba, line_width: float = 2.0,
          fill_alpha: float = _FILL_ALPHA) -> None:
    """Stroke a polyline and wash the area between it and ``base_y`` in the
    same colour. Fewer than two points draws nothing (an isolated value
    between two gaps has no line to draw; the end dot marks the newest)."""
    if len(points) < 2:
        return
    r, g, b, _a = rgba
    cr.new_path()
    cr.move_to(points[0][0], base_y)
    for x, y in points:
        cr.line_to(x, y)
    cr.line_to(points[-1][0], base_y)
    cr.close_path()
    cr.set_source_rgba(r, g, b, fill_alpha)
    cr.fill()
    cr.move_to(points[0][0], points[0][1])
    for x, y in points[1:]:
        cr.line_to(x, y)
    cr.set_line_join(_ROUND)
    cr.set_line_cap(_ROUND)
    cr.set_line_width(line_width)
    cr.set_source_rgba(*rgba)
    cr.stroke()


def _ink(area, alpha: float):
    """The theme's text colour at ``alpha``: chart text follows the light or
    dark theme like every label in the window, instead of a fixed grey."""
    c = area.get_style_context().get_color(Gtk.StateFlags.NORMAL)
    return (c.red, c.green, c.blue, alpha)


def _text(cr, text: str, x: float, y: float, rgba, size: float,
          align: float = 0.0, bold: bool = False) -> float:
    """Draw ``text`` with its baseline at ``y``; ``align`` 0 puts its left
    edge at ``x``, 1 its right edge, 0.5 its centre. Returns its width."""
    cr.select_font_face("sans-serif", 0, 1 if bold else 0)
    cr.set_font_size(size)
    ext = cr.text_extents(text)
    cr.set_source_rgba(*rgba)
    cr.move_to(x - ext.x_bearing - align * ext.width, y)
    cr.show_text(text)
    return ext.width


class _Plot:
    """The plot rectangle inside one chart's allocation."""

    def __init__(self, w: float, h: float) -> None:
        self.x0, self.y0 = _GUTTER_L, _STRIP_T
        self.x1, self.y1 = w - _PAD_R, h - _AXIS_B
        self.w, self.h = self.x1 - self.x0, self.y1 - self.y0

    def y_of(self, value: float, top: float) -> float:
        return self.y0 + (1.0 - normalize(value, 0.0, top)) * self.h

    def ok(self) -> bool:
        return self.w > 20.0 and self.h > 20.0


def _grid(cr, area, plot: _Plot, top: float, ticks, label) -> None:
    """Hairline gridlines at the ticks, their values right-aligned in the
    gutter; the baseline a step stronger than the rest."""
    cr.set_line_width(1.0)
    for v in ticks:
        y = round(plot.y_of(v, top)) + 0.5  # crisp: on the pixel grid
        cr.set_source_rgba(*_ink(area, 0.30 if v == 0 else 0.10))
        cr.move_to(plot.x0, y)
        cr.line_to(plot.x1, y)
        cr.stroke()
        _text(cr, label(v), plot.x0 - 6.0, y + 3.0, _ink(area, 0.6),
              _AXIS_FONT_PX, align=1.0)


def _time_axis(cr, area, plot: _Plot) -> None:
    """Minute ticks under the plot, "now" at the right; every minute is
    labelled when there is room, else only the two ends."""
    muted = _ink(area, 0.6)
    y = plot.y1 + 12.0
    labelled = plot.w / 5.0 >= 48.0
    cr.set_line_width(1.0)
    for minutes in range(6):
        x = round(plot.x0 + window_x(minutes * 60.0, plot.w, 0.0)) + 0.5
        cr.set_source_rgba(*_ink(area, 0.30))
        cr.move_to(x, plot.y1)
        cr.line_to(x, plot.y1 + 3.0)
        cr.stroke()
        if minutes == 0:
            _text(cr, "now", x, y, muted, _AXIS_FONT_PX, align=1.0)
        elif minutes == 5:
            _text(cr, "%s5 min" % _MINUS, x, y, muted, _AXIS_FONT_PX)
        elif labelled:
            _text(cr, "%s%d" % (_MINUS, minutes), x, y, muted,
                  _AXIS_FONT_PX, align=0.5)


def _legend(cr, area, plot: _Plot, items) -> None:
    """``(rgba, text)`` keys in the top strip: a short line in the series
    colour, then the name in the muted text colour."""
    x = plot.x0
    y = plot.y0 - 5.0
    for rgba, text in items:
        cr.set_source_rgba(*rgba)
        cr.set_line_width(2.0)
        cr.set_line_cap(_ROUND)
        cr.move_to(x, y - 3.0)
        cr.line_to(x + 12.0, y - 3.0)
        cr.stroke()
        x += 16.0
        x += _text(cr, text, x, y, _ink(area, 0.7), _AXIS_FONT_PX) + 12.0


def _end_dot(cr, x: float, y: float, rgba) -> None:
    cr.new_path()
    cr.arc(x, y, 3.5, 0.0, 2.0 * math.pi)
    cr.set_source_rgba(*rgba)
    cr.fill()


class HistoryCharts(Gtk.Box):
    """The two large charts: CPU as a continuous line, memory as a step with
    the live allocation and the max drawn as reference lines."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._spec: Optional[DetailSpec] = None
        self._cpu_area = self._chart("CPU — % of one core (may exceed 100%)",
                                     self._on_cpu_draw)
        self._mem_area = self._chart("memory", self._on_mem_draw)

    def _chart(self, title: str, handler) -> Gtk.DrawingArea:
        """One titled chart: a ``Gtk.Frame`` whose label is the title, holding
        a fixed-height drawing area."""
        frame = Gtk.Frame()
        frame.set_label_align(0.0, 0.5)
        frame.get_style_context().add_class("section")
        label = Gtk.Label()
        label.set_xalign(0.0)
        label.get_style_context().add_class("section-title")
        # the column can be narrower than the title after a drag of the divider
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_text(title)
        frame.set_label_widget(label)
        area = Gtk.DrawingArea()
        area.set_size_request(-1, CHART_H)
        area.connect("draw", handler)
        frame.add(area)
        self.pack_start(frame, False, False, 0)
        return area

    # -- the app's seam -----------------------------------------------------
    def update(self, spec: DetailSpec) -> None:
        """Point the charts at the pane's (in-place refreshed) spec."""
        self._spec = spec
        self._cpu_area.queue_draw()
        self._mem_area.queue_draw()

    def show_for(self, selected: bool) -> None:
        """Show while something is selected, hide otherwise.

        ``show_all`` rather than ``show``: GTK3 visibility is not inherited, so
        showing the box alone would reveal two empty frames. Idempotent — the
        app calls this on every tick, and a full ``show_all`` walk per frame is
        pointless work.
        """
        if selected == bool(self.get_visible()):
            return
        if selected:
            self.show_all()
        else:
            self.hide()

    # -- drawing ------------------------------------------------------------
    def _on_cpu_draw(self, area: Gtk.DrawingArea, cr) -> bool:
        spec = self._spec
        if spec is None:
            return False
        plot = _Plot(area.get_allocated_width(), area.get_allocated_height())
        if not plot.ok():
            return False
        top, ticks = axis_ticks(spec.cpu_hi)
        _grid(cr, area, plot, top, ticks, lambda v: format_tick(v) + "%")
        if 100.0 < top:
            # one full core (values may exceed it — D-3): stronger than a
            # gridline, so "above one core" reads at a glance
            y = round(plot.y_of(100.0, top)) + 0.5
            cr.set_source_rgba(*_ink(area, 0.35))
            cr.move_to(plot.x0, y)
            cr.line_to(plot.x1, y)
            cr.stroke()
        _time_axis(cr, area, plot)
        # the line (a None value — a first sample / a reset gap — splits it)
        cr.save()
        cr.translate(plot.x0, plot.y0)
        segs = window_segments(spec.cpu_values, plot.w, plot.h, 0.0, top, 0.0)
        for seg in segs:
            trace(cr, seg, plot.h, CPU_RGBA)
        latest = spec.cpu_values[-1] if spec.cpu_values else None
        if latest is not None and segs:
            _end_dot(cr, segs[-1][-1][0], segs[-1][-1][1], CPU_RGBA)
        cr.restore()
        if latest is not None:
            _text(cr, format_percent(latest), plot.x1, plot.y0 - 4.0,
                  _ink(area, 0.95), _VALUE_FONT_PX, align=1.0, bold=True)
        return False

    def _on_mem_draw(self, area: Gtk.DrawingArea, cr) -> bool:
        spec = self._spec
        if spec is None:
            return False
        plot = _Plot(area.get_allocated_width(), area.get_allocated_height())
        if not plot.ok():
            return False
        # labelled in GiB from 1 GiB up, in MiB below: "0.25 GiB" reads worse
        # than "256 MiB"
        unit, suffix = ((_GIB_KIB, " GiB") if spec.mem_hi >= _GIB_KIB
                        else (_MIB_KIB, " MiB"))
        top_u, ticks_u = axis_ticks(spec.mem_hi / unit)
        top = top_u * unit
        _grid(cr, area, plot, top, [v * unit for v in ticks_u],
              lambda v: format_tick(v / unit) + suffix)
        _time_axis(cr, area, plot)
        # the configured max and the live allocation as reference lines: "squeezed
        # by qmemman" is the used step hugging the allocation line. A level of
        # <= 0 is unset and not drawn (an honest absence, not a fake 0).
        legend = [(MEM_RGBA, "used")]
        cr.set_line_width(1.0)
        for level, rgba, name in ((spec.assigned_kib, ALLOC_RGBA,
                                   "allocated"),
                                  (spec.max_kib, MAX_RGBA, "max")):
            if level <= 0:
                continue
            y = round(plot.y_of(level, top)) + 0.5
            cr.set_source_rgba(*rgba)
            cr.move_to(plot.x0, y)
            cr.line_to(plot.x1, y)
            cr.stroke()
            legend.append((rgba, name))
        _legend(cr, area, plot, legend)
        verts = window_steps(spec.mem_points, time.monotonic(),
                             plot.w, plot.h, 0.0, top, 0.0)
        if not verts:
            _text(cr, "no reported memory data", plot.x0 + plot.w / 2.0,
                  plot.y0 + plot.h / 2.0, _ink(area, 0.6), _AXIS_FONT_PX + 1,
                  align=0.5)
            return False
        cr.save()
        cr.translate(plot.x0, plot.y0)
        trace(cr, verts, plot.h, MEM_RGBA)
        _end_dot(cr, verts[-1][0], verts[-1][1], MEM_RGBA)
        cr.restore()
        _text(cr, format_bytes(spec.mem_points[-1][1] * 1024), plot.x1,
              plot.y0 - 4.0, _ink(area, 0.95), _VALUE_FONT_PX, align=1.0,
              bold=True)
        return False
