"""Qube metadata (template / netvm / vcpus) provider (card t_b14eabf7).

The frozen ``SampleRecord`` carries no template/netvm/vcpus fields (a gap
in the frozen pipeline — the record is frozen by DESIGN.md §5 and this
lane does not reopen it), so the right pane's header gets them from a
*provider seam*, following the ``label_color`` / ``color_for`` precedent:
``QubeMetaProvider`` is the interface the pane's data layer codes against;
a later card can swap in a richer source without touching the pane.

``QubesAdminMetaProvider`` is the dom0 implementation: it reads the
``template`` / ``netvm`` / ``vcpus`` attributes off the ``QubesAdapter``'s
app. The app passes the *same* adapter it builds the collector with, so
there is one ``qubesadmin.Qubes()`` instance — one libvirt connection —
never a second. The ``qubesadmin`` import is deferred (like the dom0
adapter) so this module stays importable on a gi-less, qubesadmin-less
dev box; off-box, use degrades to a clean ``None`` (the header shows
``n/a``) rather than an ImportError traceback from deep inside the stack.

Every read is defensive: a missing or oddly typed attribute yields
``None`` for that field, and a wholesale failure (a wedged app, an
absent library) yields ``None`` for the whole meta — the monitor never
dies on metadata (the design's "no silent crash" rule).
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

__all__ = ["QubeMeta", "QubeMetaProvider", "QubesAdminMetaProvider"]


@dataclass(frozen=True)
class QubeMeta:
    """One qube's static metadata. Every field is ``Optional`` — a missing
    value is an honest ``n/a`` in the header, never a fabricated one."""
    template: Optional[str] = None
    netvm: Optional[str] = None
    vcpus: Optional[int] = None


class QubeMetaProvider(ABC):
    """The metadata seam: the data layer calls ``meta(name)`` and renders
    whatever comes back. A later source (a different qubesadmin binding,
    a cached read) implements this; the pane is untouched."""

    @abstractmethod
    def meta(self, name: str) -> Optional[QubeMeta]:
        """The metadata for one qube, or ``None`` when unknown (the qube
        is not in the source's domain list, or the source is unavailable)."""


def _ref_name(value: object) -> Optional[str]:
    """A qubesadmin ``template``/``netvm`` value: a ``QubesVM`` reference
    (an object with a ``.name``), a bare name, or ``None``. Anything else
    is ``None`` (defensive: an oddly typed attribute is a gap, not a
    crash)."""
    if value is None:
        return None
    name = getattr(value, "name", None)
    if isinstance(name, str):
        # a blank name is "not set" (an API surface that returns ""
        # instead of None): an honest n/a, never a blank label
        return name if name.strip() else None
    if isinstance(value, str):
        return value if value.strip() else None
    return None


def _vcpus(value: object) -> Optional[int]:
    """A ``vcpus`` value: an int (the normal case), or a numeric string
    (some API surfaces stringify it). A non-positive or unparseable value
    is ``None`` (defensive)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            v = int(value)
        except ValueError:
            return None
        return v if v > 0 else None
    return None


class QubesAdminMetaProvider(QubeMetaProvider):
    """The dom0 implementation over the shared ``QubesAdapter`` (one
    ``Qubes()`` instance, shared with the collector's sampler). The
    ``qubesadmin`` import happens only inside the adapter's ``app``
    property — deferred, so importing this module off-box is clean and a
    missing library degrades at *use* time to ``None``, not an
    ImportError."""

    def __init__(self, qubes: Optional[object] = None) -> None:
        # Type: a ``QubesAdapter`` (or a test stand-in with an ``app``
        # attribute). Kept loose on purpose: the seam is structural.
        self._qubes = qubes

    @property
    def app(self) -> object:
        if self._qubes is None:
            # Deferred: keeps this module importable off-box.
            from qubes_top.dom0.qubesadmin_adapter import QubesAdapter
            self._qubes = QubesAdapter()
        return self._qubes.app

    def meta(self, name: str) -> Optional[QubeMeta]:
        try:
            app = self.app
            for vm in app.domains:
                if getattr(vm, "name", None) != name:
                    continue
                return QubeMeta(
                    template=_ref_name(getattr(vm, "template", None)),
                    netvm=_ref_name(getattr(vm, "netvm", None)),
                    vcpus=_vcpus(getattr(vm, "vcpus", None)),
                )
        except Exception:
            # QubesAdminUnavailable (off-box), a wedged app, or an
            # unexpectedly typed domain: degrade to "unknown", never raise
            # into the tick — the monitor must not die on metadata.
            return None
        return None  # the qube is not in this app's domain list
