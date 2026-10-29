"""Table layouts (pure, no ``gi``): the qube list and the process table.

Each table is described **once**, and both the row widget and the column
header build from that description, so a column cannot be added to one and not
the other, and a width cannot drift between the header and the data under it.

``COLUMNS`` is the qube list — six columns: name, status, CPU %, the CPU
history sparkline, memory, the memory history sparkline. ``PROC_COLUMNS`` is
the process table: PID, name, %CPU, RSS.

``width`` is a **minimum**, in pixels: each fixed column's cells join a
``Gtk.SizeGroup`` (see ``left_pane.py`` / ``right_pane.py``), so a value wider
than its minimum widens that column in *every* row and in the header rather
than shifting everything to its right. The graph columns of the qube list
carry the slack instead (``expand``), which is why their minimums are small: at
the default divider position (``PanedConfig.DEFAULT_POSITION``) the six columns
must fit without a horizontal scrollbar, and the graphs are the only columns
that can shrink without hiding data — the numeric columns carry the values.
"""
from dataclasses import dataclass
from typing import Dict, Tuple

__all__ = ["Column", "COLUMNS", "PROC_COLUMNS", "SPACING", "DOT_SIZE",
           "DOT_GAP", "GRAPH_HEIGHT", "ARROW_WIDTH", "column",
           "fixed_columns", "WINDOW_MARGIN", "RESIZE_GRIP", "edge_at",
           "WINDOW_WIDTH", "WINDOW_HEIGHT", "ROW_SPACING", "LIST_MIN_ROWS",
           "list_min_height"]

#: The gap between two columns, in pixels. The header and the rows must use
#: the same value: every gap is another few pixels of drift otherwise.
SPACING = 6

#: The width reserved for a sort arrow in a column title, in pixels. Reserved
#: always (empty when the column is not the active sort), so activating a sort
#: cannot change any column's width.
ARROW_WIDTH = 12

#: The label-colour dot's diameter, and the gap between it and the name text.
#: The header indents its "Name" title by both, so the title sits over the
#: names and not over the dots.
DOT_SIZE = 14
DOT_GAP = 6

#: Every row's sparkline height, in pixels. Constant, so the row height is
#: constant — a stable row height is what makes the scroll-position restore
#: meaningful.
GRAPH_HEIGHT = 28


@dataclass(frozen=True)
class Column:
    """One table column.

    ``xalign`` must match between the header and the cells (a right-aligned
    number column needs a right-aligned title). ``sort_key`` is the
    ``LeftPaneModel`` sort key a header click requests; empty means the
    column is not sortable. ``max_chars`` caps a label's *natural* width so
    one absurd value (a 40-character qube name) cannot widen the column into
    the graphs; 0 means uncapped.
    """
    key: str
    title: str
    width: int
    xalign: float
    expand: bool = False
    sort_key: str = ""
    max_chars: int = 0


#: The columns, left to right. The order is the rendering order.
COLUMNS: Tuple[Column, ...] = (
    Column("name", "Name", 112, 0.0, sort_key="name", max_chars=12),
    Column("status", "Status", 56, 0.0),
    Column("cpu", "CPU", 48, 1.0, sort_key="cpu"),
    Column("cpu_graph", "CPU graph", 76, 0.0, expand=True),
    Column("mem", "Memory", 112, 1.0),
    Column("mem_graph", "Mem graph", 76, 0.0, expand=True),
)

#: The process table's columns. The numeric ones are right-aligned (a column
#: of numbers is read by its right edge) and monospaced by the pane; the name
#: is capped so one long command name cannot widen the table.
PROC_COLUMNS: Tuple[Column, ...] = (
    Column("pid", "PID", 64, 1.0, sort_key="pid"),
    Column("name", "NAME", 160, 0.0, max_chars=22),
    Column("cpu", "%CPU", 72, 1.0, sort_key="cpu"),
    Column("rss", "RSS", 90, 1.0, sort_key="rss"),
)

_BY_KEY: Dict[str, Column] = {c.key: c for c in COLUMNS}


def column(key: str) -> Column:
    """The column with this key. A typo raises ``KeyError`` at startup rather
    than silently rendering nothing."""
    return _BY_KEY[key]


def fixed_columns() -> Tuple[Column, ...]:
    """The non-expanding columns: the ones whose width is shared across every
    row by a ``Gtk.SizeGroup``."""
    return tuple(c for c in COLUMNS if not c.expand)


#: The undecorated window's margin around its two panes (D-23): the outer
#: background shows through it, and it is where the window is dragged.
WINDOW_MARGIN = 6

#: How close to the window's edge a press resizes instead of moving, in pixels.
#: Wider than the margin is not possible (the panes take the press there), so
#: the grip is the margin itself; a corner is the grip on two sides at once.
RESIZE_GRIP = WINDOW_MARGIN


def edge_at(x: float, y: float, width: int, height: int,
            grip: int = RESIZE_GRIP):
    """Which edge of a ``width`` x ``height`` window ``(x, y)`` is on: one of
    ``"n" "s" "e" "w" "ne" "nw" "se" "sw"``, or None for the inside (a move).

    The undecorated window has no frame for the window manager to resize by,
    so the press handler asks this and starts a resize drag on that edge.
    """
    ns = "n" if y < grip else "s" if y >= height - grip else ""
    ew = "w" if x < grip else "e" if x >= width - grip else ""
    return (ns + ew) or None


#: The window's default size. The height is what leaves about six qube rows
#: visible with the info box and both charts open (D-25); at 640 the owner saw
#: two and a half. ``window.py`` lowers it to fit a smaller screen.
WINDOW_WIDTH = 1100
WINDOW_HEIGHT = 760

#: The gap between two qube rows, in pixels (the row box's spacing).
ROW_SPACING = 2

#: The qube list never gets shorter than this many rows (D-25): the list is
#: what you pick from, and the info box and charts that open on a selection
#: must not squeeze it to a couple of rows. GTK grows the window instead.
LIST_MIN_ROWS = 5

#: The list's vertical padding (``.rows`` in qubes-top.css: 3 px top and bottom).
_ROWS_PADDING = 6


def list_min_height(rows: float = LIST_MIN_ROWS) -> int:
    """The scroller height that shows ``rows`` qube rows. A row is as tall as
    its sparklines (the labels are shorter)."""
    return int(round(rows * (GRAPH_HEIGHT + ROW_SPACING))) + _ROWS_PADDING
