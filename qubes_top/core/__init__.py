"""qubes-top core: sample/series model, memory report state machine, formatters.

Pure standard library. No ``gi``, no ``qubesadmin``, no ``xdg``, no network:
this module must stay importable on a dev box with none of that present.
The import contract is enforced by ``tests/test_import_guard.py`` (AST, not
text).

The public API below is **frozen**: the dom0 adapters (``qubes_top.dom0``) and
the UI (``qubes_top.ui``) code against it. DESIGN.md §5 records the frozen
interface boundaries; this docstring records the semantics those boundaries
leave open, so the adapter and UI lanes can code against this file alone.

Frozen API
----------
``SampleRecord``  -- one sample per qube, fields exactly (in order):
    ``name, domid, state, cpu_time_ns, mem_assigned_kib, mem_max_kib,
    mem_used_kib|None, swap_used_kib|None, ts``
``MemState``      -- ``REPORTED`` / ``MUTE`` / ``UNKNOWN``
``mem_report_state(used_kib, assigned_kib)``  -- the state machine, pure
``cpu_delta_pct(prev_cpu_ns, cur_cpu_ns, prev_ts, cur_ts)``  -- the delta
``CpuPoint(ts, pct|None)`` / ``MemPoint(ts, used_kib, swap_kib|None)``
``SampleModel``   -- per-qube ring buffers + state machine:
    ``ingest(record)``              -- once per qube per tick (2 s cadence)
    ``synchronize(present_names)``  -- once per tick; drops the buffers of
                                      every qube no longer in the running set
    ``cpu_series(name)`` / ``mem_series(name)``  -- oldest -> newest
    ``mem_state(name)``   -- state of the latest ingested sample, or None
    ``has(name)``
``format_bytes(n)``   -- "1.5 MiB" / "1.0 GiB" (one decimal; GiB at >= 1 GiB)
``format_percent(p)`` -- "42.5%" (one decimal)
``DEADBAND_KIB``     -- 30 * 1024 (30 MiB, frozen by DESIGN.md D-3 / §2)

Frozen semantics (recorded here; DESIGN.md is the architect's document)
---------------------------------------------------------------------
- ``ts`` is a **monotonic seconds float** supplied by the adapter (one clock,
  e.g. ``time.monotonic()``); the CPU delta uses ``dt_ns = (ts2 - ts1) * 1e9``.
  (Decision D-12, new: the frozen field list did not fix the ``ts`` unit.)
- CPU delta is ``(cpu2 - cpu1) / dt_ns * 100`` -> % of ONE core, may exceed
  100. A **gap** (a ``None`` point value) is emitted when ``dt <= 0`` (a
  ``dt == 0`` tick, or a clock regression) or when the counter **reset**
  (``cpu2 < cpu1``; qube restart). A reset is never rendered negative and is
  never clamped to zero. An idle counter (``cpu2 == cpu1``, ``dt > 0``) is a
  legitimate ``0.0`` -- that is not a gap. After a reset the restarted value
  becomes the new baseline.
- CPU points are **dense**: one per ingested sample; the first sample for a
  qube has value ``None`` (no baseline).
- Memory points are a **step** series: a point is emitted only when a
  ``REPORTED`` observation differs from the last *confirmed* point's value by
  at least the deadband (30 MiB). Two identical reads -> one point. The point
  carries the timestamp of the sample where the change was **observed** (the
  change time itself is unknowable from a 2 s cadence).
- ``MUTE``/``UNKNOWN`` samples emit **no** memory point and do not move the
  deadband reference: the qubesd fallback value (``used == assigned``) is not
  an observation, and consuming it would synthesise 100% into the series.
  (D-4: never synthesise 100%.)
- The state machine is total: ``MUTE`` when meminfo is absent (``used is
  None``) or ``used == assigned`` to the KiB (writer missing/dead);
  ``UNKNOWN`` when a ``used`` value is present but ``assigned`` is unknown
  (the fallback signature cannot be evaluated, so the value is neither
  asserted nor muted); otherwise ``REPORTED``. A genuinely-full qube is
  indistinguishable from the fallback and renders "not reported" (``MUTE``) --
  the documented cost of D-4.
- Two fixed-capacity rings per qube (cpu, mem), both defaulting to 150
  points (150 x 2 s = the 5-minute window, frozen D-10); appended **in place**
  (pre-allocated buffer, the oldest slot is overwritten at full) and dropped
  when the qube disappears (``synchronize``). The model carries **no
  selection state** -- the UI queries by name -- so buffers are retained
  across selection changes by construction. The mem ring holds 150 observed
  *changes*, which at 30 MiB quantisation can span longer than 5 minutes.
"""
import math
import re
import zlib
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Generic, Iterable, List, Optional, Sequence, Tuple, TypeVar, cast

__all__ = [
    "DEADBAND_KIB",
    "SampleRecord",
    "MemState",
    "mem_report_state",
    "cpu_delta_pct",
    "CpuPoint",
    "MemPoint",
    "SampleModel",
    "format_bytes",
    "format_gib",
    "format_percent",
    "normalize",
    "own_bounds",
    "HISTORY_WINDOW_S",
    "TICK_S",
    "window_x",
    "window_segments",
    "window_steps",
    "axis_ticks",
    "format_tick",
    "label_color",
    "parse_color",
    "NORD_LABEL_COLORS",
    "dot_color_for_label",
]

#: 30 MiB deadband, in KiB. Frozen by DESIGN.md D-3 / §2 (the meminfo-writer
#: quantises in 30 MiB; sub-30 MiB movement is not an observation).
DEADBAND_KIB = 30 * 1024


@dataclass(frozen=True)
class SampleRecord:
    """One sample for one qube. Fields are frozen, in this order.

    The two memory figures are **different things** (D-14), and on Qubes 4.3
    neither can come from ``admin.vm.CurrentState``:

    * ``mem_assigned_kib`` — what the hypervisor gives the domain *now*
      (xenstore ``memory/target``, qmemman's live balloon). qmemman keeps an
      idle qube well below its configured size.
    * ``mem_max_kib`` — the qube's **configured** maximum (xenstore
      ``memory/hotplug-max``, else ``memory/static-max``; ``maxmem * 1024``).
      This is what Qubes' Settings shows, and the denominator the UI uses for
      "used / configured".

    Both fall back to the API's ``mem`` / ``mem_static_max`` when xenstore has
    no such key — but on 4.3 those two API values are *the same number*
    (libvirt's memory total, i.e. the initial size for a memory-hotplug
    qube), so they are a fallback, never the source.
    """
    name: str
    domid: int
    state: str
    cpu_time_ns: int
    mem_assigned_kib: int
    mem_max_kib: int
    mem_used_kib: Optional[int]
    swap_used_kib: Optional[int]
    ts: float


class MemState(Enum):
    """Memory report state machine (DESIGN.md D-4)."""
    REPORTED = "reported"
    MUTE = "mute"
    UNKNOWN = "unknown"


def mem_report_state(used_kib: Optional[int],
                    assigned_kib: Optional[int]) -> MemState:
    """The memory report state machine, as a pure function over raw
    observations.

    - ``used_kib is None`` (meminfo absent)            -> MUTE
    - ``assigned_kib is None`` (used present but the
      fallback signature cannot be evaluated)          -> UNKNOWN
    - ``used_kib == assigned_kib`` to the KiB (writer
      missing/dead; qubesd's fallback)                -> MUTE
    - otherwise                                       -> REPORTED

    Never synthesises 100%: a qube at exactly its full allocation is
    indistinguishable from the fallback and is muted (D-4).

    ``assigned_kib`` is the hypervisor's **live** allocation (xenstore
    ``memory/target``) — that is the value the fallback signature compares
    against — not the qube's configured maximum (D-14).
    """
    if used_kib is None:
        return MemState.MUTE
    if assigned_kib is None:
        return MemState.UNKNOWN
    if used_kib == assigned_kib:
        return MemState.MUTE
    return MemState.REPORTED


def cpu_delta_pct(prev_cpu_ns: int, cur_cpu_ns: int,
                  prev_ts: float, cur_ts: float) -> Optional[float]:
    """CPU delta as % of ONE core: ``(cur - prev) / dt_ns * 100``.

    Gaps (returns ``None``):
    - ``dt <= 0`` -- a ``dt == 0`` tick or a clock regression;
    - ``cur < prev`` -- the ``cpu_time`` counter **reset** (qube restart).
      A reset is never rendered negative and never clamped to zero.

    Not a gap: ``cur == prev`` with ``dt > 0`` is a legitimate ``0.0`` (idle).
    The result may exceed 100 (the domain's utilisation of all its cores).
    """
    dt = cur_ts - prev_ts
    if dt <= 0.0:
        return None
    delta_ns = cur_cpu_ns - prev_cpu_ns
    if delta_ns < 0:
        return None
    return delta_ns / (dt * 1e9) * 100.0


@dataclass(frozen=True)
class CpuPoint:
    """One dense CPU sample; ``pct`` is None on the first sample and on a
    counter-reset gap."""
    ts: float
    pct: Optional[float]


@dataclass(frozen=True)
class MemPoint:
    """One memory step: the value and the timestamp of the sample where the
    change was observed. ``swap_kib`` is None when swapinfo was absent."""
    ts: float
    used_kib: int
    swap_kib: Optional[int]


_T = TypeVar("_T")


class _Ring(Generic[_T]):
    """Fixed-capacity ring: pre-allocated buffer, appended in place, the
    oldest slot overwritten at full. ``items()`` is oldest -> newest."""
    __slots__ = ("_buf", "_cap", "_next", "_count")

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("ring capacity must be >= 1")
        self._buf: List[Optional[_T]] = [None] * capacity
        self._cap = capacity
        self._next = 0
        self._count = 0

    def push(self, item: _T) -> None:
        self._buf[self._next] = item
        self._next = (self._next + 1) % self._cap
        if self._count < self._cap:
            self._count += 1

    def items(self) -> List[_T]:
        # Invariant: after the first ``cap`` pushes every slot is filled, so
        # the slices contain no None; the cast states that, the checker
        # cannot derive it.
        if self._count < self._cap:
            return cast("List[_T]", list(self._buf[:self._count]))
        return cast("List[_T]", self._buf[self._next:] + self._buf[:self._next])

    def __len__(self) -> int:
        return self._count


class _Qube:
    """Per-qube state: two rings, the cpu baseline, the deadband reference,
    and the latest report state."""
    __slots__ = ("cpu", "mem", "prev_cpu_ns", "prev_ts",
                 "mem_ref_kib", "last_state")

    def __init__(self, capacity: int) -> None:
        self.cpu = _Ring[CpuPoint](capacity)
        self.mem = _Ring[MemPoint](capacity)
        self.prev_cpu_ns: Optional[int] = None
        self.prev_ts: Optional[float] = None
        self.mem_ref_kib: Optional[int] = None
        self.last_state: Optional[MemState] = None


class SampleModel:
    """Per-qube sample model: ring buffers + state machine.

    Per tick (2 s cadence, frozen D-10): call ``ingest`` for every running
    qube, then ``synchronize`` with the set of names still present. The
    model carries no selection state, so buffers are retained across UI
    selection changes by construction.
    """

    def __init__(self, ring_capacity: int = 150,
                 deadband_kib: int = DEADBAND_KIB) -> None:
        if ring_capacity < 1:
            raise ValueError("ring capacity must be >= 1")
        if deadband_kib < 0:
            raise ValueError("deadband must be >= 0")
        self._capacity = ring_capacity
        self._deadband_kib = deadband_kib
        self._qubes: Dict[str, _Qube] = {}

    def ingest(self, record: SampleRecord) -> None:
        """Fold one sample into the model.

        The CPU ring gets a dense point (None on the first sample and on a
        counter-reset gap). The mem ring gets a step point only when the
        sample is REPORTED and the used value differs from the last confirmed
        point by at least the deadband; a MUTE/UNKNOWN sample neither emits a
        point nor moves the reference (the fallback value is not an
        observation).
        """
        q = self._qubes.get(record.name)
        if q is None:
            q = self._qubes[record.name] = _Qube(self._capacity)

        if q.prev_cpu_ns is None or q.prev_ts is None:
            pct: Optional[float] = None  # first sample: no baseline
        else:
            pct = cpu_delta_pct(q.prev_cpu_ns, record.cpu_time_ns,
                               q.prev_ts, record.ts)
        q.cpu.push(CpuPoint(record.ts, pct))
        # The current reading becomes the next baseline, reset gap or not:
        # after a restart the small restarted counter is the correct origin.
        q.prev_cpu_ns = record.cpu_time_ns
        q.prev_ts = record.ts

        state = mem_report_state(record.mem_used_kib, record.mem_assigned_kib)
        q.last_state = state
        if state is MemState.REPORTED and record.mem_used_kib is not None:
            ref = q.mem_ref_kib
            if ref is None or abs(record.mem_used_kib - ref) >= self._deadband_kib:
                q.mem.push(MemPoint(record.ts, record.mem_used_kib,
                                   record.swap_used_kib))
                q.mem_ref_kib = record.mem_used_kib

    def synchronize(self, present_names: Iterable[str]) -> None:
        """Drop the buffers of every qube no longer in ``present_names``.

        A qube that later reappears starts clean: a fresh buffer means a
        fresh CPU baseline, so a restarted qube (domid reuse is irrelevant
        here -- the buffer is keyed by *name*) cannot attach a stale counter
        baseline to its restarted history.
        """
        present = set(present_names)
        for name in list(self._qubes):
            if name not in present:
                del self._qubes[name]

    def cpu_series(self, name: str) -> List[CpuPoint]:
        q = self._qubes.get(name)
        return q.cpu.items() if q is not None else []

    def mem_series(self, name: str) -> List[MemPoint]:
        q = self._qubes.get(name)
        return q.mem.items() if q is not None else []

    def mem_state(self, name: str) -> Optional[MemState]:
        q = self._qubes.get(name)
        return q.last_state if q is not None else None

    def has(self, name: str) -> bool:
        return name in self._qubes


def format_bytes(n: float) -> str:
    """bytes -> "1.5 MiB" / "1.0 GiB". One decimal; GiB at >= 1 GiB, MiB
    below. Rounding is the IEEE-decimal rounding of f-formatting
    (deterministic for a given value). The KiB record values are converted by
    the caller (``kib * 1024``) -- the frozen formatter contract is bytes in."""
    if n >= 2 ** 30:
        return f"{n / 2 ** 30:.1f} GiB"
    return f"{n / 2 ** 20:.1f} MiB"


def format_gib(kib: float) -> str:
    """KiB -> a GiB *number* string: "1.5", "0.39".

    The left-pane row shows used and configured memory in one cell with a single
    unit for the pair ("0.39 / 1.0 GiB"), so this returns the number only; the
    caller appends " GiB". Two decimals below 1 GiB: one decimal would render
    a 40 MiB qube as "0.0", which is a wrong value, not a compact one.

    Deliberately *not* ``format_bytes``: that picks its unit per value
    ("400.0 MiB" / "1.0 GiB"), so a used/configured pair could mix units and
    the two halves of the cell would not be comparable at a glance.
    """
    gib = kib / 2 ** 20
    if gib >= 1.0:
        return f"{gib:.1f}"
    return f"{gib:.2f}"


def format_percent(p: float) -> str:
    """percent -> one decimal, e.g. "42.5%". May exceed 100 (D-3)."""
    return f"{p:.1f}%"


# ---------------------------------------------------------------------------
# Sparkline geometry + per-qube bounds + a stable label colour (card
# t_4d9516cd). Pure standard library: the UI (``qubes_top.ui``) maps a
# ``RowSpec``'s values + bounds to pixels with these; no metric arithmetic
# (CPU deltas, deadband steps, byte/percent formatting) is re-done in the
# widget code. ``own_bounds`` scales a sparkline to the *qube's own* max, not
# fleet-wide, so a 30 MiB step on a 400 MiB qube stays visible (the card's
# row-layout requirement).
# ---------------------------------------------------------------------------

def normalize(value: float, lo: float, hi: float) -> float:
    """Map ``value`` into [0, 1] with ``lo`` -> 0, ``hi`` -> 1, clamped.

    A degenerate range (``hi <= lo``) maps everything to 0.0 (the bottom) so
    the geometry never divides by zero — a flat series renders as a flat line
    at the bottom, which is the honest drawing for "no variation"."""
    if hi <= lo:
        return 0.0
    f = (value - lo) / (hi - lo)
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def own_bounds(values: Iterable[Optional[float]],
               floor: float = 0.0) -> Tuple[float, float]:
    """The y-range for one sparkline: ``(lo, hi)`` with ``lo = floor`` and
    ``hi = max`` of the non-None values — the *qube's own* max, not a
    fleet-wide one (a 30 MiB step on a 400 MiB qube is 7.5% of the height,
    visible; fleet-wide it would be sub-per-cent). A degenerate range is
    widened to ``lo + 1.0`` so the geometry never divides by zero."""
    vals = [v for v in values if v is not None]
    hi = max(vals) if vals else floor
    lo = floor
    if hi < lo:
        hi = lo
    if hi - lo < 1e-9:
        hi = lo + 1.0
    return (lo, hi)


#: The span every graph shows, in seconds, and the sampling tick (D-10: the
#: 150-point rings at 2 s). The graphs draw against this **fixed** window with
#: "now" at the right edge (D-24): one second is the same number of pixels in
#: every graph, from the first frame on, so the CPU and memory graphs line up
#: and a line does not stretch while the ring is still filling.
HISTORY_WINDOW_S = 300.0
TICK_S = 2.0


def window_x(age_s: float, width: float, pad: float,
             window_s: float = HISTORY_WINDOW_S) -> float:
    """x for a sample ``age_s`` seconds old: now at ``width - pad``,
    ``window_s`` ago at ``pad``; clamped to that span."""
    f = 1.0 - normalize(age_s, 0.0, window_s)
    return pad + f * (width - 2.0 * pad)


def window_segments(values: Sequence[Optional[float]],
                    width: float, height: float,
                    lo: float, hi: float, pad: float = 2.0,
                    tick_s: float = TICK_S,
                    window_s: float = HISTORY_WINDOW_S,
                    ) -> List[List[Tuple[float, float]]]:
    """A CPU series as polyline *segments* on the fixed window: the newest
    value at the right edge, each older one ``tick_s`` further left, values
    older than the window dropped. A ``None`` (the first sample, a
    counter-reset gap) splits the segments, so a reset is never drawn as a
    false line across the gap. y is inverted (``lo`` -> bottom)."""
    n = len(values)
    segs: List[List[Tuple[float, float]]] = []
    cur: List[Tuple[float, float]] = []
    for i, v in enumerate(values):
        age = (n - 1 - i) * tick_s
        if v is None or age > window_s:
            if cur:
                segs.append(cur)
                cur = []
            continue
        y = pad + (1.0 - normalize(v, lo, hi)) * (height - 2.0 * pad)
        cur.append((window_x(age, width, pad, window_s), y))
    if cur:
        segs.append(cur)
    return segs


def window_steps(points: Sequence[Tuple[float, float]], now: float,
                 width: float, height: float,
                 lo: float, hi: float, pad: float = 2.0,
                 window_s: float = HISTORY_WINDOW_S,
                 ) -> List[Tuple[float, float]]:
    """A memory series as a step-after path on the fixed window, ending at
    ``now`` (the right edge). ``points`` are ``(ts, value)`` observed changes
    on the same clock as ``now``.

    Each level holds until the next change, and the last one holds to now. The
    level in force when the window opens enters at the left edge, and so
    does the *first* level when the series began inside the window: memory
    is a level, the series only records its changes (30 MiB deadband, D-3),
    so a qube whose memory has not moved owns one point. Drawing that level
    across the whole width is what keeps a steady qube a flat line rather
    than a dot (the owner saw the dot on dom0). No points is [].
    """
    if not points:
        return []
    t0 = now - window_s

    def y_of(v: float) -> float:
        return pad + (1.0 - normalize(v, lo, hi)) * (height - 2.0 * pad)

    def x_of(t: float) -> float:
        return window_x(now - t, width, pad, window_s)

    # the level in force at the left edge: the last change at or before t0,
    # else the first level (see above)
    level = points[0][1]
    for ts, v in points:
        if ts > t0:
            break
        level = v
    verts: List[Tuple[float, float]] = [(x_of(t0), y_of(level))]
    for ts, v in points:
        if ts <= t0:
            continue
        x = x_of(ts)
        verts.append((x, y_of(level)))  # hold the previous level to the change
        level = v
        verts.append((x, y_of(level)))  # then jump
    verts.append((x_of(now), y_of(level)))  # the last level holds to now
    return verts


def axis_ticks(hi: float, target: int = 4) -> Tuple[float, List[float]]:
    """A readable y-axis for ``0 .. hi``: ``(top, ticks)``.

    The step is the smallest of 1, 2, 2.5 or 5 x 10^k that gives at most
    ``target`` intervals; ``top`` is ``hi`` rounded *up* to a whole step, so the
    highest gridline is labelled and the data never runs past the axis.
    ``ticks`` runs from 0 to ``top`` inclusive. A non-positive ``hi`` is
    treated as 1."""
    if hi <= 0.0:
        hi = 1.0
    raw = hi / max(1, target)
    k = 10.0 ** math.floor(math.log10(raw))
    step = next(m * k for m in (1.0, 2.0, 2.5, 5.0, 10.0) if m * k >= raw * (1 - 1e-9))
    count = int(math.ceil(hi / step - 1e-9))
    top = count * step
    return top, [i * step for i in range(count + 1)]


def format_tick(value: float) -> str:
    """An axis tick: no trailing zeros ("0", "2.5", "100")."""
    return ("%.2f" % value).rstrip("0").rstrip(".")


#: A Qubes label colour as **qubesd actually serialises it**: ``0xRRGGBB``.
#: ``qubes-core-admin@release4.3`` ``qubes/app.py`` defines the palette as
#: ``Label(3, "0xedd400", "yellow")`` — ``red 0xcc0000``, ``orange 0xf57900``,
#: ``yellow 0xedd400``, ``green 0x73d216``, ``gray 0x555555``,
#: ``blue 0x3465a4``, ``purple 0x75507b``, ``black 0x000000`` — and
#: ``admin.label.Get`` returns ``label.color`` verbatim, so the client sees
#: ``0x…``. ``#RRGGBB`` is accepted too, because that is the form a widget
#: needs (``Gdk.RGBA.parse``) and the form the rest of this code passes
#: around. Anything else is not a colour.
_LABEL_COLOR_RE = re.compile(r"\A(?:#|0[xX])([0-9a-fA-F]{6})\Z")


def parse_color(value: object) -> Optional[str]:
    """Normalise a Qubes label colour to ``#rrggbb``, or ``None``.

    Accepts both forms Qubes can produce — ``0xRRGGBB`` (what qubesd returns)
    and ``#RRGGBB`` (what a widget wants) — and returns the ``#`` form in lower
    case, or ``None`` for anything that is not a colour.

    This exists because accepting only ``#rrggbb`` was a real defect: every
    label colour on 4.3 arrives as ``0x…``, so the target rejected all of them
    and drew the fallback hash — a qube labelled *yellow* got a random colour.
    Validating *before* a widget sees the string still matters: ``Gdk.RGBA``
    reports failure by painting black rather than raising (D-20).
    """
    if not isinstance(value, str):
        return None
    match = _LABEL_COLOR_RE.match(value.strip())
    if match is None:
        return None
    return "#" + match.group(1).lower()


#: The official **Nord** palette, verbatim from `nordtheme/nord@develop`
#: (`src/nord.scss`) — copied, not recalled. Polar Night nord0-3, Snow Storm
#: nord4-6, Frost nord7-10, Aurora nord11-15.
NORD0, NORD1, NORD2, NORD3 = "#2e3440", "#3b4252", "#434c5e", "#4c566a"
NORD4, NORD5, NORD6 = "#d8dee9", "#e5e9f0", "#eceff4"
NORD7, NORD8, NORD9, NORD10 = "#8fbcbb", "#88c0d0", "#81a1c1", "#5e81ac"
NORD11, NORD12, NORD13 = "#bf616a", "#d08770", "#ebcb8b"
NORD14, NORD15 = "#a3be8c", "#b48ead"

#: Qubes' eight default labels mapped onto Nord, keeping each label's meaning
#: (D-22). Aurora carries red/orange/yellow/green/purple one-for-one; blue is
#: Frost's deepest tone because nord9/nord8 read cyan next to green.
#:
#: The two neutrals need a choice, and they are the only opinionated entries:
#: Qubes ``black`` (dom0's default) maps to **nord1**, not nord0, because nord0
#: is the Nord *background* and a dot of it disappears; Qubes ``gray`` maps to
#: the light **nord4**, because Nord has no neutral grey — nord3 would land one
#: step from nord1 and black and gray would be indistinguishable on a 14 px dot.
NORD_LABEL_COLORS = {
    "red": NORD11,
    "orange": NORD12,
    "yellow": NORD13,
    "green": NORD14,
    "purple": NORD15,
    "blue": NORD10,
    "black": NORD1,
    "gray": NORD4,
}


def dot_color_for_label(label_name: object,
                        qubesd_color: object = None) -> Optional[str]:
    """The colour a row dot should paint for ``label_name``, or ``None``.

    A qube wearing one of Qubes' eight default labels gets that label's **Nord**
    tone (D-22) — the hue still says what the label says, in the palette the
    desktop is themed with. **No ``admin.label.Get`` call is needed for those**,
    so the common case costs no RPC at all and a refused colour RPC cannot
    blank out a known label.

    Any other label name (a custom label) falls back to the colour qubesd
    reports for it, normalised by ``parse_color``; ``qubesd_color`` is that raw
    value, which the caller should only fetch when this function needs it.
    """
    if isinstance(label_name, str):
        nord = NORD_LABEL_COLORS.get(label_name.strip().lower())
        if nord is not None:
            return nord
    return parse_color(qubesd_color)


#: A fixed, distinct palette for the label colour dot's **fallback**. The dots
#: show the qube's label colour (D-20 → D-22: the Nord tone for Qubes' eight
#: default labels, qubesd's own colour for anything custom);
#: ``qubes_top.ui.labelcolors`` is that source, and this mapping covers the cases
#: where nothing can be read at all: an unknown qube, a failed label read, a
#: client that does not expose labels. It is deterministic (``zlib.crc32``,
#: never ``hash()``) so a fallback dot does not re-shuffle between launches.
_LABEL_PALETTE = [
    (231, 76, 60),    # red
    (46, 204, 113),   # green
    (52, 152, 219),   # blue
    (241, 196, 15),   # yellow
    (155, 89, 182),   # purple
    (230, 126, 34),   # orange
    (26, 188, 156),   # teal
    (236, 110, 193),  # pink
    (149, 165, 196),  # slate
    (249, 153, 57),   # amber
    (52, 73, 94),     # dark slate
    (41, 128, 185),   # steel blue
]


def label_color(name: str) -> str:
    """A deterministic, *stable* name -> colour (hex ``#rrggbb``) from
    ``_LABEL_PALETTE``, as the **fallback** for a dot whose real Qubes label
    could not be read (D-20: the dot shows the qube's label colour; this only
    covers names with no label to read — dom0 before the first label pass
    resolves, a failed read, a client without labels).

    The hash is ``zlib.crc32`` (stable across runs and processes); the
    built-in ``hash()`` is salted by PYTHONHASHSEED and must not be used — it
    would re-shuffle every row's colour on every launch.
    """
    idx = zlib.crc32(name.encode("utf-8")) % len(_LABEL_PALETTE)
    r, g, b = _LABEL_PALETTE[idx]
    return "#%02x%02x%02x" % (r, g, b)
