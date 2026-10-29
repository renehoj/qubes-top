"""Right-pane detail data (card t_b14eabf7). Pure, no ``gi``.

A ``DetailSpec`` is the *data* for the right pane for the selected qube:
the display strings (built by the ``qubes_top.core`` formatters — never
recomputed here), the chart series values + display bounds (the
``qubes_top.core`` geometry functions map them to pixels), and the
process-table display (sort + text).

The ``mem_state`` is the *effective* display state the frame carries
(``UNKNOWN`` for every qube while the xenstore channel is down; otherwise
the frozen per-qube machine) — it is never re-derived from the raw
observation here (D-4: never synthesise 100%).

The spec is *in place*: one object, updated field by field on each refresh
(the row is never discarded and recreated), mirroring the left pane's
``RowSpec`` contract. The history window itself lives in the
``SampleModel`` rings, which carry no selection state — a selection change
re-fetches the same retained series, so the window, the process sort, and
the scroll position survive it by construction (the card's requirement).

Only *display bounds* are computed here (the CPU 100% floor, the memory
max ceiling, falling back to the live allocation). The *metric* arithmetic
(the CPU delta, the deadband steps, the byte/percent formatting) is the
core's — the card's "do not compute percentages here; take them from
``qubes_top/core``".
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from qubes_top.core import MemState, SampleRecord, format_bytes, format_percent
from qubes_top.ui.processes import ProcessInfo
from qubes_top.ui.qubemeta import QubeMeta

__all__ = [
    "NO_VALUE", "NA", "STRING_CAP", "cap",
    "mem_state_text", "used_text", "swap_text", "domid_text",
    "DetailSpec", "ProcRow", "proc_display",
]

#: The em-dash placeholder for a value with no baseline yet (first reading)
#: or a gap; a missing CPU reading shows "—", never a number.
NO_VALUE = "—"

#: The placeholder for a value that is unknown (no metadata, no swap, an
#: unresolved domid, a max that is not set).
NA = "n/a"

#: The hard cap for every externally supplied string (guest names, process
#: comm, template/netvm names): untrusted input, bounded before it reaches
#: a label (the widget may additionally ellipsize visually).
STRING_CAP = 64


def cap(value) -> str:
    """Cap an externally supplied string at ``STRING_CAP`` characters and
    map a missing value to ``NA``. The cap is a hard cut (no ellipsis —
    deterministic, and the label may ellipsize visually on top of it)."""
    if value is None:
        return NA
    v = str(value)
    if len(v) <= STRING_CAP:
        return v
    return v[:STRING_CAP]


def mem_state_text(state: MemState) -> str:
    """The *worded* memory report state (the card's "say it in words,
    never as 100%"): ``MUTE`` names the cause — the qube's meminfo-writer
    is absent or dead — instead of a number; ``UNKNOWN`` says no usable
    observation exists (the channel is down, or the fallback signature
    cannot be evaluated)."""
    if state is MemState.REPORTED:
        return "reported"
    if state is MemState.MUTE:
        return ("not reported — the qube's meminfo-writer is absent or "
                "dead; the value is withheld, never shown as 100%")
    return ("unknown — no usable observation (channel down, or the "
            "fallback signature cannot be evaluated)")


def used_text(used_kib: Optional[int], state: MemState) -> str:
    """The facts-block "used" value. Only a ``REPORTED`` value is a number
    (the core formatter); ``MUTE`` says "not reported" and ``UNKNOWN`` says
    "unknown" — never a synthesised 100% (D-4)."""
    if state is MemState.REPORTED and used_kib is not None:
        return format_bytes(used_kib * 1024)
    if state is MemState.MUTE:
        return "not reported"
    if state is MemState.UNKNOWN:
        return "unknown"
    return "?"  # pragma: no cover - MemState is a closed set


def swap_text(swap_kib: Optional[int]) -> str:
    """The facts-block "swap" value (Q-3: the detail pane breaks swap out
    — decided). ``None`` (swapinfo absent) is ``NA``, never a silent zero
    (D-4: no silent zero)."""
    if swap_kib is None:
        return NA
    return format_bytes(swap_kib * 1024)


def domid_text(domid: int) -> str:
    """The domid, in words. The sentinel ``-1`` (the name is not in this
    tick's xenstore name map — the dom0 sampler's ``UNRESOLVED_DOMID``) is
    displayed as unresolved, never as a literal ``-1``."""
    if domid < 0:
        return "n/a (unresolved)"
    return str(domid)


@dataclass
class DetailSpec:
    """The *mutable* per-selection state the pane keeps in place (updated
    field by field on each refresh — the pane is never discarded and
    recreated, so the chart widgets and the process table can update the
    same widgets and preserve the scroll position and the sort)."""
    name: str = ""
    state: str = ""
    color: str = ""
    # header (display strings; ``NA`` when unknown)
    template: str = NA
    netvm: str = NA
    vcpus: str = NA
    mem_max_text: str = NA
    # facts block
    allocated_text: str = NA  # what the hypervisor gives the qube *now*
                              # (qubesd's `mem`, the live balloon) — not its
                              # configured memory, which is `mem_max_text`
    used_text: str = NA
    swap_text: str = NA
    report_text: str = ""
    domid_text: str = NA
    # charts: values + display bounds (the pixel mapping is the core's
    # geometry; ``lo`` is always 0.0)
    cpu_values: List[Optional[float]] = field(default_factory=list)
    cpu_hi: float = 100.0   # always >= 100 so the 100% reference line is
                           # on the chart even for an idle qube (values
                           # may exceed it — D-3)
    mem_points: List[Tuple[float, int]] = field(default_factory=list)
    mem_hi: float = 1.0     # the memory ceiling: the max (the red line);
                           # the assigned when the max is unset; 1.0 when
                           # neither is (degenerate; normalize maps it to
                           # the bottom, never a divide-by-zero)
    assigned_kib: int = 0   # the memory chart's reference lines (<= 0: absent,
    max_kib: int = 0       # the line is not drawn)

    def refresh(self,
                name: str,
                record: SampleRecord,
                mem_state: MemState,
                cpu_values: Sequence[Optional[float]],
                mem_points: Sequence[Tuple[float, int]],
                meta: Optional[QubeMeta],
                color_for: Callable[[str], str]) -> None:
        """Recompute every mutable field from this tick's inputs, in place.
        The metric text is the core's (``format_bytes`` / ``format_percent``
        via the helpers above); this only assembles and sets the display
        bounds."""
        self.name = cap(name)
        self.state = record.state
        self.color = color_for(name)
        # header (the metadata comes from the provider seam; ``None`` fields
        # are ``NA`` — a missing template is not a qube fault)
        self.template = cap(meta.template if meta is not None else None)
        self.netvm = cap(meta.netvm if meta is not None else None)
        self.vcpus = (str(meta.vcpus) if meta is not None
                      and meta.vcpus is not None else NA)
        self.mem_max_text = (format_bytes(record.mem_max_kib * 1024)
                            if record.mem_max_kib > 0 else NA)
        # facts
        self.allocated_text = (format_bytes(record.mem_assigned_kib * 1024)
                               if record.mem_assigned_kib > 0 else NA)
        self.used_text = used_text(record.mem_used_kib, mem_state)
        self.swap_text = swap_text(record.swap_used_kib)
        self.report_text = mem_state_text(mem_state)
        self.domid_text = domid_text(record.domid)
        # charts
        self.cpu_values = list(cpu_values)
        vals = [v for v in cpu_values if v is not None]
        self.cpu_hi = max(100.0, max(vals)) if vals else 100.0
        self.mem_points = list(mem_points)
        hi = record.mem_max_kib
        if hi <= 0:
            hi = record.mem_assigned_kib
        if hi <= 0:
            hi = 1
        self.mem_hi = float(hi)
        self.assigned_kib = record.mem_assigned_kib
        self.max_kib = record.mem_max_kib


@dataclass(frozen=True)
class ProcRow:
    """One *display* process row: the raw ``ProcessInfo`` values reduced to
    label text by the core formatters (the pane only sets these strings)."""
    pid: int
    name: str        # capped
    cpu_text: str    # "12.3%" or NO_VALUE (a first reading / a gap)
    rss_text: str    # "1.5 MiB" (the core formatter)


#: A large sentinel for sorting: a row with no CPU value (a first reading —
#: no baseline yet) sorts *last* in both directions, never first.
_NO_CPU = float("inf")


def proc_display(rows: Sequence[ProcessInfo], sort_key: str,
                 direction: int) -> List[ProcRow]:
    """The process-table rows with the pane's sort applied (top-like:
    ``%CPU`` descending by default; a missing CPU value sorts last in both
    directions; the tiebreak is a *stable* ascending pid). ``sort_key`` is
    one of ``"cpu"`` | ``"pid"`` | ``"rss"``; an unknown key sorts by CPU
    (the default — a mistyped key must not crash the pane)."""
    key = sort_key if sort_key in ("cpu", "pid", "rss") else "cpu"

    def k(r: ProcessInfo):
        if key == "pid":
            return (direction * r.pid, r.pid)
        v = (r.rss_kib if key == "rss" else r.cpu_pct)
        if v is None:
            # a missing value (a first reading for CPU, a page-less read
            # for rss) sorts *last* in both directions, never first
            v = -_NO_CPU if direction == -1 else _NO_CPU
        return (direction * v, r.pid)

    out = sorted(rows, key=k)
    return [ProcRow(
        pid=r.pid,
        name=cap(r.name),
        cpu_text=format_percent(r.cpu_pct) if r.cpu_pct is not None
        else NO_VALUE,
        rss_text=(format_bytes(r.rss_kib * 1024)
                  if r.rss_kib is not None else NO_VALUE),
    ) for r in out]
