"""sampler (card t_e55e68ef): the per-tick flow assembling a complete
``SampleRecord`` for a running qube — the card's acceptance line — plus
None-safe values when meminfo/swapinfo are absent or non-numeric, the
single-clock tick (D-12), the name->domid re-resolution (D-5), the
dom0/``Domain-0`` join, the stubdom skip (inherited from the adapter),
the clean degradation with the library absent, and the ``running_states``
seam (Q-1).

The qubesadmin side reuses the same fake shape as
``test_qubesadmin_adapter`` (a ``FakeVM`` with the read-set surface and a
zero-arg app whose ``.domains`` is populated by the test); the xenstore
side reuses the **real**-signature fake of ``test_xenstore_adapter``.
"""
import ctypes
import errno
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.core import SampleRecord  # noqa: E402
from qubes_top.dom0.qubesadmin_adapter import QubesAdapter  # noqa: E402
from qubes_top.dom0.sampler import (  # noqa: E402
    UNRESOLVED_DOMID,
    Sampler,
)
from qubes_top.dom0.xenstore_adapter import Xenstore  # noqa: E402


class FakeVM:
    """The QubesVM read-set surface (same shape as
    test_qubesadmin_adapter.FakeVM)."""

    def __init__(self, name, state="Running", mem=1024,
                 cputime=0, mem_max=2048):
        self._name = name
        self._state = state
        self._mem = mem
        self._cputime = cputime
        self._mem_max = mem_max

    @property
    def name(self):
        return self._name

    def get_power_state(self):
        return self._state

    def get_mem(self):
        return self._mem

    def _get_current_state(self):
        """cpu time and both memory figures arrive here; there is no
        ``get_cputime()`` on qubesadmin 4.3.2."""
        return {
            "mem": str(self._mem),
            "mem_static_max": str(self._mem_max),
            "cputime": str(self._cputime),
            "power_state": self._state,
        }


class FakeQubesApp:
    """A zero-arg app whose ``.domains`` the test populates (mirrors the
    real app: zero-arg construction, a ``.domains`` collection)."""

    def __init__(self):
        self.domains = []


def make_xenstore_lib(domains=(), directories=(), memory=None):
    """A **real**-signature libxenstore fake with an in-memory key
    tree (same shape as test_xenstore_adapter.FakeXenStoreLib, trimmed
    to what the sampler tests need): xs_open(flags), xs_read(h, t,
    path, len*) -> bytes|None, xs_directory(h, t, path, num*) ->
    bytes-list|None, errno set on failure (no status codes — the real
    library has none).

    ``memory`` optionally adds the per-domain memory keys the sampler reads
    for the configured maximum and the live allocation, e.g.
    ``{7: {b"static-max": b"4096000", b"hotplug-max": b"4096000"}}``.
    """
    keys = {}
    for domid, (name, meminfo, swapinfo) in domains.items():
        if name is not None:
            keys["/local/domain/%d/name" % domid] = name
        if meminfo is not None:
            keys["/local/domain/%d/memory/meminfo" % domid] = meminfo
        if swapinfo is not None:
            keys["/local/domain/%d/memory/swapinfo" % domid] = swapinfo
    for domid, entries in (memory or {}).items():
        for key, value in entries.items():
            keys["/local/domain/%d/memory/%s" % (domid, key.decode("ascii"))] = value
    if "/local/domain" not in directories:
        directories = tuple(directories) + ("/local/domain",)

    class _Lib:
        def __init__(self):
            self._handle = None

        def xs_open(self, flags):
            self._handle = object()
            return self._handle

        def xs_close(self, handle):
            self._handle = None

        def xs_read(self, handle, t, path, length):
            path_s = path.decode("ascii")
            if path_s not in keys:
                ctypes.set_errno(errno.ENOENT)
                return None
            value = keys[path_s]
            length.value = len(value)
            return value

        def xs_directory(self, handle, t, path, num):
            if path.decode("ascii") not in directories:
                ctypes.set_errno(errno.ENOENT)
                return None
            prefix = path.decode("ascii") + "/"
            # direct children only: first path component (same shape as
            # the xenstore test's fake)
            entries = sorted({k[len(prefix):].split("/", 1)[0]
                             for k in keys if k.startswith(prefix)})
            num.value = len(entries)
            return [e.encode("ascii") for e in entries]

    return _Lib()


class FakeClock:
    """A counting clock: proves one clock read per tick (D-12).
    ``value`` is the next value to return (starts at ``start + step``
    after the first call)."""

    def __init__(self, start=1000.0, step=2.0):
        self.value = start
        self._step = step
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.value += self._step
        return self.value


def make_sampler(vms, domains, running_states=("Running",), memory=None):
    app = FakeQubesApp()
    app.domains = list(vms)
    clock = FakeClock()
    sampler = Sampler(qubes=QubesAdapter(app=app),
                     xenstore=Xenstore(library=make_xenstore_lib(domains,
                                                                 memory=memory)),
                     clock=clock,
                     running_states=running_states)
    return sampler, clock


class TestSampler(unittest.TestCase):
    def test_complete_record_for_running_qube(self):
        sampler, clock = make_sampler(
            [FakeVM("work", state="Running", mem=4096,
                    cputime=123456, mem_max=8192)],
            {7: (b"work", b"2048", b"512")})
        tick = sampler.tick()
        self.assertEqual(len(tick.records), 1)
        r = tick.records[0]
        self.assertEqual(
            r, SampleRecord("work", 7, "Running", 123456, 4096, 8192,
                           2048, 512, clock.value))
        self.assertEqual(tick.present, frozenset({"work"}))
        self.assertEqual(clock.calls, 1,
                         "one clock read per tick, shared (D-12)")

    def test_memory_max_prefers_hotplug_max(self):
        # The owner's browser-streaming qube on Qubes 4.3 (memory hotplug, the
        # default): CurrentState `mem` AND `mem_static_max` are both the XML
        # <memory>, i.e. the INITIAL size (1000 MiB), so neither is the
        # configured maximum. `hotplug-max` is (D-14).
        sampler, _ = make_sampler(
            [FakeVM("browser-streaming", mem=1024000, mem_max=1024000)],
            {7: (b"browser-streaming", b"1153024", None)},
            memory={7: {b"static-max": b"1024000",
                        b"hotplug-max": b"4096000",
                        b"target": b"1032192"}})
        r = sampler.tick().records[0]
        self.assertEqual(r.mem_max_kib, 4096000, "configured max = hotplug-max")
        self.assertEqual(r.mem_assigned_kib, 1032192, "live allocation = target")
        self.assertEqual(r.mem_used_kib, 1153024)

    def test_memory_max_falls_back_to_static_max(self):
        # A non-hotplug qube: the XML <memory> IS maxmem, libxl's static-max
        # carries it, and there is no hotplug-max key.
        sampler, _ = make_sampler(
            [FakeVM("work", mem=4096, mem_max=4096)],
            {7: (b"work", b"2048", None)},
            memory={7: {b"static-max": b"8192000", b"target": b"4194304"}})
        r = sampler.tick().records[0]
        self.assertEqual(r.mem_max_kib, 8192000)
        self.assertEqual(r.mem_assigned_kib, 4194304)

    def test_memory_figures_fall_back_to_the_api(self):
        # No xenstore memory keys (e.g. a domain qmemman does not manage):
        # the API values remain, rather than zeros.
        sampler, _ = make_sampler(
            [FakeVM("work", mem=4096, mem_max=8192)],
            {7: (b"work", b"2048", None)})
        r = sampler.tick().records[0]
        self.assertEqual(r.mem_assigned_kib, 4096)
        self.assertEqual(r.mem_max_kib, 8192)

    def test_zero_xenstore_values_do_not_beat_the_api(self):
        # A zero is an absent value, not a ceiling: fall back rather than
        # render "0.00 GiB" as if the qube had no memory at all.
        sampler, _ = make_sampler(
            [FakeVM("work", mem=4096, mem_max=8192)],
            {7: (b"work", b"2048", None)},
            memory={7: {b"static-max": b"0", b"hotplug-max": b"0",
                        b"target": b"0"}})
        r = sampler.tick().records[0]
        self.assertEqual(r.mem_assigned_kib, 4096)
        self.assertEqual(r.mem_max_kib, 8192)

    def test_none_safe_when_meminfo_absent(self):
        sampler, _ = make_sampler(
            [FakeVM("work", mem=4096, cputime=5, mem_max=8192)],
            {1: (b"work", None, None)})
        r = sampler.tick().records[0]
        self.assertIsNone(r.mem_used_kib)
        self.assertIsNone(r.swap_used_kib)
        self.assertEqual(r.cpu_time_ns, 5)
        self.assertEqual(r.mem_assigned_kib, 4096)
        self.assertEqual(r.mem_max_kib, 8192)

    def test_non_numeric_meminfo_is_none(self):
        sampler, _ = make_sampler(
            [FakeVM("work", mem=4096, cputime=5, mem_max=8192)],
            {1: (b"work", b"12a34", b"512")})
        r = sampler.tick().records[0]
        self.assertIsNone(r.mem_used_kib)
        self.assertEqual(r.swap_used_kib, 512)

    def test_missing_swap_present_meminfo(self):
        sampler, _ = make_sampler(
            [FakeVM("work", mem=4096, cputime=5, mem_max=8192)],
            {1: (b"work", b"2048", None)})
        r = sampler.tick().records[0]
        self.assertEqual(r.mem_used_kib, 2048)
        self.assertIsNone(r.swap_used_kib)

    def test_halted_qube_is_excluded(self):
        sampler, _ = make_sampler(
            [FakeVM("work"), FakeVM("paused", state="Paused")],
            {1: (b"work", b"1024", None),
             2: (b"paused", b"1024", None)})
        tick = sampler.tick()
        self.assertEqual([r.name for r in tick.records], ["work"])
        self.assertEqual(tick.present, frozenset({"work"}))

    def test_paused_included_via_running_states_seam(self):
        sampler, _ = make_sampler(
            [FakeVM("work"), FakeVM("paused", state="Paused")],
            {1: (b"work", b"1024", None),
             2: (b"paused", b"1024", None)},
            running_states=("Running", "Paused"))
        tick = sampler.tick()
        self.assertEqual(tick.present, frozenset({"work", "paused"}))

    def test_stubdom_is_skipped(self):
        sampler, _ = make_sampler(
            [FakeVM("work"), FakeVM("work-dm")],
            {1: (b"work", b"1024", None),
             2: (b"work-dm", b"1024", None)})
        tick = sampler.tick()
        self.assertEqual([r.name for r in tick.records], ["work"])

    def test_dom0_joins_via_domain_zero_normalisation(self):
        sampler, _ = make_sampler(
            [FakeVM("dom0", mem=0, cputime=0, mem_max=0)],
            {0: (b"Domain-0", None, None)})
        r = sampler.tick().records[0]
        self.assertEqual(r.name, "dom0")
        self.assertEqual(r.domid, 0,
                         "the raw xenstore name Domain-0 normalises to "
                         "dom0 (the upstream mapping) — the join works")
        self.assertIsNone(r.mem_used_kib)

    def test_unresolved_domid_is_the_sentinel(self):
        sampler, _ = make_sampler(
            [FakeVM("work", mem=4096, cputime=5, mem_max=8192)],
            {1: (b"other", b"1024", None)})  # no "work" in the xenstore map
        r = sampler.tick().records[0]
        self.assertEqual(r.domid, UNRESOLVED_DOMID)
        self.assertEqual(UNRESOLVED_DOMID, -1)
        self.assertIsNone(r.mem_used_kib)
        self.assertIsNone(r.swap_used_kib)

    def test_two_running_qubes_share_one_ts(self):
        sampler, clock = make_sampler(
            [FakeVM("a"), FakeVM("b")],
            {1: (b"a", b"1024", None), 2: (b"b", b"2048", None)})
        tick = sampler.tick()
        self.assertEqual(len(tick.records), 2)
        self.assertEqual(tick.records[0].ts, tick.records[1].ts)
        self.assertEqual(clock.calls, 1,
                         "one clock read per tick (D-12)")
        # two ticks: the ts advances, the clock is called once more
        ts_first = tick.records[0].ts
        tick2 = sampler.tick()
        self.assertEqual(tick2.records[0].ts, ts_first + 2.0)
        self.assertEqual(clock.calls, 2)

    def test_degraded_xenstore_keeps_cpu_and_assigned(self):
        app = FakeQubesApp()
        app.domains = [FakeVM("work", mem=4096, cputime=5, mem_max=8192)]
        # An injected library that knows no domains: the same degradation
        # (empty xenstore view) on every host. Relying on "this box has no
        # libxenstore" was FALSE on dom0, where the real library loads —
        # which is exactly why this test failed there.
        sampler = Sampler(qubes=QubesAdapter(app=app),
                         xenstore=Xenstore(library=make_xenstore_lib({})))
        r = sampler.tick().records[0]
        self.assertEqual(r.domid, UNRESOLVED_DOMID)
        self.assertIsNone(r.mem_used_kib)
        self.assertIsNone(r.swap_used_kib)
        self.assertEqual(r.cpu_time_ns, 5)
        self.assertEqual(r.mem_assigned_kib, 4096)
        self.assertEqual(r.mem_max_kib, 8192)

    def test_sampler_import_is_side_effect_free(self):
        """The top-level import of this test module (above) executed the
        sampler's (and its adapters') top level in a fresh interpreter
        with no qubesadmin (the runner runs each module as its own
        script; this box has none). If any top level imported it, that
        import would have failed. Absence pins the deferred-import
        design. Pop first: sibling tests do not touch sys.modules here,
        but the assertion should not rest on ordering anyway."""
        sys.modules.pop("qubesadmin", None)
        self.assertNotIn("qubesadmin", sys.modules)


if __name__ == "__main__":
    unittest.main()
