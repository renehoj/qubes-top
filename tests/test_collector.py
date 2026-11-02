"""collector (card t_7ce21687): the card's acceptance line, off-box.

Fake clock + fake sources (the card's method): a raising source does
not kill the loop; a hung source is timed out; a stopped collector
joins within a bounded time; no busy-spin (the sleep path is asserted
directly — the fake sleep records its calls — not inferred from wall
time); interval drift bounded across 100 ticks; a qube appearing and
disappearing mid-run is handled without leaking ring buffers. Plus the
behaviour bullets: keep-the-last-sample-and-mark-it-stale, the xenstore
degradation (UNKNOWN for every qube, CPU + assigned/max continue, no
silent zero), the qubesadmin-wholesale flag, start/stop idempotence,
restart, and subscriber-error isolation.

Only the *join timeout* and the *hung-source timeout* use real time
(they are ``threading.Event`` waits — the fake clock cannot fake a
thread). The fake sleep paces the loop (1 fake s = 5 ms real) so the
test and the loop stay in lockstep; a ``stop()`` can therefore
overshoot by at most one in-flight cycle, and the assertions are
written for that (content-based waits, count bounds of
``[n, n+1]`` cycles, no exact frame indices).
"""
import pathlib
import sys
import threading
import time
import unittest
from typing import List, Optional, cast

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.core import MemState, SampleModel, SampleRecord  # noqa: E402
from qubes_top.core.collector import Collector, SourceStatus  # noqa: E402


# -- fakes ---------------------------------------------------------------

class FakeClock:
    """A free-running, manually advanced clock (the D-12 contract: one
    clock, monotonic-seconds floats)."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, d):
        self.now += d


class FakeSleep:
    """Records every call (the sleep path is asserted directly, per the
    card), advances the fake clock by the slept duration, and — so the
    fake world does not spin away from the test in real time — paces
    the loop with a real ``time.sleep`` at a fixed scale (1 fake second
    = 5 ms real). Cadence/drift semantics stay on the fake timeline;
    only the real-time lockstep is added."""

    SCALE = 0.005  # 1 fake second = 5 ms real

    def __init__(self, clock):
        self.clock = clock
        self.durations: List[float] = []

    def __call__(self, d):
        assert d > 0, "a sleep of <= 0 is a busy-spin"
        self.durations.append(d)
        self.clock.advance(d)
        time.sleep(d * self.SCALE)


class TickResult:
    """A plain stand-in for the dom0 ``Sampler.Tick`` (the TickLike
    structural contract: ``.records`` + ``.present``)."""

    def __init__(self, records, present):
        self.records = records
        self.present = frozenset(present)


class FakeSource:
    """A fake ``tick_fn`` with switchable per-call behaviour (ok /
    raise / hang). The CPU counter advances 1 s of CPU per tick (50% of
    one core at the 2 s cadence), so the dense CPU series has a
    non-gap value from the second tick."""

    def __init__(self, clock, call_cost=0.01):
        self.clock = clock
        self.call_cost = call_cost
        self.present: List[str] = []
        self.used = {}  # name -> used KiB or None (None = no meminfo)
        self.error: Optional[BaseException] = None
        self.hang: Optional[threading.Event] = None
        self.cpu_ns = {}
        self.calls = 0

    def tick(self):
        self.clock.advance(self.call_cost)
        self.calls += 1
        if self.hang is not None:
            self.hang.wait()
            self.hang = None
        if self.error is not None:
            raise self.error
        records = []
        for name in self.present:
            self.cpu_ns[name] = self.cpu_ns.get(name, 0) + 10 ** 9
            records.append(SampleRecord(
                name=name,
                domid=1,
                state="Running",
                cpu_time_ns=self.cpu_ns[name],
                mem_assigned_kib=1024,
                mem_max_kib=2048,
                mem_used_kib=self.used.get(name),
                swap_used_kib=None,
                ts=self.clock.now,
            ))
        return TickResult(records, self.present)


def _default_factory(target):
    return threading.Thread(target=target, daemon=True,
                           name="qubes-top-test-worker")


def make_collector(source, model, clock, sleep, interval=2.0,
                   call_timeout=1.0, source_status=None,
                   thread_factory=None):
    return Collector(source.tick, model, interval=interval,
                    call_timeout=call_timeout,
                    source_status=source_status,
                    clock=clock, sleep=sleep,
                    thread_factory=thread_factory or _default_factory)


def wait_for(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def wait_for_new(frames, after, pred, timeout=10.0):
    """The first frame at index >= ``after`` satisfying ``pred`` (or
    None). Content-based, overshoot-safe: it never re-matches a frame
    from before the state change that introduced it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for i, f in enumerate(frames):
            if i >= after and pred(f):
                return f
        time.sleep(0.005)
    return None


def idx_of(frames, frame):
    return frames.index(frame) + 1


class CollectorTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.sleep = FakeSleep(self.clock)
        self.model = SampleModel(ring_capacity=16)
        self.source = FakeSource(self.clock)
        self.frames: List = []
        self.collector = make_collector(
            self.source, self.model, self.clock, self.sleep)
        self.collector.subscribe(self.frames.append)


# -- acceptance: a raising source does not kill the loop -------------------

class TestRaisingSource(CollectorTestCase):
    def test_raising_source_does_not_kill_loop(self):
        self.source.present = ["webapp"]
        self.source.used["webapp"] = 512  # != 1024 assigned: REPORTED
        self.collector.start()
        self.assertTrue(wait_for(lambda: any(f.ok for f in self.frames)))
        # qubesadmin raises wholesale (or one qube's metric read): the
        # previous frame is kept and flagged.
        self.source.error = RuntimeError("mgmt api down")
        self.assertTrue(wait_for(
            lambda: sum(1 for f in self.frames if not f.ok) >= 2,
            timeout=15.0))
        bad = next(f for f in self.frames if not f.ok)
        self.assertIn("mgmt api down", bad.reason)
        self.assertIn("webapp", bad.qubes)  # the last sample is kept
        self.assertTrue(bad.qubes["webapp"].stale)  # ...and marked stale
        self.assertEqual(bad.qubes["webapp"].record.mem_assigned_kib, 1024)
        # (two failures published above: the loop is alive through them)
        # recovery: the error clears, fresh frames resume
        self.source.error = None
        ok_before = sum(1 for f in self.frames if f.ok)
        self.assertTrue(wait_for(
            lambda: sum(1 for f in self.frames if f.ok) > ok_before,
            timeout=15.0))
        self.assertTrue(self.frames[-1].ok)
        self.collector.stop()

    def test_first_failure_is_an_empty_flagged_frame(self):
        # never a silent empty: the flag is what the UI shows
        self.source.error = RuntimeError("mgmt api down")
        self.collector.start()
        self.assertTrue(wait_for(lambda: len(self.frames) >= 1))
        self.assertFalse(self.frames[0].ok)
        self.assertEqual(self.frames[0].qubes, {})
        self.assertEqual(self.frames[0].present, frozenset())
        self.assertIn("mgmt api down", self.frames[0].reason)
        self.collector.stop()

    def test_stale_tick_leaves_the_model_untouched(self):
        self.source.present = ["webapp"]
        self.source.used["webapp"] = 512
        self.collector.start()
        self.assertTrue(wait_for(lambda: any(f.ok for f in self.frames)))
        self.source.error = RuntimeError("down")
        self.assertTrue(wait_for(lambda: any(not f.ok for f in self.frames)))
        # after the observed failure no ok tick is in flight (ingest
        # precedes publish; the error was set before it), so the model
        # count is stable
        stable = len(self.model.cpu_series("webapp"))
        self.assertGreaterEqual(stable, 1)
        # one more failed tick: the model must not change
        self.assertTrue(wait_for(
            lambda: sum(1 for f in self.frames if not f.ok) >= 2))
        self.assertEqual(len(self.model.cpu_series("webapp")), stable,
                        "a failed tick must not ingest or synchronise")
        self.collector.stop()


# -- acceptance: a hung source is timed out -------------------------------

class TestHungSource(CollectorTestCase):
    def test_hung_source_is_timed_out_and_loop_survives(self):
        source = self.source
        source.present = ["webapp"]
        source.used["webapp"] = 512
        self.collector = make_collector(source, self.model, self.clock,
                                       self.sleep, call_timeout=0.15)
        self.collector.subscribe(self.frames.append)
        self.collector.start()
        self.assertTrue(wait_for(lambda: any(f.ok for f in self.frames)))
        # the source hangs: the per-call timeout must fire
        hang = threading.Event()
        source.hang = hang
        self.assertTrue(wait_for(
            lambda: any(not f.ok and "timed out" in (f.reason or "")
                       for f in self.frames),
            timeout=15.0))
        # the cycle does not stall: the next cadence tick publishes too —
        # and starts NO new call while one is outstanding
        # (one reused libxenstore handle, D-11)
        calls_at_timeout = source.calls
        self.assertTrue(wait_for(
            lambda: any(not f.ok
                       and "previous call still outstanding" in (f.reason or "")
                       for f in self.frames),
            timeout=15.0))
        self.assertEqual(source.calls, calls_at_timeout,
                         "a busy tick must not start a new call")
        # release the hung call: its late result must be discarded, and
        # the next tick is exactly one fresh call
        i = len(self.frames)
        hang.set()
        f = wait_for_new(self.frames, i, lambda f: f.ok, timeout=15.0)
        self.assertIsNotNone(f)
        self.assertEqual(source.calls, calls_at_timeout + 1,
                         "exactly one fresh call after the release — the "
                         "abandoned call's late result is discarded")
        self.collector.stop()

    def test_stop_during_hang_joins_bounded(self):
        source = self.source
        source.present = ["webapp"]
        self.collector = make_collector(source, self.model, self.clock,
                                       self.sleep, call_timeout=0.15)
        self.collector.subscribe(self.frames.append)
        self.collector.start()
        self.assertTrue(wait_for(lambda: any(f.ok for f in self.frames)))
        hang = threading.Event()
        source.hang = hang
        self.assertTrue(wait_for(
            lambda: any(not f.ok and "timed out" in (f.reason or "")
                       for f in self.frames),
            timeout=15.0))
        t0 = time.monotonic()
        self.collector.stop()
        self.assertLess(time.monotonic() - t0, 1.0,
                        "stop() must return within a bounded time even "
                        "with a hung source outstanding")
        self.assertFalse(self.collector.running)
        hang.set()  # release the abandoned thread (cleanup)


# -- acceptance: a stopped collector joins within a bounded time ----------

class TestStop(CollectorTestCase):
    def test_stop_before_start_is_a_noop(self):
        t0 = time.monotonic()
        self.collector.stop()
        self.assertLess(time.monotonic() - t0, 0.1)
        self.assertFalse(self.collector.running)

    def test_stop_joins_bounded(self):
        self.source.present = ["webapp"]
        self.collector.start()
        self.assertTrue(wait_for(lambda: any(f.ok for f in self.frames)))
        t0 = time.monotonic()
        self.collector.stop()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertFalse(self.collector.running)

    def test_stop_is_idempotent(self):
        self.source.present = ["webapp"]
        self.collector.start()
        self.assertTrue(wait_for(lambda: any(f.ok for f in self.frames)))
        self.collector.stop()
        t0 = time.monotonic()
        self.collector.stop()
        self.assertLess(time.monotonic() - t0, 0.1)

    def test_start_is_idempotent(self):
        # the thread_factory also spawns the per-call probe thread
        # (handle safety), so counting factory calls would overcount:
        # the direct assertion is that a second start() does not
        # replace the worker (it would, if it spawned a second one)
        self.source.present = ["webapp"]
        self.collector.subscribe(self.frames.append)
        self.collector.start()
        first = self.collector._worker  # white-box: the worker handle
        self.assertIsNotNone(first)
        self.collector.start()  # a second call while running: no 2nd worker
        self.assertIs(self.collector._worker, first,
                      "a second start() must not spawn a second worker")
        self.assertTrue(self.collector.running)
        self.assertTrue(wait_for(lambda: any(f.ok for f in self.frames)))
        self.collector.stop()

    def test_restart_after_stop_keeps_the_model(self):
        self.source.present = ["webapp"]
        self.source.used["webapp"] = 512

        def fresh_count():
            return sum(1 for f in self.frames if f.ok)

        self.collector.start()
        self.assertTrue(wait_for(lambda: fresh_count() >= 2))
        self.collector.stop()  # joins: the count is frozen on return
        n1 = fresh_count()
        self.assertGreaterEqual(n1, 2)
        self.collector.start()
        self.assertTrue(wait_for(lambda: fresh_count() >= n1 + 2))
        self.collector.stop()
        n2 = fresh_count()
        self.assertGreaterEqual(n2 - n1, 2)
        self.assertLessEqual(n2 - n1, 3,
                             "at most one in-flight cycle overshoots stop")
        # the model (external) is untouched by stop: the ring persists
        self.assertEqual(len(self.model.cpu_series("webapp")), n2)
        self.collector.stop()  # idempotent


# -- acceptance: no busy-spin (assert the sleep path) ---------------------

class TestSleepPath(CollectorTestCase):
    def test_sleep_path_is_used_and_bounded(self):
        self.source.present = ["webapp"]
        self.collector.start()
        self.assertTrue(wait_for(
            lambda: sum(1 for f in self.frames if f.ok) >= 5))
        self.collector.stop()
        self.assertTrue(self.sleep.durations,
                        "the loop never called sleep — busy-spin")
        self.assertTrue(all(d > 0 for d in self.sleep.durations))
        self.assertLessEqual(max(self.sleep.durations), 0.05 + 1e-9,
                             "the sleep is chunked against the stop flag")
        # 5 ok frames => 4 complete cycles; the in-flight cycle adds at
        # most one more. (Per complete cycle the sleep is 1.99: the
        # 0.01 call cost is subtracted from the interval.)
        total = sum(self.sleep.durations)
        self.assertGreaterEqual(total, 4 * 1.99 - 1e-9)
        self.assertLessEqual(total, 6 * 1.99 + 1e-9)
        self.assertGreaterEqual(len(self.sleep.durations), 5,
                                "at least one sleep per published tick")

    def test_a_call_longer_than_the_interval_skips_the_sleep(self):
        # catch up, never burst: a 3 s call at a 2 s interval publishes
        # late frames at the call's cadence and sleeps nothing. (This
        # test is deliberately unpaced: skipping the sleep means there
        # is no sleep to pace — the loop spins between the wait and the
        # bounded stop, which is exactly what is being measured.)
        slow = FakeSource(self.clock, call_cost=3.0)
        slow.present = ["webapp"]
        slow_frames = []
        self.collector = make_collector(slow, self.model, self.clock,
                                       self.sleep)
        self.collector.subscribe(slow_frames.append)
        self.collector.start()
        self.assertTrue(wait_for(lambda: len(slow_frames) >= 3))
        self.collector.stop()
        self.assertEqual(len(self.sleep.durations), 0,
                         "a late tick must not sleep (no negative sleep)")
        # the first N frames span (N-1) * 3.0 on the fake timeline
        n = len(slow_frames)
        span = slow_frames[-1].ts - slow_frames[0].ts
        self.assertGreaterEqual(span, (n - 1) * 3.0 - 1e-9,
                                "cadence degrades to the call cost, it "
                                "does not burst back to the interval")
        self.assertLessEqual(span, (n - 1) * 3.0 + 1e-9)


# -- acceptance: interval drift bounded across 100 ticks ------------------

class TestDrift(CollectorTestCase):
    def test_drift_bounded_over_100_ticks(self):
        self.source.present = ["webapp"]
        self.collector.start()
        self.assertTrue(wait_for(
            lambda: sum(1 for f in self.frames if f.ok) >= 100,
            timeout=60.0))
        self.collector.stop()
        ok_frames = [f for f in self.frames if f.ok][:100]
        self.assertGreaterEqual(len(ok_frames), 100)
        span = ok_frames[-1].ts - ok_frames[0].ts
        # the fake timeline: each cycle is exactly 2.0 (0.01 call cost +
        # 1.99 sleep), so 100 ticks span 99 * 2.0 — no accumulated
        # drift. The bound asserted is the card's: bounded, not exact.
        self.assertLess(abs(span - 99 * 2.0), 0.05,
                        "drift over 100 ticks must be bounded")
        # and the sleep path carried it: >= one sleep per completed tick
        self.assertGreaterEqual(len(self.sleep.durations), 99)
        total = sum(self.sleep.durations)
        self.assertGreaterEqual(total, 99 * 1.99 - 1e-6)
        self.assertLessEqual(total, 101 * 1.99 + 1e-6,
                             "at most one in-flight cycle overshoots")


# -- qubes appearing / disappearing mid-run, no ring-buffer leak ---------

class TestQubeLifecycle(CollectorTestCase):
    def test_qube_coming_and_going_does_not_leak_rings(self):
        source = self.source
        model = self.model
        self.collector.start()

        # tick 1: webapp appears
        source.present = ["webapp"]
        source.used["webapp"] = 512
        f1 = wait_for_new(self.frames, 0,
                          lambda f: f.ok and f.present == frozenset({"webapp"}))
        self.assertIsNotNone(f1)
        self.assertTrue(model.has("webapp"))

        # tick 2: appvmm appears
        source.present = ["webapp", "appvmm"]
        source.used["appvmm"] = 100 * 1024  # != 1024 assigned: REPORTED
        f2 = wait_for_new(self.frames, idx_of(self.frames, f1),
                          lambda f: f.ok and f.present ==
                          frozenset({"webapp", "appvmm"}))
        self.assertIsNotNone(f2)
        self.assertTrue(model.has("appvmm"))

        # tick 3: the source raises (a failed tick) — appvmm vanishes in
        # the *next* successful tick, but its buffer must persist across
        # the failed one (no sync without a fresh present set)
        source.error = RuntimeError("outage")
        f3 = wait_for_new(self.frames, idx_of(self.frames, f2),
                          lambda f: not f.ok)
        assert f3 is not None
        self.assertIn("appvmm", f3.qubes,
                      "a stale frame keeps the previous frame's samples")
        self.assertTrue(model.has("appvmm"),
                        "a failed tick must not synchronise")

        # tick 4: the outage ends; appvmm is gone
        source.error = None
        source.present = ["webapp"]
        f4 = wait_for_new(self.frames, idx_of(self.frames, f3),
                          lambda f: f.ok and f.present == frozenset({"webapp"}))
        self.assertIsNotNone(f4)
        self.assertFalse(model.has("appvmm"),
                         "a vanished qube's buffers are dropped at the "
                         "next fresh tick")

        # tick 5: sysd appears; the survivor's buffer is intact
        source.present = ["webapp", "sysd"]
        source.used["sysd"] = 200 * 1024
        f5 = wait_for_new(self.frames, idx_of(self.frames, f4),
                          lambda f: f.ok and f.present ==
                          frozenset({"webapp", "sysd"}))
        self.assertIsNotNone(f5)
        self.assertTrue(model.has("sysd"))
        self.assertTrue(model.has("webapp"))
        self.assertEqual(len(model._qubes), 2,
                         "no ring-buffer leak: the registry holds exactly "
                         "the live qubes (white-box: the model's "
                         "per-qube registry)")
        # the survivor's series: a point per fresh tick it was present
        # (>= 4; t3 was a failed tick, not ingested)
        webapp_cpu = model.cpu_series("webapp")
        self.assertGreaterEqual(len(webapp_cpu), 4)
        self.assertIsNone(webapp_cpu[0].pct)  # first sample: no baseline
        # float tolerance: the fake clock's + chain accumulates float
        # error, so "1 s CPU / 2 s" is nominal, not exact
        pct = webapp_cpu[1].pct
        self.assertIsNotNone(pct)
        self.assertAlmostEqual(cast(float, pct), 50.0, places=9)
        self.assertFalse(model.has("appvmm"))
        self.collector.stop()


# -- degradation: xenstore unavailable -------------------------------------

class TestDegradation(CollectorTestCase):
    def test_xenstore_down_marks_every_qube_unknown(self):
        source = self.source
        source.present = ["webapp"]
        source.used["webapp"] = None  # read_all() -> {} : no meminfo
        status = {"ok": False}
        self.collector = make_collector(
            source, self.model, self.clock, self.sleep,
            source_status=lambda: SourceStatus(status["ok"]))
        self.collector.subscribe(self.frames.append)
        self.collector.start()
        self.assertTrue(wait_for(
            lambda: sum(1 for f in self.frames if f.ok) >= 2))
        f = next(f for f in self.frames if f.ok)
        self.assertFalse(f.xenstore_ok)
        sample = f.qubes["webapp"]
        self.assertIs(sample.mem_state, MemState.UNKNOWN,
                      "a dead channel is UNKNOWN for every qube — never "
                      "the per-qube MUTE (no observation was made at all)")
        # CPU + assigned/max continue (the card's degradation rule)
        self.assertIsNone(sample.record.mem_used_kib)  # no silent zero
        self.assertEqual(sample.record.mem_assigned_kib, 1024)
        self.assertEqual(sample.record.mem_max_kib, 2048)
        cpu = self.model.cpu_series("webapp")
        self.assertGreaterEqual(len(cpu), 2,
                                "the CPU series continues while "
                                "xenstore is down")
        self.assertIsNotNone(cpu[-1].pct)
        self.assertEqual(self.model.mem_series("webapp"), [],
                         "no mem point is synthesised (no silent zero)")
        # the frozen per-qube machine is untouched: a raw None
        # observation with a working channel is MUTE
        self.assertIs(self.model.mem_state("webapp"), MemState.MUTE)
        self.collector.stop()

    def test_channel_recovery_resumes_per_qube_state(self):
        source = self.source
        source.present = ["webapp"]
        source.used["webapp"] = None
        status = {"ok": False}
        self.collector = make_collector(
            source, self.model, self.clock, self.sleep,
            source_status=lambda: SourceStatus(status["ok"]))
        self.collector.subscribe(self.frames.append)
        self.collector.start()
        self.assertTrue(wait_for(
            lambda: any(f.ok and "webapp" in f.qubes
                       and f.qubes["webapp"].mem_state is MemState.UNKNOWN
                       for f in self.frames)))
        status["ok"] = True
        source.used["webapp"] = 512  # != 1024 assigned: REPORTED
        self.assertTrue(wait_for(
            lambda: any(f.ok and "webapp" in f.qubes
                       and f.qubes["webapp"].mem_state is MemState.REPORTED
                       for f in self.frames),
            timeout=15.0))
        self.collector.stop()

    def test_a_raising_probe_reads_as_not_ok(self):
        source = self.source
        source.present = ["webapp"]
        source.used["webapp"] = 512

        def broken():
            raise RuntimeError("probe broken")

        self.collector = make_collector(
            source, self.model, self.clock, self.sleep,
            source_status=broken)
        self.collector.subscribe(self.frames.append)
        self.collector.start()
        self.assertTrue(wait_for(lambda: len(self.frames) >= 1))
        self.assertFalse(self.frames[0].xenstore_ok,
                         "a broken probe must not masquerade as a "
                         "healthy channel")
        self.assertIs(self.frames[0].qubes["webapp"].mem_state,
                      MemState.UNKNOWN)
        self.collector.stop()


# -- subscriber-error isolation -------------------------------------------

class TestSubscriberErrors(CollectorTestCase):
    def test_a_raising_subscriber_does_not_kill_the_loop(self):
        self.source.present = ["webapp"]
        good: List = []

        def bad(frame):
            raise RuntimeError("ui handler broke")

        self.collector.subscribe(bad)
        self.collector.subscribe(good.append)
        self.collector.start()
        self.assertTrue(wait_for(lambda: len(good) >= 2))
        self.assertTrue(self.collector.subscriber_errors)
        self.assertIn("ui handler broke", self.collector.subscriber_errors[0])
        self.collector.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
