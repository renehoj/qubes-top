"""qubesadmin adapter: per-domain metrics from the qubesadmin API.

Constructs with the **top-level** package attribute ``qubesadmin.Qubes()``
(DESIGN.md D-11). Verified against the real package source
(QubesOS/qubes-core-admin-client@master, ``qubesadmin/__init__.py``):
the package binds ``Qubes = app.QubesLocal`` when ``/etc/qubes-release``
exists (dom0) and ``Qubes = app.QubesRemote`` otherwise — one
package-level attribute, bound per environment. ``qubesadmin.app``
defines ``QubesBase`` / ``QubesLocal`` / ``QubesRemote`` and has **no**
``Qubes`` attribute: ``qubesadmin.app.Qubes`` does not exist, and the
previous project's first live run died on exactly that (while 575 tests
passed). D-11 fixes the attribute so the bug cannot reappear by a
convenience edit.

Per domain, the frozen read set (DESIGN.md §5) — all four figures come from
one ``admin.vm.CurrentState`` call, because the client has no accessor that
returns cpu time: ``get_power_state()`` (str: Running/Paused/...),
``get_mem()`` (**the XML ``<memory>`` total** — the initial size on Qubes 4.3
for memory-hotplug qubes; NOT the live allocation, D-14), ``cputime``
(cumulative counter in **nanoseconds** — upstream's equivalent accessor
returns libvirt's ``virDomainInfo.cpuTime``, documented in ns; the
``[FIXME unit]`` note in ``qubes/vm/qubesvm.py`` was undocumented, not
unknown), ``mem_static_max`` (**the same total** in 4.3 — ``maxMemory()`` is
``virDomainDefGetMemoryTotal()`` too). The memory figures the UI shows come
from xenstore instead (``hotplug-max``/``static-max``/``target``); these API
values are the fallback.

Qubes 4.3.2 has no ``get_cputime()``: calling it does not raise
AttributeError, it becomes a *property* lookup in qubesd and fails with
QubesNoSuchPropertyError — the probe caught this on the real target.

Runs as the invoking dom0 user — no root, no helper service.

The qubesadmin import is deferred to first ``app`` access, so this module
stays importable on a box without qubesadmin; absence is a clean
``QubesAdminUnavailable`` at *use* time, never an ImportError at import
time.

Stubdomains: the raw xenstore/xl view *includes* stubdomains (names
ending ``-dm``); the API-driven domain list is what ``domains()``
iterates, and any straggler is filtered by the same ``-dm`` suffix
heuristic the mgmt-API stats path itself uses (QubesOS/qubes-core-admin@
main, ``qubes/app.py``, ``get_vm_stats`` line 638:
``name.endswith("-dm")``).
"""
from typing import Callable, List, Optional

__all__ = [
    "QubesAdapter", "QubesAdminUnavailable", "DomainView", "is_stubdom_name",
]


class QubesAdminUnavailable(RuntimeError):
    """qubesadmin is not importable in this environment (expected off-box;
    the adapter is dom0-side). Explicit at use time, never an ImportError
    at import time."""


def is_stubdom_name(name: str) -> bool:
    """The ``-dm`` suffix — the mgmt-API stats path's own stubdom
    heuristic (citation in the module docstring)."""
    return name.endswith("-dm")


def read_current_state(vm) -> dict:
    """``admin.vm.CurrentState`` as a dict of ASCII strings.

    Mirrors qubesadmin's own ``_get_current_state`` (vm/__init__.py) and uses
    it when the class actually defines it — the class, not the instance, so a
    client without it cannot turn ``getattr`` into a qubesd property lookup.
    The raw RPC is the fallback, so a client rename degrades to the documented
    method instead of raising AttributeError.
    """
    getter = getattr(type(vm), "_get_current_state", None)
    if callable(getter):
        return getter(vm)
    raw = vm.qubesd_call(vm._method_dest, "admin.vm.CurrentState")
    state = {}
    for part in raw.decode("ascii").split():
        key, _, value = part.partition("=")
        state[key] = value
    return state


class DomainView:
    """One domain from ``app.domains``. Identity + power state are read up
    front; the metric reads are lazy, so a running qube costs all four and
    the join happens per tick (2 s cadence, D-10)."""
    __slots__ = ("name", "state", "_vm", "_cached")

    def __init__(self, vm) -> None:
        self._vm = vm
        self._cached = None
        self.name = vm.name
        self.state = self._state().get("power_state", "NA")

    def _state(self) -> dict:
        """One ``admin.vm.CurrentState`` per view, shared by every fact.

        qubesadmin 4.3.2 has NO ``get_cputime()``: because ``QubesVM`` defines
        ``__getattr__``, an unknown attribute is resolved as a *property*
        lookup in qubesd and raises QubesNoSuchPropertyError ("Invalid
        property 'get_cputime'"). CPU time is in the CurrentState response —
        ``get_mem()`` is a thin wrapper over the same call — so read it once
        and take all four figures from it. ``mem_static_max`` only exists in
        this response too (the qubesd *property* is ``maxmem``).
        """
        if self._cached is None:
            self._cached = read_current_state(self._vm)
        return self._cached

    @property
    def mem_assigned_kib(self) -> int:
        """``CurrentState``'s ``mem`` — **the initial memory on Qubes 4.3**,
        not the live allocation and not the configured maximum (D-14).

        Upstream `release4.3` (``qubes/vm/qubesvm.py``): ``get_mem()`` returns
        ``libvirt_domain.info()[1]``, and libvirt's libxl driver fills
        ``info->maxMem`` from ``virDomainDefGetMemoryTotal()`` — the XML
        ``<memory>`` element. Qubes 4.3 writes that element as ``vm.memory``
        (the *initial* size) for every memory-hotplug qube, which is the
        default for PVH/PV qubes with a Qubes kernel; only non-hotplug qubes
        get ``vm.maxmem`` there. Since 4.3 also fills ``info->memory`` from
        the same definition, ``mem`` and ``mem_static_max`` come back equal —
        which is why dividing "used" by either one showed the initial size
        (the owner's "it says 1000 MiB, it's assigned 4 GiB").

        The sampler prefers xenstore for both figures: ``memory/target`` for
        the live allocation and ``memory/hotplug-max``/``static-max`` for the
        configured maximum. This API value is only the fallback (and the only
        value available for a domain that has no xenstore keys).
        """
        return int(self._state()["mem"])

    @property
    def cpu_time_ns(self) -> int:
        """Cumulative cpu_time counter (unit contract: see module
        docstring). The core's delta formula is ns-based."""
        return int(self._state()["cputime"])

    @property
    def mem_max_kib(self) -> int:
        """``CurrentState``'s ``mem_static_max`` — on Qubes 4.3 the *same*
        value as ``mem`` (both are libvirt's memory total), so this is NOT the
        configured maximum either: it is the XML ``<memory>``, i.e. the initial
        memory for hotplug qubes (D-14).

        Upstream `release4.3`: ``get_mem_static_max()`` returns
        ``libvirt_domain.maxMemory()``, and libvirt's libxl driver answers
        that with ``virDomainDefGetMemoryTotal()`` too. The real maximum is
        ``<maxMemory>`` in the XML (``vm.maxmem``), which Qubes mirrors into
        xenstore ``memory/hotplug-max``; the sampler reads it there.
        """
        return int(self._state()["mem_static_max"])


class QubesAdapter:
    """Per-domain metrics over the qubesadmin API.

    ``app`` is injectable for tests. When not injected, the package is
    imported (deferred) and the package-level, environment-bound
    ``Qubes`` is constructed — D-11's top-level attribute, zero-arg.
    """

    def __init__(self, app: Optional[object] = None,
                 is_stubdom: Callable[[str], bool] = is_stubdom_name) -> None:
        self._app = app
        self._is_stubdom = is_stubdom

    @property
    def app(self):
        if self._app is None:
            try:
                # Deferred: keeps this module importable off-box.
                import qubesadmin
            except ModuleNotFoundError as exc:
                raise QubesAdminUnavailable(
                    "qubesadmin is not importable in this environment "
                    "(expected off-box; the adapter is dom0-side)") from exc
            # D-11: the top-level package attribute — bound per
            # environment at package level (QubesLocal on dom0,
            # QubesRemote off-box). Zero-arg, as in the real package.
            self._app = qubesadmin.Qubes()
        return self._app

    def domains(self) -> List[DomainView]:
        """All non-stubdom domains, state read; metric reads lazy (see
        ``DomainView``)."""
        return [DomainView(vm) for vm in self.app.domains
                if not self._is_stubdom(vm.name)]
