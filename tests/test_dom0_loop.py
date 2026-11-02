"""loop wiring (card t_7ce21687): ``build_collector`` assembles the
**real** ``Sampler`` + the **real** ``Collector`` with the dom0
sources, every collaborator injectable. The fakes mirror the real
shapes (same as ``test_dom0_sampler``): a ``FakeVM`` with the read-set
surface, a zero-arg app whose ``.domains`` the test populates, and the
**real**-signature libxenstore stand-in (no status codes — the real
library has none).

What this proves off-box:

- the wiring end-to-end: real sampler + real collector + injected
  fakes, two live ticks, one reused xenstore handle, the name ->
  domid re-resolution (D-5), the stubdom skip, the dom0/``Domain-0``
  join, the channel probe bound to ``Xenstore.available``;
- construction with the **default real adapters** is safe off-box
  (lazy imports), and the first tick degrades cleanly — qubesadmin
  raises wholesale, the frame is flagged (never a crash, never a
  silent empty);
- the channel probe reflects ``Xenstore.available`` (absent library
  -> not-ok -> every qube ``UNKNOWN`` per the collector's
  degradation rule).
"""
import ctypes
import errno
import pathlib
import sys
import threading
import time
import unittest
from typing import Callable, cast

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.core import MemState  # noqa: E402
from qubes_top.core.collector import SourceStatus  # noqa: E402
from qubes_top.dom0.loop import build_collector  # noqa: E402
from qubes_top.dom0.qubesadmin_adapter import QubesAdapter  # noqa: E402
from qubes_top.dom0.xenstore_adapter import Xenstore  # noqa: E402


class FakeVM:
    """The QubesVM read-set surface (same shape as
    test_dom0_sampler.FakeVM)."""

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
    """A zero-arg app whose ``.domains`` the test populates (mirrors
    the real app: zero-arg construction, a ``.domains`` collection)."""

    def __init__(self):
        self.domains = []


def make_xenstore_lib(domains=(), directories=()):
    """A **real**-signature libxenstore fake with an in-memory key tree
    (same shape as test_dom0_sampler.make_xenstore_lib): xs_open(flags),
    xs_read(h, t, path, len*) -> bytes|None, xs_directory(h, t, path,
    num*) -> bytes-list|None, errno set on failure."""
    keys = {}
    for domid, (name, meminfo, swapinfo) in domains.items():
        if name is not None:
            keys["/local/domain/%d/name" % domid] = name
        if meminfo is not None:
            keys["/local/domain/%d/memory/meminfo" % domid] = meminfo
        if swapinfo is not None:
            keys["/local/domain/%d/memory/swapinfo" % domid] = swapinfo
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
            entries = sorted({k[len(prefix):].split("/", 1)[0]
                             for k in keys if k.startswith(prefix)})
            num.value = len(entries)
            return [e.encode("ascii") for e in entries]

    return _Lib()


def wait_for(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def make_wired(vms, domains, interval=0.1, call_timeout=0.05):
    app = FakeQubesApp()
    app.domains = list(vms)
    xenstore = Xenstore(library=make_xenstore_lib(domains))
    collector = build_collector(
        qubes=QubesAdapter(app=app),
        xenstore=xenstore,
        interval=interval,
        call_timeout=call_timeout,
    )
    return collector, xenstore


class TestWiring(unittest.TestCase):
    def test_end_to_end_two_ticks_with_injected_fakes(self):
        # dom 1: a running qube; dom 2: a stubdom (skipped by the
        # adapter); the raw name "Domain-0" exercises the dom0 join.
        app_vm = FakeVM("webapp", state="Running", mem=1024,
                        cputime=5 * 10 ** 9, mem_max=2048)
        stub = FakeVM("webapp-dm", state="Running", mem=1024)
        collector, xenstore = make_wired(
            [app_vm, stub],
            domains={1: (b"webapp", b"524288", b"0"),
                     2: (b"webapp-dm", b"1024", None),
                     0: (b"Domain-0", b"1048576", None)})
        # the one-shot lazy handle open (D-11: open once, reuse)
        # triggers on the first read — trigger it here so the
        # availability assert is deterministic
        xenstore.read_all()
        self.assertTrue(xenstore.available)  # the fake library loads
        frames = []
        collector.subscribe(frames.append)
        collector.start()
        self.assertTrue(wait_for(lambda: len(frames) >= 2))
        collector.stop()
        for frame in frames[:2]:
            self.assertTrue(frame.ok)
            self.assertTrue(frame.xenstore_ok)
            self.assertEqual(frame.present, frozenset({"webapp"}),
                             "the stubdom is skipped (adapter filter) and "
                             "dom0 has no qubesadmin domain (no row)")
            sample = frame.qubes["webapp"]
            self.assertEqual(sample.record.domid, 1,
                             "name -> domid re-resolved from this tick's "
                             "xenstore map (D-5)")
            self.assertEqual(sample.record.cpu_time_ns, 5 * 10 ** 9)
            self.assertEqual(sample.record.mem_assigned_kib, 1024,
                             "assigned from qubesadmin get_mem() (D-2)")
            self.assertEqual(sample.record.mem_max_kib, 2048)
            self.assertEqual(sample.record.mem_used_kib, 524288,
                             "used from the guest-authored meminfo")
            self.assertEqual(sample.record.swap_used_kib, 0)
            self.assertIs(sample.mem_state, MemState.REPORTED)
            self.assertFalse(sample.stale)

    def test_channel_probe_reflects_xenstore_availability(self):
        lib = make_xenstore_lib({1: (b"webapp", b"512", None)})
        xenstore = Xenstore(library=lib)
        collector = build_collector(
            xenstore=xenstore,
            qubes=QubesAdapter(app=FakeQubesApp()),
            interval=0.1, call_timeout=0.05)
        xenstore.read_all()  # trigger the one-shot lazy handle open
        # white-box: the probe is bound to Xenstore.available
        probe = cast("Callable[[], SourceStatus]", collector._source_status)
        self.assertTrue(probe().xenstore_ok)
        xenstore.close()  # handle not open: available -> False
        self.assertFalse(xenstore.available)
        self.assertFalse(probe().xenstore_ok)

    def test_default_real_adapters_construct_safely_offbox(self):
        # no gi, no qubesadmin, no libxenstore on this box: the factory
        # must not raise at construction (lazy imports), and the first
        # tick must degrade cleanly — qubesadmin raises wholesale, the
        # frame is flagged, no crash, no silent empty.
        collector = build_collector()
        self.assertFalse(collector.model is None)
        frames = []
        collector.subscribe(frames.append)
        collector.start()
        self.assertTrue(wait_for(lambda: len(frames) >= 1))
        collector.stop()
        first = frames[0]
        self.assertFalse(first.ok, "off-box the first tick must be flagged")
        self.assertEqual(first.qubes, {})  # flagged, not silently empty
        self.assertIsNotNone(first.reason)
        self.assertIn("QubesAdminUnavailable", first.reason)

    def test_stopped_wired_collector_joins_bounded(self):
        app_vm = FakeVM("webapp", state="Running")
        collector, _ = make_wired([app_vm], {})
        collector.start()
        self.assertTrue(wait_for(lambda: collector.running))
        t0 = time.monotonic()
        collector.stop()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertFalse(collector.running)


class _LeakGuard(unittest.TestCase):
    """Sanity: the test process itself must not be left with non-daemon
    test workers (the default factory is daemon; this guards against a
    regression that would hang process exit)."""

    def test_no_non_daemon_threads_left(self):
        time.sleep(0.05)  # let any straggler thread start
        bad = [t for t in threading.enumerate()
               if not t.daemon and t.name != "MainThread"]
        self.assertEqual(bad, [],
                         "test workers must be daemon threads: %r"
                         % [t.name for t in bad])


if __name__ == "__main__":
    unittest.main(verbosity=2)
