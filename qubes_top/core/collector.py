"""qubes-top core: the sampling loop (card t_7ce21687).

Pure standard library (``threading``, ``time``, ``dataclasses``,
``typing``). No ``gi``, no ``qubesadmin``, no network — importable and
testable off-box like the rest of ``qubes_top.core`` (AST guard,
``tests/test_import_guard.py``). The dom0 wiring
(``qubes_top.dom0.loop``) and the UI (``qubes_top.ui``) code against the
API below; the UI subscribes with a callback that hands frames to the
GLib main loop (``GLib.idle_add``) — the worker thread never touches
a widget, which is the subscriber's contract, not the collector's.

Frozen API
----------
``SourceStatus(xenstore_ok)`` -- the per-tick channel probe result
``QubeSample(record, mem_state, stale, reason)`` -- one qube in a frame
``Frame(ts, qubes, present, ok, reason, xenstore_ok)`` -- one published
   tick; ``qubes`` is a mapping of ``QubeSample`` keyed by qube name
   (the UI queries by name, mirroring ``SampleModel``)
``Collector(tick_fn, model, *, interval=2.0, call_timeout=1.0,
            source_status=None, clock=time.monotonic, sleep=time.sleep,
            thread_factory=...)``:
    ``start()``        -- idempotent: spawns one worker thread; a call
                         while running is a no-op (never two workers);
                         a call after ``stop()`` restarts
    ``stop()``         -- idempotent: sets the stop flag, joins the
                         worker within a bounded time (the bound below),
                         and returns even if the join times out (the
                         worker is a daemon thread, so a stuck worker
                         cannot block window close or process exit);
                         ``stop()`` before ``start()`` is a no-op
    ``subscribe(cb)``  -- ``cb(frame)`` is invoked on the worker thread
                         for every published frame; a raising callback
                         is recorded and skipped, never propagated
    ``running``        -- flag-based (``start()`` until a clean
                         ``stop()``), not ``is_alive()``-based: a
                         freshly started thread is not "alive" for an
                         OS-slice, and ``is_alive()`` in that window
                         would let a second ``start()`` spawn a second
                         worker on one model
    ``model``          -- the ``SampleModel`` this collector folds into
    ``subscriber_errors`` -- recorded subscriber failures (diagnostics)

``DEFAULT_INTERVAL``   -- 2.0 s (D-10 cadence; a parameter, not a
                         redesign — the owner's call changes it via the
                         ``interval`` argument)
``DEFAULT_CALL_TIMEOUT`` -- 1.0 s (validated ``< interval``, D-13: one
                         hung call may delay at most one tick, never
                         swallow the cadence)

Frozen semantics (recorded here; DESIGN.md is the architect's document)
---------------------------------------------------------------------
- **Cadence, drift-bounded:** each cycle is ``t0 -> source call ->
  publish -> sleep(interval - elapsed)``. The sleep subtracts the
  measured call cost, so the wall cadence is locked to ``interval`` and
  a slow call cannot accumulate drift across ticks. A call that runs
  longer than the interval publishes a late frame and skips the sleep —
  catch up, never burst. The sleep is chunked (``min(remaining,
  0.05 s)``) against the stop flag, so ``stop()`` is noticed within one
  chunk.
- **One source call at a time, per-call timeout:** the ``tick_fn`` (the
  dom0 ``Sampler.tick``) runs on a short-lived child thread; the worker
  joins it with ``call_timeout``. A **raising** source fails the tick
  instantly. A **hung** source times out: the frame is published stale,
  and the abandoned call thread is *kept tracked* — no new call starts
  while one is outstanding (the dom0 adapters reuse one libxenstore
  handle, D-11; two threads inside the same C handle is a hazard the
  design never allowed). While the outstanding call is running, the
  loop publishes flagged stale frames at full cadence (the cycle never
  stalls, the loop never dies); the late result, when it finally
  returns, is **discarded** — a 30 s-old reading is never folded into
  the model as fresh. The abandoned thread is a daemon and is never
  joined by ``stop()``.
- **Failure isolation / degradation** (the frame's ``ok`` flag is the
  "flag the UI" signal):
  - a tick that raises (qubesadmin wholesale — ``QubesAdminUnavailable``,
    a wedged mgmt API, a persistent xenstore read error — or any single
    qube's metric read, which the frozen ``Sampler`` does not isolate):
    the **previous frame's samples are kept and marked stale**
    (``ok=False`` + ``reason``); nothing is ingested, nothing is
    synchronised, the model is untouched. A failure with no previous
    frame publishes an empty, flagged frame — never a silent empty.
  - **xenstore unavailable** (the channel probe says not-ok — e.g. the
    library is absent, ``read_all()`` degrades to ``{}``): the tick
    still succeeds and is ingested normally — the CPU series continues
    (dense points; meminfo is guest-side, qubesadmin is dom0-side), the
    mem series freezes (a ``None`` observation emits no point and moves
    no deadband reference — the frozen model semantics), and **every
    qube's effective ``mem_state`` is ``UNKNOWN``** for that frame. This
    is deliberately distinct from the per-qube frozen state machine:
    ``used is None`` with a *working* channel is ``MUTE`` (the writer
    is dead — a per-qube observation), while a *dead channel* is
    ``UNKNOWN`` (no observation was made at all). A 0 used-value is
    never synthesised (D-4: "no silent zero"). The model's own
    ``mem_state`` is not overridden — the frame carries the *effective*
    display state; the frozen machine stays total over raw
    observations.
  - **qubesadmin raises wholesale:** the raise path above — the previous
    frame is kept, ``ok=False``, ``reason`` carries the exception; the
    CPU/assigned/max data of the last good frame is what the UI shows,
    flagged.
- **Qubes appearing/disappearing mid-run:** on a *fresh* tick the
  model is ingested, then ``synchronize(present)`` drops the buffers of
  every qube no longer present (the frozen ``SampleModel`` semantics —
  no ring-buffer leak; a reappearance starts clean, so a restarted qube
  cannot attach a stale CPU baseline). A *stale* tick does NOT
  synchronize (``present`` is unknown for that tick): buffers persist
  across the failed tick and the next fresh tick reconciles — a qube
  that vanished during an outage is dropped at most one cadence late.
- **Restart:** ``start()`` after ``stop()`` spawns a new worker; the
  model (an external object) is untouched, so rings persist across
  restarts; an outstanding call from the previous run still suppresses
  new calls (handle safety).
- **``stop()`` join bound:** ``call_timeout + 2 * 0.05 s + 0.25 s`` —
  once the stop flag is set, the worker exits within at most one join
  timeout + one sleep chunk + the publish. A hung *subscriber* callback
  is the one unbounded case (documented; the daemon fallback applies).

The ``tick_fn`` contract (structurally ``TickLike``; the dom0
``Sampler.tick`` satisfies it): returns an object with ``.records`` (an
iterable of ``SampleRecord``) and ``.present`` (an iterable of qube
names — the ``SampleModel.synchronize`` input). The ``source_status``
contract: a zero-arg callable returning ``SourceStatus`` (the dom0
wiring probes ``Xenstore.available``); a raising probe reads as not-ok
(a broken probe must not masquerade as a healthy channel); ``None``
(default) means "no channel knowledge — assume ok".
"""
import threading
import time
from dataclasses import dataclass
from typing import (Callable, Collection, Dict, FrozenSet, List, Optional,
                   Protocol, Sequence, cast)

from qubes_top.core import MemState, SampleModel, SampleRecord

__all__ = [
    "DEFAULT_INTERVAL",
    "DEFAULT_CALL_TIMEOUT",
    "SourceStatus",
    "QubeSample",
    "Frame",
    "Collector",
    "TickLike",
]

#: D-10 cadence: 2 s. Configurable via ``Collector(interval=...)`` —
#: a parameter, not a redesign.
DEFAULT_INTERVAL = 2.0

#: One hung call may delay at most one tick. Validated ``< interval``
#: (D-13): if the timeout could swallow the whole cadence, a hung
#: source would stall the cycle, which the card forbids.
DEFAULT_CALL_TIMEOUT = 1.0

#: Sleep chunk: the stop flag is re-checked every chunk, so ``stop()``
#: is noticed within 0.05 s of the flag being set.
_SLEEP_CHUNK = 0.05


@dataclass(frozen=True)
class SourceStatus:
    """One channel probe. ``xenstore_ok`` is False when the xenstore
    channel is unavailable (library absent / handle not open) — the
    degradation case the card calls out explicitly."""
    xenstore_ok: bool


@dataclass(frozen=True)
class QubeSample:
    """One qube in one frame. ``record`` is the fresh sample on an
    ``ok`` frame and the **kept last sample** on a stale frame
    (``stale=True`` + ``reason`` — the card's "keep the last sample
    and mark it stale"). ``mem_state`` is the *effective* display state
    (``UNKNOWN`` for every qube while the xenstore channel is down;
    otherwise the model's per-qube state — see the module docstring)."""
    record: SampleRecord
    mem_state: MemState
    stale: bool
    reason: Optional[str]


@dataclass(frozen=True)
class Frame:
    """One published tick. ``ok`` is False on a degraded/failed tick —
    the UI's flag signal (a stale frame keeps the previous frame's
    ``qubes``/``present``, flagged; the first-ever failure is an empty,
    flagged frame, never a silent empty). ``xenstore_ok`` is the
    channel status for this tick (the last known one on a stale frame).
    Series are not carried here: the UI pulls them from the model
    (``Collector.model``) — a fixed-capacity, in-place ring is a
    GIL-benign cross-thread read (worst case: a snapshot one point
    behind; at a 2 s cadence that is invisible to a sparkline)."""
    ts: float
    qubes: Dict[str, QubeSample]
    present: FrozenSet[str]
    ok: bool
    reason: Optional[str]
    xenstore_ok: bool


class TickLike(Protocol):
    """Structural contract for ``tick_fn`` results (the dom0
    ``Sampler.Tick`` satisfies it; tests use a plain stand-in)."""

    records: Sequence[SampleRecord]
    present: Collection[str]


def _default_thread_factory(target: Callable[[], None]) -> threading.Thread:
    return threading.Thread(target=target, daemon=True,
                           name="qubes-top-collector")


class _Call:
    """One outstanding ``tick_fn`` execution on a child thread.
    ``result``/``error`` are written by the child and are read by the
    worker only after ``done`` is set (the ``Event`` provides the
    happens-before), or not at all on a timeout (a late result is
    discarded)."""
    __slots__ = ("thread", "result", "error", "done", "_tick_fn")

    def __init__(self, tick_fn: Callable[[], object]) -> None:
        self.thread: Optional[threading.Thread] = None
        self.result: Optional[object] = None
        self.error: Optional[BaseException] = None
        self.done = threading.Event()
        self._tick_fn = tick_fn

    def run(self) -> None:
        try:
            self.result = self._tick_fn()
        except BaseException as exc:  # park any tick failure
            self.error = exc
        finally:
            self.done.set()


class Collector:
    """The sampling loop: one ``tick_fn`` pass per interval, off the UI
    thread, with per-call timeout, failure isolation, degradation, and
    an idempotent ``start()``/``stop()``. See the module docstring for
    the frozen semantics; every collaborator is injectable (tests run
    it with a fake clock, a fake sleep, and fake sources)."""

    def __init__(self,
                 tick_fn: Callable[[], "TickLike"],
                 model: SampleModel,
                 *,
                 interval: float = DEFAULT_INTERVAL,
                 call_timeout: float = DEFAULT_CALL_TIMEOUT,
                 source_status: Optional[
                     Callable[[], SourceStatus]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 thread_factory: Callable[
                     [Callable[[], None]], threading.Thread] =
                     _default_thread_factory) -> None:
        if interval <= 0:
            raise ValueError("interval must be > 0")
        if call_timeout <= 0:
            raise ValueError("call_timeout must be > 0")
        if call_timeout >= interval:
            raise ValueError(
                "call_timeout must be < interval: one hung call may "
                "delay at most one tick, never swallow the cadence")
        if not callable(tick_fn):
            raise TypeError("tick_fn must be callable")
        self._tick_fn = tick_fn
        self._model = model
        self._interval = interval
        self._call_timeout = call_timeout
        self._source_status = source_status
        self._clock = clock
        self._sleep = sleep
        self._thread_factory = thread_factory
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self._pending: Optional[_Call] = None
        self._last: Optional[Frame] = None
        self._last_xs_ok = True  # last known channel status (first frame)
        self._subscribers: List[Callable[["Frame"], None]] = []
        self._subscriber_errors: List[str] = []

    # -- public API ---------------------------------------------------

    @property
    def model(self) -> SampleModel:
        """The model this collector folds into (the UI pulls series
        from it; the collector's worker thread is its only writer)."""
        return self._model

    @property
    def running(self) -> bool:
        """True from ``start()`` until a clean ``stop()``. Flag-based,
        not ``is_alive()``-based: a freshly ``start()``-ed thread is
        not yet "alive" for an OS-slice, and ``is_alive()`` in that
        window would let a second ``start()`` spawn a second worker on
        one model — the exact hazard ``start()`` must prevent."""
        return self._running

    @property
    def subscriber_errors(self) -> List[str]:
        return list(self._subscriber_errors)

    def subscribe(self, cb: Callable[["Frame"], None]) -> None:
        """``cb(frame)`` is invoked on the worker thread for every
        published frame. A raising callback is recorded in
        ``subscriber_errors`` and skipped — a broken UI handler must
        not kill the sampler. Contract: hand the frame to the UI main
        loop yourself (``GLib.idle_add``); never let a widget be
        touched from this thread."""
        self._subscribers.append(cb)

    def start(self) -> None:
        """Spawn the worker thread. Idempotent: a call while running is
        a no-op (never two workers on one model); a call after
        ``stop()`` restarts (the model is untouched, so rings persist;
        an outstanding call from the previous run still suppresses new
        calls — handle safety, see the module docstring)."""
        with self._lock:
            if self._running:
                return  # already running: no second worker
            self._stop.clear()
            self._running = True
            worker = self._thread_factory(self._run)
            self._worker = worker
        worker.start()

    def stop(self) -> None:
        """Idempotent. ``stop()`` before ``start()`` is a no-op. Joins
        the worker within ``call_timeout + 2 * 0.05 s + 0.25 s`` and
        returns even if the join times out (the worker is a daemon
        thread — a stuck worker cannot block window close or process
        exit). A second ``stop()`` after a timed-out join retries the
        join; once the worker has exited it is a no-op."""
        with self._lock:
            worker = self._worker
            if worker is None or not self._running:
                return
            self._stop.set()
        worker.join(self._call_timeout + 2 * _SLEEP_CHUNK + 0.25)
        if not worker.is_alive():
            with self._lock:
                self._running = False

    # -- the worker loop -----------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = self._clock()
            pending = self._pending
            if pending is not None and not pending.done.is_set():
                # A previous call is still outstanding: do NOT start a
                # new one (one libxenstore handle, D-11 — two threads
                # inside it is a hazard the design never allowed) and
                # do NOT wait on it again (the join already timed
                # out). The cycle advances at full cadence with a
                # flagged frame; the late result is discarded when it
                # lands.
                frame = self._stale_frame(
                    "source busy: previous call still outstanding")
            else:
                self._pending = None  # reap (a finished call, or none)
                frame = self._run_tick()
            self._publish(frame)
            remaining = self._interval - (self._clock() - t0)
            self._sleep_out(remaining)

    def _run_tick(self) -> Frame:
        call = _Call(self._tick_fn)
        call.thread = self._thread_factory(call.run)
        call.thread.start()
        self._pending = call
        if call.done.wait(self._call_timeout):
            self._pending = None  # finished: result consumed below
            if call.error is not None:
                return self._stale_frame(
                    "tick raised: %s: %s"
                    % (type(call.error).__name__, call.error))
            return self._fresh_frame(cast("TickLike", call.result))
        # Timed out: keep ``self._pending`` (the abandoned thread is
        # still tracked; the next tick takes the busy path above).
        return self._stale_frame(
            "call timed out after %.3fs" % self._call_timeout)

    def _fresh_frame(self, tick: "TickLike") -> Frame:
        records = list(tick.records)
        for record in records:
            self._model.ingest(record)
        self._model.synchronize(tick.present)
        xs_ok = self._probe_channel()
        self._last_xs_ok = xs_ok
        qubes: Dict[str, QubeSample] = {}
        for record in records:
            # The *effective* display state: a dead channel is UNKNOWN
            # for every qube (the card's degradation rule); a working
            # channel shows the frozen per-qube machine (post-ingest —
            # "the state of the latest ingested sample").
            if not xs_ok:
                state = MemState.UNKNOWN
            else:
                # Just ingested: the buffer exists, so the model state
                # cannot be None — the cast states what the model's
                # docstring guarantees but cannot express.
                state = cast("MemState", self._model.mem_state(record.name))
            qubes[record.name] = QubeSample(record, state, False, None)
        ts = records[0].ts if records else self._clock()
        frame = Frame(ts=ts, qubes=qubes,
                      present=frozenset(tick.present),
                      ok=True, reason=None, xenstore_ok=xs_ok)
        self._last = frame
        return frame

    def _stale_frame(self, reason: str) -> Frame:
        prev = self._last
        if prev is None:
            # No previous sample to keep: an empty, flagged frame —
            # never a silent empty.
            return Frame(ts=self._clock(), qubes={}, present=frozenset(),
                        ok=False, reason=reason,
                        xenstore_ok=self._last_xs_ok)
        qubes = {name: QubeSample(sample.record, sample.mem_state,
                                  True, reason)
                 for name, sample in prev.qubes.items()}
        return Frame(ts=self._clock(), qubes=qubes,
                    present=prev.present, ok=False, reason=reason,
                    xenstore_ok=prev.xenstore_ok)

    def _probe_channel(self) -> bool:
        if self._source_status is None:
            return True  # no channel knowledge: assume ok (documented)
        try:
            return bool(self._source_status().xenstore_ok)
        except Exception:
            return False  # a broken probe must not masquerade as ok

    def _publish(self, frame: Frame) -> None:
        for cb in list(self._subscribers):
            try:
                cb(frame)
            except Exception as exc:
                self._subscriber_errors.append(
                    "%s: %s" % (type(exc).__name__, exc))

    def _sleep_out(self, remaining: float) -> None:
        """Chunked, stop-aware sleep. ``remaining <= 0`` (a tick that
        ran longer than the interval) skips the sleep: catch up, never
        burst. The sleep path is asserted directly in the tests (the
        fake sleep records its calls) — no busy-spin is possible: every
        cycle either sleeps or was late."""
        while remaining > 0 and not self._stop.is_set():
            chunk = min(remaining, _SLEEP_CHUNK)
            self._sleep(chunk)
            remaining -= chunk
