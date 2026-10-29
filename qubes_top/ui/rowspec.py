"""Row data for the left pane (card t_4d9516cd). Pure, no ``gi``.

A ``RowSpec`` is the *data* for one left-pane row: the display strings (built
by the ``qubes_top.core`` formatters — never recomputed here), the sparkline
series values + per-qube bounds (for the CPU polyline and the memory step
path), and the label colour. The GTK widget layer maps the values + bounds to
pixels with the ``qubes_top.core`` geometry functions and does *no* metric
arithmetic of its own, which is what keeps this module testable off-box
against a fake feed.

The ``mem_state`` is the *effective* display state the frame carries
(``UNKNOWN`` for every qube while the xenstore channel is down; otherwise the
frozen per-qube machine) — it is never re-derived from the raw observation
here (D-4: never synthesise 100%).
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from qubes_top.core import (MemState, SampleRecord, format_bytes, format_gib,
                          format_percent, own_bounds)

__all__ = ["RowData", "RowSpec", "make_row_spec", "dom0_rowdata"]

#: The em-dash placeholder for a value that has no reading: the first CPU
#: sample, a gap, or a memory reading the guest's meminfo-writer did not report
#: (MUTE). The card forbids rendering a reset as a negative or a clamped zero,
#: so an unknown CPU reading shows "—", not a number; the memory cell uses the
#: same dash so the numeric column stays narrow (the prose lives in the detail
#: pane).
NO_VALUE = "—"


@dataclass(frozen=True)
class RowData:
    """One row's raw inputs, pre-extracted by the caller (the app) from the
    ``SampleModel`` rings — a GIL-benign cross-thread read. Keeping the series
    *out* of the model means the model is a pure function of this data and is
    trivially testable against a fake feed with no collector at all."""
    name: str
    record: SampleRecord
    mem_state: MemState
    cpu_values: Tuple[Optional[float], ...]   # dense CPU %; None = gap
    mem_points: Tuple[Tuple[float, int], ...]  # (ts, used_kib), step series


@dataclass
class RowSpec:
    """The *mutable* per-row state the model keeps in place (updated field by
    field on each refresh — the row is never discarded and recreated, so the
    widget can update the same row widget in place and preserve selection)."""
    name: str
    state: str = ""
    color: str = ""
    cpu_text: str = NO_VALUE
    mem_text: str = ""
    cpu_values: List[Optional[float]] = field(default_factory=list)
    mem_points: List[Tuple[float, int]] = field(default_factory=list)
    cpu_bounds: Tuple[float, float] = (0.0, 1.0)
    mem_bounds: Tuple[float, float] = (0.0, 1.0)
    #: The **upper bound each sparkline is drawn against**, as text for the
    #: label drawn in the middle of that graph (D-21). Empty when the series
    #: has nothing to draw (no CPU value yet, no memory point yet) — a label
    #: over an empty graph would describe an interval that is not there.
    #: For CPU this *is* the series maximum (``own_bounds``), so "10.0%" means
    #: the graph spans 0–10%; for memory it is the ceiling the graph is scaled
    #: to, which is the same denominator the memory column divides by.
    cpu_top_text: str = ""
    mem_top_text: str = ""

    def latest_cpu(self) -> Optional[float]:
        """The most recent CPU value (the last non-None), or None (the first
        sample / a trailing gap). Used for the row's CPU text and the sort
        key; the sort tiebreak on name is stable, so this must be the *same*
        value the text shows (a row sorts by what it displays)."""
        for v in reversed(self.cpu_values):
            if v is not None:
                return v
        return None

    def refresh_from(self, data: RowData, color_for: Callable[[str], str]) -> None:
        """Recompute every mutable field from ``data`` in place. The metric
        arithmetic is the core's (``format_percent`` / ``format_bytes`` /
        ``own_bounds``); this only assembles."""
        self.state = data.record.state
        self.color = color_for(data.name)
        # The displayed CPU is the latest *non-None* value, so the text and
        # the sort key agree even when the very last point is a gap (the
        # first sample or a counter-reset). Never a negative, never a
        # clamped zero — a gap shows "—".
        latest = None
        for v in reversed(data.cpu_values):
            if v is not None:
                latest = v
                break
        self.cpu_text = format_percent(latest) if latest is not None else NO_VALUE
        # The qube's *configured* memory is the meaningful denominator: the
        # hypervisor's live allocation (``mem_assigned_kib``) is what qmemman
        # happens to give it this second, which for an idle qube is far below
        # what it is configured for.
        ceiling = data.record.mem_max_kib or data.record.mem_assigned_kib
        self.mem_text = _mem_text(data.record.mem_used_kib, ceiling,
                                  data.mem_state)
        self.cpu_values = list(data.cpu_values)
        self.mem_points = list(data.mem_points)
        self.cpu_bounds = own_bounds(self.cpu_values)
        # The graph states its own upper bound (D-21) — the owner's ask:
        # "if it says 10% it would mean the graph currently has 0-10 as the
        # interval". That is the bound the geometry uses, so the label cannot
        # describe an interval the graph is not drawn against; for CPU it is
        # the series maximum, which is what makes the per-row auto-scaling
        # readable instead of invisible.
        self.cpu_top_text = (format_percent(self.cpu_bounds[1])
                             if any(v is not None for v in self.cpu_values)
                             else "")
        # The memory sparkline is scaled to the same ceiling — the configured
        # max, else the live allocation, else the observed max — not to the
        # series' own maximum. The memory series carries only observed
        # changes, so it has no fixed range: auto-scaling it re-scales the
        # whole line whenever a new maximum appears, and a qube that has not
        # moved (one point) would draw its flat line at the very top of the
        # graph — which reads as "full". This is the ceiling the detail chart
        # uses too.
        bounds_hi = float(ceiling)
        if bounds_hi <= 0.0:
            bounds_hi = own_bounds(v for _, v in self.mem_points)[1]
        self.mem_bounds = (0.0, bounds_hi)
        # The memory graph's label is the *ceiling it is scaled to* (the
        # configured max, else the live allocation), not the series maximum:
        # the two differ deliberately (see above), and labelling the observed
        # maximum over a graph drawn against the ceiling would describe an
        # interval that is not on screen. Same denominator as the memory cell.
        self.mem_top_text = (format_bytes(self.mem_bounds[1] * 1024)
                             if self.mem_points else "")


def _mem_text(used_kib: Optional[int], max_kib: Optional[int],
             state: MemState) -> str:
    """The row's "used / configured" string, in GiB, with one unit for the
    pair ("1.1 / 3.9 GiB").

    The denominator is the qube's **configured memory** (``mem_max_kib`` —
    qubesd's ``mem_static_max``, i.e. ``maxmem * 1024``), never the memory
    the hypervisor is giving it right now. qmemman balloons an idle qube well
    below its configured size (a 4 GiB qube sitting at 1 GiB is normal), and
    the guest's own "used" includes swap — so "used / live allocation" can
    read as *more than full* and looks like a units bug. It is not.

    GiB always — a per-value unit (``format_bytes``) makes the two halves
    incomparable and costs width in a column that has none to spare. MUTE
    renders the shared dash and UNKNOWN a question mark; the *prose*
    ("not reported — the qube's meminfo-writer is absent or dead") belongs in
    the detail pane (``rightspec.mem_state_text``), not in a numeric cell —
    "not reported / 3.9 GiB" is 23 characters and would widen the column for
    every row, because the rows share one width. Never a synthesised 100%
    (D-4).
    """
    if max_kib is None or max_kib <= 0:
        limit = "?"
    else:
        limit = format_gib(max_kib)
    if state is MemState.REPORTED and used_kib is not None:
        used = format_gib(used_kib)
    elif state is MemState.MUTE:
        used = NO_VALUE
    elif state is MemState.UNKNOWN:
        used = "?"
    else:  # pragma: no cover - MemState is a closed set
        used = "?"
    return "%s / %s GiB" % (used, limit)


def make_row_spec(data: RowData, color_for: Callable[[str], str]) -> RowSpec:
    """Build a fresh ``RowSpec`` from ``data`` (the first time a name appears)."""
    spec = RowSpec(name=data.name)
    spec.refresh_from(data, color_for)
    return spec


def dom0_rowdata(record: SampleRecord,
                 cpu_values: List[Optional[float]],
                 mem_points: List[Tuple[float, int]]) -> RowData:
    """The dom0 row's ``RowData``. dom0 is always ``REPORTED`` (its values come
    straight from the local ``/proc`` read, not a guest meminfo-writer) and is
    rendered like any other row — the same ``RowSpec`` / sort / sparkline
    machinery, no special-casing in the row layer."""
    return RowData(name="dom0", record=record, mem_state=MemState.REPORTED,
                  cpu_values=tuple(cpu_values),
                  mem_points=tuple(mem_points))
