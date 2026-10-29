"""Thin dom0 wiring for the core sampling loop (card t_7ce21687).

The loop logic lives in ``qubes_top.core.collector`` (pure, off-box
testable). This module only constructs it with the dom0 sources and
keeps every collaborator injectable (tests inject the fakes; the UI —
M3 — injects nothing or the same fakes via the same seams).

The UI's job (M3, out of scope here): ``collector.subscribe(cb)`` where
``cb`` hands each frame to the GLib main loop via ``GLib.idle_add`` —
the collector's worker thread never touches a widget. Sparkline series
are pulled from ``collector.model`` (a GIL-benign fixed-capacity
in-place ring — the worst case is a snapshot one point behind, which
at a 2 s cadence is invisible to a sparkline).
"""
from typing import Optional

from qubes_top.core import SampleModel
from qubes_top.core.collector import Collector, SourceStatus
from qubes_top.dom0.qubesadmin_adapter import QubesAdapter
from qubes_top.dom0.sampler import Sampler
from qubes_top.dom0.xenstore_adapter import Xenstore

__all__ = ["build_collector"]


def build_collector(*,
                   qubes: Optional[QubesAdapter] = None,
                   xenstore: Optional[Xenstore] = None,
                   model: Optional[SampleModel] = None,
                   interval: float = 2.0,
                   call_timeout: float = 1.0) -> Collector:
    """Assemble the sampling loop with the dom0 sources.

    - one ``Xenstore`` (one reused handle, D-11) shared by the sampler
      (the per-tick pass) and the channel probe (degradation flag);
    - one ``QubesAdapter`` (top-level ``qubesadmin.Qubes()`` attribute,
      D-11; stubdoms skipped — both frozen in the adapters);
    - the channel probe is ``Xenstore.available`` — the *silent*
      degradation signal (library absent / handle not open:
      ``read_all()`` returns ``{}`` and the collector must mark every
      qube ``UNKNOWN`` rather than letting the frozen per-qube state
      machine read the absent meminfo as a per-qube ``MUTE``).
    """
    xenstore = xenstore if xenstore is not None else Xenstore()
    qubes = qubes if qubes is not None else QubesAdapter()
    model = model if model is not None else SampleModel()
    sampler = Sampler(qubes=qubes, xenstore=xenstore)
    return Collector(
        sampler.tick,
        model,
        interval=interval,
        call_timeout=call_timeout,
        source_status=lambda: SourceStatus(xenstore.available),
    )
