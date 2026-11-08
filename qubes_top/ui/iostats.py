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
  /proc/net/dev`` and taken from its ``vif<domid>.*`` lines (any devid, so
  a VPN or other non-default netvm works too) — the same bounded
  exception as the guest process table (D-15): fixed argv, no shell, the name
  validated, one read per tick for the selected qube only, on a background
  poller. The netvm writes those numbers, so they are untrusted (D-6):
  digits only, bounded length.

  The backend's ``rx`` is what the qube *sent* and its ``tx`` what the qube
  *received*; ``parse_net_dev`` swaps them so every value here is from the qube's
  side ("in" = received, "out" = sent).

Counters become per-second rates in ``RateMeter`` (a decrease or a new
domid is a gap, never a negative — D-5), and ``IoHistory`` keeps one value
per sampling tick so the charts can draw it on the same fixed window as the
CPU line (``core.window_segments``).
"""
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Hashable, List, Optional, Sequence, Tuple

from qubes_top.core import HISTORY_WINDOW_S, TICK_S
from qubes_top.ui.guestproc import QUBE_NAME_RE, qvm_run_prefix, run_qvm

__all__ = [
    "VBD_ROOT", "SECTOR_BYTES", "read_vbd_bytes", "NET_DEV", "vif_argv", "parse_net_dev",
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


#: The netvm's interface table: one line per interface, always present (a
#: missing ``/sys/class/net/vif<domid>.0`` would leave nothing to say why).
NET_DEV = "/proc/net/dev"

#: A Qubes vif backend: ``vif<frontend domid>.<devid>``, plus the ``-emu``
#: twin an HVM's emulated NIC gets.
_VIF_RE = re.compile(r"vif(\d{1,9})\.(\d{1,4})(?:-emu)?")

#: An interface name worth echoing in a note (D-6: the netvm wrote it).
_IFNAME_RE = re.compile(r"[A-Za-z0-9_.@-]{1,32}")

#: Bounds on the untrusted table: lines read, names echoed in a note.
_MAX_LINES = 4096
_MAX_NAMES = 8


def vif_argv(netvm: str) -> List[str]:
    """The argv that reads the netvm's interface table. ``cat`` of one fixed
    file; no shell (``qvm_run_prefix``)."""
    return qvm_run_prefix(netvm) + ["cat", NET_DEV]


def parse_net_dev(text: Optional[str], domid: int,
                  ) -> Tuple[Optional[Tuple[int, int]], List[str]]:
    """``/proc/net/dev`` -> ``((in, out), names)``.

    ``(in, out)`` is the qube's bytes **from its own side** (the backend's
    tx, then its rx), summed over every ``vif<domid>.*`` line, or None when
    there is none or one of them is not two counters (D-6). ``names`` are the
    table's interface names (sanitized, at most ``_MAX_NAMES``), so a note
    can say what the netvm has instead.
    """
    names: List[str] = []
    rx = tx = 0
    found = bad = False
    for line in (text or "").splitlines()[:_MAX_LINES]:
        name, sep, rest = line.partition(":")
        if not sep:
            continue  # the two header lines
        name = name.strip()
        if _IFNAME_RE.fullmatch(name) and len(names) < _MAX_NAMES:
            names.append(name)
        m = _VIF_RE.fullmatch(name)
        if m is None or int(m.group(1)) != domid:
            continue
        fields = rest.split()
        r = _counter(fields[0]) if len(fields) >= 16 else None
        t = _counter(fields[8]) if len(fields) >= 16 else None
        if r is None or t is None:
            bad = True
            continue
        found = True
        rx += r
        tx += t
    if bad or not found:
        return None, names
    return (tx, rx), names


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
            rc, out, err = self._runner(vif_argv(netvm), self._timeout)
        except Exception as exc:  # a runner bug must not kill the poller
            return NetIoReading(None, self._clock(), "qvm-run failed: %s" % exc)
        ts = self._clock()
        if rc != 0:
            detail = _first_line(err) or ("exit status %s" % (rc,))
            return NetIoReading(None, ts, "%s: %s" % (netvm, detail))
        counters, names = parse_net_dev(out, domid)
        if counters is None:
            return NetIoReading(None, ts, "%s has no vif%d.* (has: %s)" % (
                netvm, domid, ", ".join(names) or "no interfaces"))
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
