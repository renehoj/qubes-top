"""The dom0 (host) stats source (card t_4d9516cd). Read-only, dom0-local.

dom0 is not a qubesadmin domain (the sampler iterates guest domains only), so
its row's data comes from a separate, read-only source: the local ``/proc``.
The dom0 row is **rendered like any other row**: this module supplies a raw
host reading, the app folds it into a *dedicated* ``SampleModel`` (the host
model) as a synthetic ``SampleRecord``, so dom0 gets exactly the same series
treatment as a qube (dense CPU delta via the core ``cpu_delta_pct``, the
30 MiB step memory) — no special-casing in the row layer.

The seam: ``HostStatsProvider`` is the interface (tests inject a fake);
``ProcHostProvider`` is the dom0 implementation (reads ``/proc/stat`` and
``/proc/meminfo``). It is *defensive* — a missing or malformed file returns
``None`` (the app keeps the last known dom0 row) rather than raising into the
tick; the monitor must not die on host data (the design's "no silent crash"
rule). The *metric* arithmetic (the CPU delta, the step quantisation) is the
core model's, never this module's; ``host_to_record`` does only unit
conversion and the trivial ``used = total - available``.
"""
import time
from dataclasses import dataclass
from typing import Callable, Optional

from qubes_top.core import SampleRecord

__all__ = ["HostRaw", "host_to_record", "ProcHostProvider"]


@dataclass(frozen=True)
class HostRaw:
    """One raw host reading. ``cpu_jiffies`` is the *cumulative busy* jiffies
    (a monotonic non-decreasing counter since boot — /proc/stat accumulates,
    so no previous-reading state is needed here; the *delta* is the core
    model's). Memory is point-in-time KiB."""
    cpu_jiffies: int
    ts: float
    mem_total_kib: int
    mem_available_kib: int


def host_to_record(raw: HostRaw, hz: int, name: str = "dom0") -> SampleRecord:
    """A synthetic ``SampleRecord`` for the host. ``hz`` (CLK_TCK) converts the
    cumulative jiffies to a cumulative nanosecond counter so the core
    ``cpu_delta_pct`` (which divides by ``dt * 1e9``) is correct. ``used =
    total - available``; assigned = max = total (the host's own allocation);
    state is always ``Running`` (the host is up). No metric computation — the
    CPU delta is the core model's."""
    if hz <= 0:
        hz = 100
    used = max(0, raw.mem_total_kib - raw.mem_available_kib)
    return SampleRecord(
        name=name, domid=0, state="Running",
        cpu_time_ns=raw.cpu_jiffies * 1_000_000_000 // hz,
        mem_assigned_kib=raw.mem_total_kib,
        mem_max_kib=raw.mem_total_kib,
        mem_used_kib=used,
        swap_used_kib=None,
        ts=raw.ts,
    )


class ProcHostProvider:
    """Reads the host's CPU and memory from ``/proc`` (read-only; the dom0
    user needs no root for these). Every collaborator is injectable (the clock
    and the two file paths) so tests drive it with fakes; on dom0 the defaults
    read the real files. Defensive: a read/parse failure returns ``None``."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 stat_path: str = "/proc/stat",
                 meminfo_path: str = "/proc/meminfo") -> None:
        self._clock = clock
        self._stat_path = stat_path
        self._meminfo_path = meminfo_path

    def read(self) -> Optional[HostRaw]:
        jiffies = self._read_cpu_jiffies()
        if jiffies is None:
            return None
        total, avail = self._read_meminfo()
        if total is None:
            return None
        return HostRaw(cpu_jiffies=jiffies, ts=self._clock(),
                       mem_total_kib=total, mem_available_kib=avail)

    def _read_cpu_jiffies(self) -> Optional[int]:
        """The cumulative *busy* jiffies from the first ``cpu`` line of
        /proc/stat. busy = total - idle - iowait, where total sums user/nice/
        system/idle/iowait/irq/softirq/steal (guest time is excluded — it is
        already counted in user/system, so summing it would double-count)."""
        try:
            with open(self._stat_path) as f:
                for line in f:
                    if not line.startswith("cpu "):
                        continue
                    parts = line.split()[1:]
                    if len(parts) < 4:
                        return None
                    vals = [int(p) for p in parts]
                    user, nice, system, idle = vals[0], vals[1], vals[2], vals[3]
                    iowait = vals[4] if len(vals) > 4 else 0
                    irq = vals[5] if len(vals) > 5 else 0
                    softirq = vals[6] if len(vals) > 6 else 0
                    steal = vals[7] if len(vals) > 7 else 0
                    total = user + nice + system + idle + iowait + irq + \
                        softirq + steal
                    busy = total - idle - iowait
                    return max(0, busy)
        except (OSError, ValueError, IndexError):
            return None
        return None

    def _read_meminfo(self) -> tuple:
        """(MemTotal, MemAvailable) in KiB from /proc/meminfo. ``MemAvailable``
        is absent on very old kernels -> treated as 0 (used == total); a
        missing ``MemTotal`` is a failure -> (None, None)."""
        try:
            total = None
            avail = None
            with open(self._meminfo_path) as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        avail = int(line.split()[1])
            if total is None:
                return (None, None)
            if avail is None:
                avail = 0
            return (total, avail)
        except (OSError, ValueError, IndexError):
            return (None, None)
