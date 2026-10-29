"""The Qubes label colour behind each row's dot (**D-20**).

Everywhere else in Qubes the dot beside a qube's name is its **label** — the
security one-word classification the user already reads by reflex (red =
untrusted, purple = work, …). Until D-20 the dot here was a ``zlib.crc32`` hash
of the name: stable, but meaningless, and actively misleading in a monitor of a
security-oriented system, because it dresses an arbitrary colour in the same
visual language as a real label.

This module is the real source. It is pure — no ``gi``, no top-level
``qubesadmin`` import — and it degrades to the hash (``core.label_color``) for
anything it cannot read, so a dot is always drawn and never a crash.

**The API, read from source** (``QubesOS/qubes-core-admin-client@release4.3``,
the branch the target runs), because the shape here is easy to get wrong and a
wrong guess only fails on dom0:

* ``vm.label`` is not an attribute on ``QubesVM`` at all — ``QubesVM`` has no
  ``label`` in ``vm/__init__.py``. It is resolved by ``PropertyHolder.__getattr__``
  (``base.py``), which issues ``admin.vm.property.Get label`` and, for a
  property whose qubesd type is ``label``, returns
  ``self.app.labels.get_blind(value)`` — a **``label.Label`` object**, not a
  string (``base.py``, ``_parse_type_value``). An empty value is ``None``.
* ``label.name`` is a plain attribute (``'red'``, ``'green'``, …).
* ``label.color`` is an ``admin.label.Get <name>`` RPC to ``dom0`` returning the
  HTML spec in Qubes' own form (``0xcc0000`` — see ``core.parse_color``); the
  result is cached **on the Label instance** (``label.py``).
  ``app.labels.get_blind`` returns the same instance per name for the process's
  lifetime, so the RPC is paid once per distinct label.

**Which colour a dot paints (D-22).** For Qubes' eight default labels the dot
shows that label's **Nord** tone (``core.NORD_LABEL_COLORS``): same meaning
(red is red), in the palette the desktop is themed with. Those eight need no
colour RPC at all — saved work and one less way to lose a dot. A *custom* label
name falls back to the colour qubesd reports for it.

**Cost, and why the cache exists.** The first pass costs one
``admin.vm.property.Get`` per domain (≈ N RPCs) plus one ``admin.label.Get`` per
distinct **custom** label (the eight defaults are answered from the Nord map and
cost no colour RPC at all — D-22); the labels' colours are then free for the
process. The
``property.Get`` calls are not, so the whole domain list is re-read on a TTL
(60 s, ``refresh_interval``) and immediately when an unknown name is asked for
(a qube that started after the last pass gets its real colour on the next
frame, not up to a minute of hash). That is N RPCs per minute against the
sampler's N per 2 s tick, so the extra load is noise — and no label read ever
runs per row.

**A failed read is not a removal.** A transient ``property.Get`` failure keeps
the colour already known for that qube rather than flipping its dot to the
fallback and back; a qube genuinely gone from ``app.domains`` is dropped, so a
recreated qube with the same name cannot inherit the old one's colour.
"""
import time
from typing import Any, Callable, Dict, Optional

from qubes_top.core import dot_color_for_label, label_color, parse_color

__all__ = ["LabelColorProvider", "DEFAULT_REFRESH_S"]

#: How long a label pass is trusted, in seconds. Labels change by user action
#: in Settings, so a minute of staleness is invisible; the cost it buys is
#: N ``admin.vm.property.Get`` calls per minute instead of per frame.
DEFAULT_REFRESH_S = 60.0


def _label_color_hex(label) -> Optional[str]:
    """The ``#rrggbb`` colour of one ``qubesadmin.label.Label``, or ``None``.

    ``label.color`` raises ``QubesPropertyAccessError`` when
    ``admin.label.Get`` gets no response (``label.py``), so this is the one
    read that must be guarded per call. The value arrives as **``0xRRGGBB``**
    (``qubes-core-admin@release4.3`` ``qubes/app.py``:
    ``Label(3, "0xedd400", "yellow")``, and ``admin.label.Get`` returns
    ``label.color`` verbatim) — ``core.parse_color`` normalises it to the
    ``#rrggbb`` a widget needs, and returns ``None`` for anything else rather
    than letting ``Gdk.RGBA.parse`` paint black.
    """
    try:
        colour = label.color
    except Exception:
        return None
    return parse_color(colour)


class LabelColorProvider:
    """``color_for(name) -> '#rrggbb'`` from the qube's real Qubes label.

    The callable is what the UI already takes (``LeftPaneModel`` and
    ``RightPane`` both accept a ``color_for``), so the rendering layer is
    untouched by D-20 — it stops receiving a hash and starts receiving a label
    colour.

    ``qubes`` is a ``QubesAdapter`` (or a duck-typed stand-in with ``.app``);
    ``clock`` and ``refresh_interval`` are injectable so the TTL is testable
    without sleeping. Nothing here raises: a provider that dies would take a
    frame's rendering with it, and a dot is not worth a blank window.
    """

    def __init__(self, qubes: Optional[object] = None,
                 refresh_interval: float = DEFAULT_REFRESH_S,
                 clock: Callable[[], float] = time.monotonic,
                 fallback: Callable[[str], str] = label_color) -> None:
        self._qubes: Any = qubes
        self._interval = float(refresh_interval)
        self._clock = clock
        self._fallback = fallback
        #: qube name -> '#rrggbb' from the last successful pass.
        self._by_qube: Dict[str, str] = {}
        self._last_pass: Optional[float] = None
        #: name -> the pass that confirmed it is *not* a domain. Without this,
        #: every ``color_for`` for an unknown name (a stale selection, or any
        #: name at all while qubesd is wedged and the map stays empty) would
        #: re-read the whole domain list — a per-row RPC storm dressed up as a
        #: cache. Bounded by the number of names ever asked about.
        self._checked: Dict[str, float] = {}

    # -- the seam the UI takes ---------------------------------------------
    def color_for(self, name: Optional[str]) -> str:
        """The dot colour for ``name``: the qube's label, else the fallback."""
        key = "" if name is None else str(name)
        if self._stale() or (key and key not in self._by_qube
                             and not self._recently_checked(key)):
            self._refresh()
            if key and key not in self._by_qube:
                # This pass saw the domains and did not find the name: it is
                # not a qube (or has no readable label). Do not ask again
                # until the interval is up.
                self._checked[key] = (self._last_pass
                                      if self._last_pass is not None else
                                      self._clock())
        return self._by_qube.get(key) or self._fallback(key)

    # -- the pass ----------------------------------------------------------
    @property
    def app(self):
        """The qubesadmin app, via the shared adapter (one ``Qubes()``
        instance — the same one the sampler and the metadata provider use).

        Deferred, exactly as ``QubesAdminMetaProvider`` does it: this module
        must import on a box without ``qubesadmin``.
        """
        if self._qubes is None:
            from qubes_top.dom0.qubesadmin_adapter import QubesAdapter
            self._qubes = QubesAdapter()
        return self._qubes.app

    def _stale(self) -> bool:
        if self._last_pass is None:
            return True
        return (self._clock() - self._last_pass) >= self._interval

    def _recently_checked(self, key: str) -> bool:
        """True when a recent pass already confirmed ``key`` is not a domain."""
        at = self._checked.get(key)
        if at is None:
            return False
        return (self._clock() - at) < self._interval

    def _refresh(self) -> None:
        """Re-read every domain's label. Never raises.

        The timestamp is stamped **first**: a wedged qubesd must not be asked
        again on every single ``color_for`` call (the cache would otherwise
        turn a slow daemon into a per-row RPC storm).
        """
        self._last_pass = self._clock()
        try:
            domains = list(self.app.domains)
        except Exception:
            # The daemon is unavailable (or the client has no labels): keep
            # the map we already have and let the fallback cover the rest.
            return
        painting = dict(self._by_qube)  # a failed read is not a removal
        seen = set()
        #: label name -> colour, for this pass only: one ``label.color`` read
        #: per *distinct label*, however many qubes carry it.
        label_hex: Dict[str, Optional[str]] = {}
        for vm in domains:
            try:
                name = vm.name
            except Exception:
                continue
            if not isinstance(name, str) or not name:
                continue
            seen.add(name)
            try:
                label = vm.label          # admin.vm.property.Get label
            except Exception:
                continue                  # keep the colour we had, if any
            if label is None:
                continue                  # no label set: fallback dot
            try:
                label_name = label.name
            except Exception:
                continue
            if label_name not in label_hex:
                # A Qubes default label is answered from the Nord map alone
                # (D-22) — no colour RPC, and a refused RPC cannot blank it;
                # only a custom label name has to ask qubesd what its colour is.
                colour = dot_color_for_label(label_name)
                if colour is None:
                    colour = _label_color_hex(label)
                label_hex[label_name] = colour
            colour = label_hex[label_name]
            if colour is None:
                continue                  # unreadable label: fallback dot
            painting[name] = colour
        for gone in [n for n in painting if n not in seen]:
            del painting[gone]            # a recreated qube starts clean
        self._by_qube = painting

    # -- diagnostics -------------------------------------------------------
    def colors(self) -> Dict[str, str]:
        """A snapshot of the current map (name -> colour), refreshed if stale.

        For the staged probe and for tests; the UI uses ``color_for``.
        """
        if self._stale():
            self._refresh()
        return dict(self._by_qube)
