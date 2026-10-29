"""The dom0 process source (card t_b14eabf7). Read-only, dom0-local.

The right pane's process panel is v1: **dom0 only**. This module reads
the local ``/proc`` (the dom0 user needs no root for per-process stat
reads) and nothing else; for a guest qube the provider returns ``None``
and the pane shows an explicit "not available for this qube".

The seam is the card's: ``ProcessProvider`` is the interface the *pane*
codes against (it calls ``list(name)`` and renders whatever comes back).
A later qrexec-based agent slots in by implementing this interface — the
pane is never touched.

``Dom0ProcProvider`` is the dom0 implementation. Injectables: ``proc_root``
(the /proc directory), ``clock``, ``hz`` (CLK_TCK), ``page_size``, and
``host_name`` (the name this provider answers for — the app's ``dom0``).
It is **stateful by design**: it keeps the previous per-pid
(``utime + stime``, ``ts``) so the per-process CPU delta is computed by
subtraction — top-like: % of one CPU (may exceed 100 for a
multithreaded process). A first reading for a pid (no baseline yet)
yields ``cpu_pct = None`` (the pane shows "—", mirroring the qube CPU
gap contract — a first sample is never a number). A **pid reuse** (the
counter *dropped*) or a clock regression clamps to ``0.0``: a replaced
process is not a negative reading and not a gap — ``top`` shows it as
idle. Defensive: a vanished or malformed pid is skipped; a wholesale
``listdir`` failure yields ``[]`` (an unreadable /proc is an empty
table, never a crash — the monitor must not die on host data).

The CPU delta computation lives *here* (the data source), not in the
pane: the frozen ``SampleRecord`` carries no per-process counters, so the
process panel has no ``qubes_top/core`` series to draw on — the provider
is the source, exactly as ``ProcHostProvider`` is the dom0 row's source.
"""
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

__all__ = ["ProcessInfo", "ProcessProvider", "Dom0ProcProvider"]


@dataclass(frozen=True)
class ProcessInfo:
    """One process reading. ``cpu_pct`` is the % of ONE CPU (may exceed
    100 for a multithreaded process); ``None`` on a first reading (no
    baseline yet). ``rss_kib`` is the resident set size in KiB; ``None``
    on a page-less read (no value — never a silent zero)."""
    pid: int
    name: str            # the kernel comm (untrusted text — capped on
                        # display by the spec layer)
    cpu_pct: Optional[float]
    rss_kib: Optional[int]


class ProcessProvider(ABC):
    """The process-panel seam (the card's provider interface): the pane
    calls ``list(name)`` and renders whatever comes back — a list for a
    qube the source covers, ``None`` for one it does not (v1: every
    guest). A qrexec-based agent implements this; the pane stays
    untouched."""

    @abstractmethod
    def list(self, name: str) -> Optional[List[ProcessInfo]]:
        """The process list for one qube, or ``None`` when the source
        does not cover it (v1: every non-host name). Never raises into
        the tick (a read failure degrades to an empty list for the
        host)."""


def _hz_default() -> int:
    """CLK_TCK (jiffies per second). Falls back to 100 (the common Linux
    default) if the query is unavailable."""
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        return 100
    return hz if hz and hz > 0 else 100


def _page_default() -> int:
    """The system page size in bytes. Falls back to 4096 (the common
    Linux default) if the query is unavailable."""
    try:
        page = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 4096
    return page if page and page > 0 else 4096


class Dom0ProcProvider(ProcessProvider):
    """The dom0 implementation: reads ``/proc/<pid>/stat`` per pid (the
    comm name between the *first* ``(`` and the *last* ``)`` — it may
    itself contain spaces and parentheses — plus ``utime`` (field 14),
    ``stime`` (field 15) and ``rss`` (field 24, in pages)). No shell, no
    ``qvm-run``, no network: plain file reads of dom0-local state (the
    design's non-goals)."""

    def __init__(self, proc_root: str = "/proc",
                 clock: Callable[[], float] = time.monotonic,
                 hz: Optional[int] = None,
                 page_size: Optional[int] = None,
                 host_name: str = "dom0") -> None:
        self._root = proc_root
        self._clock = clock
        self._hz = hz if (hz and hz > 0) else _hz_default()
        self._page = page_size if (page_size and page_size > 0) \
            else _page_default()
        self._host = host_name
        # pid -> (busy_jiffies, ts) of the previous reading (the baseline
        # for the delta; the /proc counters are cumulative, so no prior
        # state is needed beyond the last reading).
        self._base: Dict[int, Tuple[int, float]] = {}

    def list(self, name: str) -> Optional[List[ProcessInfo]]:
        if name != self._host:
            return None  # v1: guests are not available
        ts = self._clock()
        out: List[ProcessInfo] = []
        seen = set()
        for pid in self._list_pids():
            stat = self._read_stat(pid)
            if stat is None:
                continue
            comm, busy, rss_kib = stat
            seen.add(pid)
            prev = self._base.get(pid)
            if prev is None:
                pct: Optional[float] = None  # first reading: no baseline
            else:
                pbusy, pts = prev
                dt = ts - pts
                delta = busy - pbusy
                if dt <= 0 or delta < 0:
                    pct = 0.0  # a pid reuse / a clock regression: clamp
                else:
                    pct = delta / (dt * self._hz) * 100.0
            self._base[pid] = (busy, ts)
            out.append(ProcessInfo(
                pid=pid, name=comm, cpu_pct=pct,
                # a zero-page read has no value (a dying pid / a kernel
                # thread): None, never a silent zero (D-4)
                rss_kib=rss_kib if rss_kib > 0 else None))
        for pid in list(self._base):
            if pid not in seen:
                del self._base[pid]
        return out

    def _list_pids(self) -> List[int]:
        try:
            entries = os.listdir(self._root)
        except OSError:
            return []
        out = []
        for e in entries:
            if e.isdigit():
                out.append(int(e))
        return out

    def _read_stat(self, pid: int) -> Optional[Tuple[str, int, int]]:
        """``(comm, busy_jiffies, rss_kib)`` from ``/proc/<pid>/stat``;
        ``None`` on any problem (the pid vanished between the listdir and
        the read, or the file is malformed — kernel threads and dying
        pids do this every tick)."""
        try:
            with open(os.path.join(self._root, str(pid), "stat")) as f:
                line = f.readline()
        except OSError:
            return None
        try:
            l = line.index("(")
            r = line.rindex(")")
            if l < 0 or r < l:
                return None
            comm = line[l + 1:r]
            fields = line[r + 1:].split()
            # After the comm: kernel field 3 -> index 0. utime = field 14
            # -> index 11; stime = field 15 -> index 12; rss (pages) =
            # field 24 -> index 21.
            busy = int(fields[11]) + int(fields[12])
            pages = int(fields[21])
        except (ValueError, IndexError):
            return None
        return (comm, max(0, busy), max(0, pages) * self._page // 1024)
