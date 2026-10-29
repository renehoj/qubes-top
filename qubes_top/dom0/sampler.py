"""Per-tick flow assembling ``qubes_top.core.SampleRecord`` (card
t_e55e68ef; the ingest/synchronize calls belong to the UI tick loop,
which owns the ``SampleModel`` — this module only assembles the records).

Per tick (2 s cadence, D-10):

1. one ``time.monotonic()`` read (D-12: one clock per tick, shared by
   every sample of the tick — the frozen field list does not fix the
   ``ts`` source, so the sampler fixes it);
2. one xenstore pass (one reused handle — the ``Xenstore`` adapter);
3. one qubesadmin domain iteration (the ``QubesAdapter``);
4. one ``SampleRecord`` per running non-stubdom qube, with
   ``name -> domid`` re-resolved from *this tick's* xenstore name map
   (D-5: a domid is a resource id and is reused across restarts; the
   record's ``domid`` field is a bare int, so the "not in this tick's
   map" sentinel ``UNRESOLVED_DOMID`` (-1) cannot leak into series
   logic — the core keys buffers by *name*).
"""
import time
from dataclasses import dataclass
from typing import Callable, FrozenSet, Iterable, List, Optional, Tuple

from qubes_top.core import SampleRecord
from qubes_top.dom0.qubesadmin_adapter import QubesAdapter
from qubes_top.dom0.xenstore_adapter import Xenstore

__all__ = ["Sampler", "Tick", "UNRESOLVED_DOMID"]

#: "The qube's name is not in this tick's xenstore name map" (see the
#: module docstring: the sentinel is inert in the core, which keys
#: buffers by name).
UNRESOLVED_DOMID = -1


@dataclass(frozen=True)
class Tick:
    """One tick's output: the assembled records and the running set
    (``present``) — the latter is exactly the input for
    ``SampleModel.synchronize()``."""
    records: Tuple[SampleRecord, ...]
    present: FrozenSet[str]


class Sampler:
    """Assemble one ``SampleRecord`` per running qube per tick.

    All collaborators are injectable (tests); the defaults construct the
    real adapters (dom0-side). ``running_states`` is the Q-1 seam:
    the default is ``("Running",)``; the owner's call changes it to a
    wider tuple (e.g. including ``"Paused"``) without a redesign.
    """

    def __init__(self, qubes: Optional[QubesAdapter] = None,
                 xenstore: Optional[Xenstore] = None,
                 clock: Callable[[], float] = time.monotonic,
                 running_states: Iterable[str] = ("Running",)) -> None:
        self._qubes = qubes if qubes is not None else QubesAdapter()
        self._xenstore = xenstore if xenstore is not None else Xenstore()
        self._clock = clock
        self._running_states = tuple(running_states)

    def tick(self) -> Tick:
        ts = self._clock()
        by_name = self._xenstore.read_all()
        records: List[SampleRecord] = []
        for view in self._qubes.domains():
            if view.state not in self._running_states:
                continue
            reading = by_name.get(view.name)
            records.append(SampleRecord(
                name=view.name,
                domid=reading.domid if reading is not None else UNRESOLVED_DOMID,
                state=view.state,
                cpu_time_ns=view.cpu_time_ns,
                # The memory figures come from xenstore, not from the API:
                # on Qubes 4.3 CurrentState's `mem` and `mem_static_max` are
                # BOTH libvirt's `virDomainDefGetMemoryTotal`, which is the
                # XML `<memory>` element — the *initial* memory for every
                # memory-hotplug qube (the 4.3 default), and identical to the
                # max otherwise. The configured maximum is `hotplug-max` /
                # `static-max`, and the live allocation is `target` (D-14).
                mem_assigned_kib=_read_or(reading, "target_kib",
                                         view.mem_assigned_kib),
                mem_max_kib=_max_or(reading, view.mem_max_kib),
                mem_used_kib=reading.mem_used_kib
                if reading is not None else None,
                swap_used_kib=reading.swap_used_kib
                if reading is not None else None,
                ts=ts,
            ))
        return Tick(records=tuple(records),
                    present=frozenset(r.name for r in records))


def _read_or(reading, attribute: str, fallback: int) -> int:
    """``reading.<attribute>`` when xenstore had it, else the API fallback."""
    value = getattr(reading, attribute, None) if reading is not None else None
    return value if value is not None and value > 0 else fallback


def _max_or(reading, fallback: int) -> int:
    """The domain's configured maximum from xenstore (``hotplug-max`` else
    ``static-max``), else the API's ``mem_static_max``."""
    if reading is not None:
        value = reading.configured_max_kib()
        if value is not None:
            return value
    return fallback
