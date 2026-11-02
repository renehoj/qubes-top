"""Disk and network I/O of the **selected** qube (D-27). Pure, no ``gi``.

Two sources, because the two kinds of traffic are counted in different
domains:

* **Disk** — Qubes serves every qube's virtual disks from dom0 (blkback), so
  the kernel here counts them, per frontend domain and device, under
  ``/sys/bus/xen-backend/devices/vbd-<domid>-<devid>/statistics/``
  (``rd_sect`` / ``wr_sect``, 512-byte sectors; the same files ``xentop``
  and libxenstat read). A plain file read in dom0: no fork, no guest
  involved, and the numbers are the backend's, not the guest's.
* **Network** — a qube's ``vif`` backend lives in its **netvm**
  (sys-firewall, usually), not in dom0; dom0's ``/proc/net/dev`` has nothing
  for it, which is why ``xentop``'s NETTX/NETRX read 0 on Qubes. The counters
  are read in the netvm with ``qvm-run -p --no-shell <netvm> cat
  /sys/class/net/vif<domid>.0/statistics/{rx,tx}_bytes`` — the same bounded
  exception as the guest process table (D-15): fixed argv, no shell, the name
  validated, one read per tick for the selected qube only, on a background
  poller. The netvm writes those numbers, so they are untrusted (D-6):
  digits only, bounded length.

  The backend's ``rx`` is what the qube *sent* and its ``tx`` what the qube
  *received*; ``parse_vif`` swaps them so every value here is from the qube's
  side ("in" = received, "out" = sent).

Counters become per-second rates in ``RateMeter`` (a decrease or a new
domid is a gap, never a negative — D-5), and ``IoHistory`` keeps one value
per sampling tick so the charts can draw it on the same fixed window as the
CPU line (``core.window_segments``).
"""
import os
import time
from dataclasses import dataclass
from typing import Callable, Hashable, List, Optional, Sequence, Tuple

from qubes_top.core import HISTORY_WINDOW_S, TICK_S
from qubes_top.ui.guestproc import QUBE_NAME_RE, qvm_run_prefix, run_qvm

__all__ = [
    "VBD_ROOT", "SECTOR_BYTES", "read_vbd_bytes", "vif_argv", "parse_vif",
    "NetIoReading", "NetIoProvider", "RateMeter", "IoHistory",
]

#: Where blkback registers one device per (frontend domid, virtual disk).
VBD_ROOT = "/sys/bus/xen-backend/devices"

#: blkback counts in 512-byte sectors whatever the disk's own sector size.
SECTOR_BYTES = 512

#: A counter is a u64: at most 20 digits. Anything longer is not a counter.
_MAX_DIGITS = 20


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="ascii") as fh:
            return fh.read(64)
    except (OSError, ValueError):
        return None


def _counter(text: Optional[str]) -> Optional[int]:
    """A counter value: ASCII digits only (D-6), at most a u64's length."""
    if text is None:
        return None
    t = text.strip()
    if not t or len(t) > _MAX_DIGITS or not (t.isascii() and t.isdigit()):
        return None
    return int(t)


def read_vbd_bytes(domid: int, root: str = VBD_ROOT,
                   listdir: Callable[[str], List[str]] = os.listdir,
                   read: Callable[[str], Optional[str]] = _read_text,
                   ) -> Optional[Tuple[int, int]]:
    """The domain's ``(read, written)`` bytes summed over all its virtual
    disks, or None when it has none or one could not be read.

    A partly read sum is refused rather than returned: a disk missing from
    one sum and present in the next would read as a burst of I/O.
    """
    if domid < 0:
        return None
    prefix = "vbd-%d-" % domid
    try:
        names = [n for n in listdir(root) if n.startswith(prefix)]
    except OSError:
        return None
    if not names:
        return None
    rd = wr = 0
    for name in names:
        stats = os.path.join(root, name, "statistics")
        r = _counter(read(os.path.join(stats, "rd_sect")))
        w = _counter(read(os.path.join(stats, "wr_sect")))
        if r is None or w is None:
            return None
        rd += r
        wr += w
    return rd * SECTOR_BYTES, wr * SECTOR_BYTES


def vif_argv(netvm: str, domid: int) -> List[str]:
    """The argv that reads one qube's vif counters in its netvm. ``cat``
    prints the two files in order; no shell (``qvm_run_prefix``)."""
    base = "/sys/class/net/vif%d.0/statistics/" % domid
    return qvm_run_prefix(netvm) + ["cat", base + "rx_bytes",
                                    base + "tx_bytes"]


def parse_vif(text: Optional[str]) -> Optional[Tuple[int, int]]:
    """``cat``'s two lines -> ``(in, out)`` bytes **from the qube's side**
    (the backend's tx, then its rx), or None unless both are counters."""
    fields = (text or "").split()
    if len(fields) != 2:
        return None
    rx, tx = _counter(fields[0]), _counter(fields[1])
    if rx is None or tx is None:
        return None
    return tx, rx


@dataclass(frozen=True)
class NetIoReading:
    """One netvm reading: the ``(in, out)`` counters (None = unreadable),
    when they were read (``time.monotonic``), and a note for an absence."""

    counters: Optional[Tuple[int, int]]
    ts: float
    note: str = ""


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:160]
    return ""


class NetIoProvider:
    """Reads ``(netvm, domid)`` targets for ``GuestProcPoller`` (the same
    one-fetch-at-a-time, selected-only poller as the process table). Never
    raises: a failure is a reading with a note."""

    def __init__(self, runner: Optional[Callable[..., Tuple[int, str, str]]] = None,
                 timeout: float = 8.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._runner = runner if runner is not None else run_qvm
        self._timeout = timeout
        self._clock = clock

    def read(self, target: Tuple[str, int]) -> NetIoReading:
        netvm, domid = target
        if not QUBE_NAME_RE.match(netvm or "") or not isinstance(domid, int) \
                or domid < 0:
            return NetIoReading(None, self._clock(), "not a valid target")
        try:
            rc, out, err = self._runner(vif_argv(netvm, domid), self._timeout)
        except Exception as exc:  # a runner bug must not kill the poller
            return NetIoReading(None, self._clock(), "qvm-run failed: %s" % exc)
        ts = self._clock()
        if rc != 0:
            detail = _first_line(err) or ("exit status %s" % (rc,))
            return NetIoReading(None, ts, "%s: %s" % (netvm, detail))
        counters = parse_vif(out)
        if counters is None:
            return NetIoReading(None, ts, "%s printed no vif counters" % netvm)
        return NetIoReading(counters, ts)


class RateMeter:
    """Cumulative counters -> per-second rates, for one source at a time.

    ``feed(key, ts, counters)`` returns the rates since the previous feed, or
    None when there is no usable baseline: the first reading, a new ``key``
    (another qube, or the same qube under a new domid — D-5), an unreadable
    reading, or a counter that went *down* (a re-attached disk, a recreated
    vif). A gap, never a negative and never a zero.
    """

    def __init__(self) -> None:
        self._prev: Optional[Tuple[Hashable, float, Tuple[int, ...]]] = None

    def feed(self, key: Hashable, ts: float,
             counters: Optional[Sequence[int]]) -> Optional[Tuple[float, ...]]:
        if counters is None:
            self._prev = None
            return None
        cur = tuple(counters)
        prev, self._prev = self._prev, (key, ts, cur)
        if prev is None or prev[0] != key or len(prev[2]) != len(cur):
            return None
        dt = ts - prev[1]
        if dt <= 0.0 or any(c < p for c, p in zip(cur, prev[2])):
            return None
        return tuple((c - p) / dt for c, p in zip(cur, prev[2]))


#: One value per tick over the charts' window (the rings' D-10 size).
RING = int(HISTORY_WINDOW_S / TICK_S)


class IoHistory:
    """The selected qube's I/O rates, one value per sampling tick (None = a
    gap), newest last — the shape ``core.window_segments`` draws.

    It holds **one** qube: a new ``key`` starts it over. The history is only
    sampled while the qube is selected, so keeping another qube's older
    values would draw them as if they were contiguous with the new ones.
    The notes say why a chart is empty, in words.
    """

    def __init__(self, capacity: int = RING) -> None:
        self._capacity = capacity
        self.key: Optional[Hashable] = None
        self.disk_read: List[Optional[float]] = []
        self.disk_write: List[Optional[float]] = []
        self.net_in: List[Optional[float]] = []
        self.net_out: List[Optional[float]] = []
        self.disk_note = ""
        self.net_note = ""
        self.netvm = ""

    def reset(self, key: Optional[Hashable]) -> None:
        """Start over for ``key`` (no-op when it is the current one)."""
        if key == self.key:
            return
        self.key = key
        for series in self._series():
            series.clear()
        self.disk_note = self.net_note = self.netvm = ""

    def push(self, key: Hashable,
             disk: Optional[Sequence[float]],
             net: Optional[Sequence[float]]) -> None:
        """Append this tick's ``(read, write)`` and ``(in, out)`` rates."""
        self.reset(key)
        for series, value in zip(self._series(),
                                 (*(disk or (None, None)),
                                  *(net or (None, None)))):
            series.append(value)
            del series[:-self._capacity]

    def _series(self):
        return (self.disk_read, self.disk_write, self.net_in, self.net_out)
