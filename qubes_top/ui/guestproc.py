"""The guest process table: ``qvm-run -p <vm> … top -b`` and its parser.

The right pane's process panel was dom0-only in v1 (the panel's own seam,
``ProcessProvider``, was built for a later agent). This module is the guest
source: it runs the guest's **own** ``top`` in batch mode through
``qvm-run`` and parses the output.

**This is the one place the monitor starts a process inside a guest** (D-15).
The design's original non-goal was "no command execution"; the owner asked
for the guest process list on 2026-10-18 and ``qvm-run`` is the only way to
see it. The departure is bounded on purpose:

* read-only — ``top`` writes nothing, and the command is a fixed argv, not a
  string built from data;
* no shell — ``--no-shell`` execs ``top`` directly in the guest, so neither
  the qube name nor anything else can be parsed by ``sh``;
* one process per poll, only for the **selected** qube, only while it is
  selected (a background poller, ``GuestProcPoller``, never a per-tick loop
  over the fleet);
* the qube name is validated against the Qubes name alphabet before it
  reaches the argv.

``top -b -n 2 -d 1`` prints two frames and we parse the second: the first
frame's ``%CPU`` is an average since boot, the second's is a delta over the
one-second interval — the same "recent activity" meaning the dom0 table's
per-tick delta has. The cost is that a fetch takes ~1 s, which is why it runs
on its own thread and never in the GLib loop.

Everything here is pure: no ``gi``, no ``qubesadmin``, and the runner is
injectable, so the parser and the polling logic are unit-tested off-box.
"""
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Protocol, Tuple

from qubes_top.ui.processes import ProcessInfo

__all__ = [
    "QUBE_NAME_RE", "GuestProcReading", "GuestProcProvider", "GuestProcPoller",
    "parse_top", "qvm_run_argv", "run_qvm",
]


class Readable(Protocol):
    """What the poller needs from its provider (``GuestProcProvider`` in the
    app, a duck-typed double in tests)."""

    def read(self, name: str) -> "GuestProcReading":
        ...

#: The Qubes name alphabet, anchored. Anything else is refused before it can
#: reach an argv (a name starting with "-" would otherwise be an option).
QUBE_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

#: A command name is guest-supplied text: cap it (the display layer caps
#: again; this bounds what one reading can hold).
_NAME_CAP = 200

#: The batch interval, in seconds, between top's two frames. 1 s gives a
#: meaningful %CPU delta without making the table feel stale.
TOP_DELAY_S = 1

#: A bound on one reading's rows. Batch mode prints the whole table (~500
#: rows on a busy host, measured), so this is not a UI window — it is the
#: limit that stops a hostile or runaway output from growing a reading
#: without bound. The pane scrolls.
MAX_ROWS = 2048


def qvm_run_argv(name: str, delay: int = TOP_DELAY_S) -> List[str]:
    """The argv that reads one guest's process table.

    ``--no-shell`` is load-bearing: ``qvm-run`` otherwise hands COMMAND to
    the guest's shell, and the command would then be a string. With it, the
    guest execs ``top`` directly, so there is no shell to inject into.
    ``-w`` (bare) asks for wide output so COMMAND is not truncated; procps-ng
    treats a bare ``-w`` as 512 columns in batch mode.
    """
    return [
        "qvm-run", "-q", "-p", "--no-shell", name,
        "top", "-b", "-n", "2", "-d", str(delay), "-w", "-o", "%CPU",
    ]


def run_qvm(argv: List[str], timeout: float) -> Tuple[int, str, str]:
    """The default runner: ``qvm-run`` in dom0, captured, never raising.

    Returns ``(returncode, stdout, stderr)``. ``qvm-run`` filters terminal
    escape sequences by default, so a hostile guest cannot paint the
    terminal through this pipe; we still decode leniently.
    """
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def _number(text: str) -> Optional[float]:
    """One of top's numbers, with the locale's decimal comma accepted.

    top formats through ``setlocale()``, so a guest with e.g. ``da_DK`` prints
    ``12,5`` rather than ``12.5``. ``float()`` rejects that, every row's
    ``%CPU`` parsed as unknown, and the column read as a wall of dashes (the
    owner's "%CPU does not get shown"). Both separators are accepted; a
    thousands separator cannot appear in these fields (top prints at most one
    decimal), so replacing every comma is safe.
    """
    if text is None:
        return None
    t = text.strip()
    if t.endswith("%"):
        t = t[:-1].strip()
    if not t:
        return None
    for candidate in (t, t.replace(",", ".")):
        try:
            return float(candidate)
        except ValueError:
            continue
    return None


def _res_kib(text: str) -> Optional[int]:
    """top's RES column: KiB by default ("123456"), or scaled with a suffix
    ("1.2g") when top was asked for human units. An unparseable value is
    None — never a silent zero."""
    t = text.strip().lower()
    if not t:
        return None
    mult = 1
    if t[-1] in ("k", "m", "g", "t"):
        mult = {"k": 1, "m": 1024, "g": 1024 ** 2, "t": 1024 ** 3}[t[-1]]
        t = t[:-1]
    value = _number(t)
    if value is None:
        return None
    return int(value * mult)


def _float_or_none(text: str) -> Optional[float]:
    return _number(text)


def parse_top(text: Optional[str], max_rows: int = MAX_ROWS) -> List[ProcessInfo]:
    """Parse ``top -b`` output into ``ProcessInfo`` rows, or ``[]``.

    The **last** ``PID … COMMAND`` header wins: ``-n 2`` prints two frames
    and only the second one's ``%CPU`` is a real recent delta. Columns are
    located by their header names, so a locale-stable field order or an
    extra column does not shift the values; a row whose width does not match
    the header is skipped (a garbled or hostile line must not become a
    process), and the COMMAND column keeps everything after it, spaces
    included.
    """
    lines = [ln.rstrip("\r") for ln in (text or "").splitlines()]
    header_index = -1
    header: List[str] = []
    for i, line in enumerate(lines):
        fields = line.split()
        if fields and fields[0] == "PID" and "COMMAND" in fields:
            header_index = i
            header = fields
    if header_index < 0:
        return []
    try:
        i_pid = header.index("PID")
        i_cpu = header.index("%CPU")
        i_res = header.index("RES")
        i_cmd = header.index("COMMAND")
    except ValueError:
        return []
    rows: List[ProcessInfo] = []
    for line in lines[header_index + 1:]:
        fields = line.split(None, len(header) - 1)
        if len(fields) != len(header):
            continue
        try:
            pid = int(fields[i_pid])
        except ValueError:
            continue
        name = fields[i_cmd].strip()
        if not name:
            continue
        rss = _res_kib(fields[i_res])
        rows.append(ProcessInfo(
            pid=pid,
            name=name[:_NAME_CAP],
            cpu_pct=_float_or_none(fields[i_cpu]),
            rss_kib=rss if rss is not None and rss > 0 else None))
        if len(rows) >= max_rows:
            break
    return rows


@dataclass(frozen=True)
class GuestProcReading:
    """One guest process reading: the rows (None = none could be read) and a
    one-line note explaining an absence — the pane shows the note, so
    "empty" and "could not read" never look the same."""

    rows: Optional[List[ProcessInfo]]
    note: str = ""


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:160]
    return ""


class GuestProcProvider:
    """``ProcessProvider`` for a guest: ``qvm-run`` + the guest's top.

    ``runner`` is injectable (a test passes a fake with the same
    ``(argv, timeout) -> (rc, stdout, stderr)`` shape). The provider never
    raises: every failure becomes a ``GuestProcReading`` with a note.
    """

    def __init__(self, runner: Optional[Callable[..., Tuple[int, str, str]]] = None,
                 timeout: float = 8.0) -> None:
        self._runner = runner if runner is not None else run_qvm
        self._timeout = timeout

    def read(self, name: str) -> GuestProcReading:
        if not QUBE_NAME_RE.match(name or ""):
            return GuestProcReading(None, "not a valid qube name")
        try:
            rc, out, err = self._runner(qvm_run_argv(name), self._timeout)
        except Exception as exc:  # a runner bug must not kill the poller
            return GuestProcReading(None, "qvm-run failed: %s" % (exc,))
        if rc != 0:
            detail = _first_line(err) or ("exit status %s" % (rc,))
            return GuestProcReading(None, "qvm-run: %s" % detail)
        rows = parse_top(out)
        if not rows:
            return GuestProcReading(
                None, "the guest printed no process table (is `top` installed?)")
        return GuestProcReading(rows)

    # -- the pane's seam ------------------------------------------------
    def list(self, name: str) -> Optional[List[ProcessInfo]]:
        return self.read(name).rows


class GuestProcPoller:
    """Poll the **selected** guest's process table on a background thread.

    ``qvm-run`` + ``top -n 2 -d 1`` costs about a second; run on the GUI
    thread it would freeze the window on every tick, and run in the sampling
    loop it would delay every qube's row. So: one fetch at a time, for the
    selected qube only, posted back to the main loop through ``post``
    (``GLib.idle_add`` in the app — injected here so this module stays
    ``gi``-free and ``poll_once`` is unit-testable).

    A target change is fetched immediately; a repeat fetch waits
    ``interval`` seconds from the previous *start* so a slow guest cannot
    queue fetches. ``emit(target, reading)`` runs on the main loop.
    """

    def __init__(self, provider: Readable,
                 emit: Callable[[str, GuestProcReading], object],
                 post: Callable[..., object],
                 interval: float = 1.5,
                 idle_tick: float = 0.2,
                 host_name: str = "dom0") -> None:
        self._provider = provider
        self._emit = emit
        self._post = post
        self._interval = interval
        self._idle_tick = idle_tick
        self._host = host_name
        self._target: Optional[str] = None
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- target (called from the main thread on selection) ---------------
    def set_target(self, name: Optional[str]) -> None:
        with self._lock:
            self._target = name
            # one target at a time: forget the other qubes' cadence
            self._last = {k: v for k, v in self._last.items() if k == name}

    def target(self) -> Optional[str]:
        with self._lock:
            return self._target

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="qubes-top-guestproc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(2.0, self._provider_timeout() + 1.0))
            self._thread = None

    def _provider_timeout(self) -> float:
        return float(getattr(self._provider, "_timeout", 8.0))

    # -- the loop -------------------------------------------------------
    def _run(self) -> None:  # pragma: no cover - exercised via poll_once
        while not self._stop.is_set():
            try:
                self.poll_once(time.monotonic())
            except Exception:
                # A poller that dies silently would look like "no data"; the
                # next tick retries instead.
                pass
            self._stop.wait(self._idle_tick)

    def poll_once(self, now: float) -> bool:
        """Fetch once when the target is due. Returns True when a fetch ran.

        Pure decision + one provider call + one ``post``; no thread state is
        touched except the per-target cadence map.
        """
        target = self.target()
        if not target or target == self._host:
            return False
        with self._lock:
            last = self._last.get(target)
            if last is not None and now - last < self._interval:
                return False
            self._last[target] = now
        reading = self._provider.read(target)
        self._post(self._emit, target, reading)
        return True
