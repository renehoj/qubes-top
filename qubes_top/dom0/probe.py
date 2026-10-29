"""Staged dom0 probe (card t_ae308ca8): verify the data path on the real
target, before the UI is built on top of the adapters.

The previous project shipped a GUI that had never executed anywhere; both
live defects were invisible off-box. This probe is the first thing a
human runs on dom0 (DESIGN.md section 7, M2 gate), and it refuses on
the wrong host.

Run it (from the repo root, or via ``packaging/dom0-probe.sh``):

    python3 -m qubes_top --probe

Contract (card t_ae308ca8):

- one PASS/FAIL/NOTE line per check, then a completion marker;
- refusal: if all Qubes imports fail (the qubesadmin import AND the
  ctypes libxenstore load), print FAIL and stop -- a run on the wrong
  host must never read as green;
- read-only: stdout only, no file writes anywhere (the wrapper script
  owns the ``mktemp -d`` scratch directory, removed on exit);
- human-pastable: no colour, no spinners.

Output order:

1. the gate (the two Qubes import checks + the gate line); on refusal:
   the marker and stop;
2. one line per running qube (two real ``Sampler`` ticks 5.0 s apart,
   rows sorted by name):
   ``name domid state assigned max meminfo swapinfo
   report_state(reported|mute|unknown) meminfo_changed(5s)``;
3. the environment block: ``/etc/qubes-release``, ``python3 -V``,
   ``qubesadmin.__version__``, the ctypes libxenstore load result, the
   GTK 3.0 pin (``gi.require_version`` on both namespaces, Gtk first),
   ``Gdk.Screen.get_default().is_composited()``, ``command -v qubes-top``,
   and the M2 name check (``qubes-top`` absent from the installed
   qubesadmin tools directory -- a gate, not a formality).

Exit codes: 0 completed (some checks may be FAIL -- the marker carries
the fail count), 2 refused (not dom0), 1 crashed (raw traceback
printed; paraphrasing loses the line that matters).
"""
import os
import subprocess
import sys
import time
import traceback
from typing import Any, List, Optional, Tuple

from qubes_top.core import SampleRecord, dot_color_for_label, mem_report_state
from qubes_top.dom0.qubesadmin_adapter import QubesAdapter
from qubes_top.dom0.sampler import Sampler
from qubes_top.dom0.xenstore_adapter import (
    Xenstore, XenstoreError, XenstoreUnavailable, candidate_names,
)

__all__ = [
    "EXIT_OK", "EXIT_CRASHED", "EXIT_REFUSED", "MEMINFO_WINDOW_S",
    "ROW_COLUMNS", "gate_refuse", "qubesadmin_import_check",
    "libxenstore_check", "meminfo_changed", "format_qube_row",
    "row_header", "qube_row_phase", "qubes_release_check",
    "python_version_line", "qubesadmin_version_line", "gtk_check",
    "composited_line", "command_v_line", "name_free_check",
    "label_colors_check", "run_probe",
]

EXIT_OK = 0
EXIT_CRASHED = 1
EXIT_REFUSED = 2

#: The 5 s window for ``meminfo_changed(5s)`` (card t_ae308ca8).
MEMINFO_WINDOW_S = 5.0

#: Row columns in the card's order: (column name, field width). The
#: assigned/max/meminfo/swapinfo values are KiB (8 digits at 32 GiB).
#: The figures come from xenstore (D-14): ``assigned`` is ``memory/target``
#: (qmemman's live balloon) and ``max`` is ``memory/hotplug-max`` else
#: ``memory/static-max`` (the configured maximum). They differ legitimately —
#: a qube configured at 4000 MiB can sit at 1000 MiB — and ``meminfo``
#: (guest-reported, includes swap) can exceed ``assigned``.
ROW_COLUMNS: Tuple[Tuple[str, int], ...] = (
    ("name", 20),
    ("domid", 6),
    ("state", 8),
    ("assigned", 9),
    ("max", 9),
    ("meminfo", 9),
    ("swapinfo", 9),
    ("report_state", 13),
    ("meminfo_changed(5s)", 18),
)


def gate_refuse(qubesadmin_ok: bool, libxenstore_ok: bool) -> bool:
    """The card's refusal condition, verbatim: *all* Qubes imports
    failed. One working Qubes import path (the python package or the C
    library) means the host is Qubes-enough to sample; each missing
    path is still reported as its own FAIL line."""
    return not (qubesadmin_ok or libxenstore_ok)


def qubesadmin_import_check() -> Tuple[bool, str]:
    """The first Qubes import: ``import qubesadmin``. Returns
    (importable, detail) -- the raw exception text on failure, the
    package ``__version__`` on success."""
    try:
        import qubesadmin
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    return True, getattr(qubesadmin, "__version__", "unknown")


def libxenstore_check() -> Tuple[bool, str]:
    """The second Qubes import: the ctypes load of libxenstore, through
    the real adapter (one code path, not a re-implementation).

    Returns (loaded, detail). ``loaded`` means the ctypes load
    succeeded -- including the case where the library is present but
    ``xs_open`` returns NULL (a target anomaly: the library *did*
    load, so the gate counts it; the anomaly is in the detail)."""
    xs = Xenstore()
    try:
        xs.read_all()
    except XenstoreUnavailable as exc:
        return True, "library present, xs_open returned NULL: %s" % exc
    except XenstoreError as exc:
        return xs.available, "library loaded (%s), read failed: %s" % (
            xs.library_name, exc)
    if xs.available:
        return True, "loaded: %s" % xs.library_name
    return False, "no candidate loaded: %s" % ", ".join(candidate_names())


def meminfo_changed(first: Optional[int], second: Optional[int]) -> bool:
    """The 5 s window: did the meminfo value change between the two
    ticks?

    None-safe: ``(None, None)`` is no change; a value on one side and
    None on the other (the key appeared or vanished) is a change."""
    return first != second


def _cell(value: object, width: int) -> str:
    text = "-" if value is None else str(value)
    return text.ljust(width)


def format_qube_row(rec: SampleRecord, report_state: str,
                   changed: Optional[str]) -> str:
    """One report line for one running qube: the card's column list, in
    order. ``changed`` is ``"yes"`` / ``"no"`` or None (the second
    tick failed; rendered ``-``)."""
    cells = (
        rec.name, rec.domid, rec.state, rec.mem_assigned_kib,
        rec.mem_max_kib, rec.mem_used_kib, rec.swap_used_kib,
        report_state, changed,
    )
    return "".join(_cell(v, w) for v, (_, w) in zip(cells, ROW_COLUMNS))


def row_header() -> str:
    """The column header line, built from the same widths as the rows."""
    return "".join(name.ljust(w) for name, w in ROW_COLUMNS)


def qube_row_phase() -> List[str]:
    """Two real ``Sampler`` ticks 5.0 s apart (the frozen per-tick flow
    -- the probe exercises the real adapters, not a bypass). Returns the
    report lines. Tick failures are FAIL lines with the raw traceback:
    the probe never dies silently and never fabricates a value."""
    lines: List[str] = [
        "qube rows: one per running qube (sorted by name; two real "
        "Sampler ticks %.1f s apart)" % MEMINFO_WINDOW_S,
        row_header(),
    ]
    sampler = Sampler()
    try:
        tick1 = sampler.tick()
    except Exception:
        lines.append("FAIL qubes-tick-1: raw traceback follows")
        lines.append(traceback.format_exc())
        lines.append("NOTE qube-rows: no rows (first tick failed)")
        return lines

    tick2 = None
    try:
        time.sleep(MEMINFO_WINDOW_S)
        tick2 = sampler.tick()
    except Exception:
        lines.append("FAIL qubes-tick-2: raw traceback follows")
        lines.append(traceback.format_exc())

    by_name = ({r.name: r for r in tick2.records}
               if tick2 is not None else None)
    if not tick1.records:
        lines.append("NOTE qube-rows: no running qubes")
        return lines

    for rec in sorted(tick1.records, key=lambda r: r.name):
        state = mem_report_state(rec.mem_used_kib,
                                rec.mem_assigned_kib).value
        if by_name is None:
            changed = None  # second tick failed: unknown, not "no"
        elif rec.name not in by_name:
            changed = "yes"  # the qube (and its meminfo key) is gone
        else:
            changed = "yes" if meminfo_changed(
                rec.mem_used_kib, by_name[rec.name].mem_used_kib
            ) else "no"
        lines.append(format_qube_row(rec, state, changed))
    return lines


def label_colors_check(adapter: Any = None) -> List[str]:
    """Every domain's Qubes label name and colour (D-20), read exactly the way
    the UI reads it: ``vm.label`` (``admin.vm.property.Get label``, resolved by
    ``PropertyHolder.__getattr__`` into a ``label.Label`` object) then
    ``Label.color`` (``admin.label.Get``, ``#rrggbb``).

    This is the check that decides — before the GUI is opened — whether the
    row dots can show real labels *on this target*. The API shape above is
    read from ``release4.3`` source and has never executed here; a domain
    whose label cannot be read is a FAIL carrying the raw exception, never a
    silently hashed dot.

    ``adapter`` is injectable (tests drive a fake off-box); the default is the
    real ``QubesAdapter``, i.e. one ``Qubes()`` instance.
    """
    lines = ["labels: every domain's Qubes label (D-20 — the row dot's colour)"]
    if adapter is None:
        try:
            adapter = QubesAdapter()
        except Exception as exc:
            return lines + ["FAIL labels: adapter construction raised: %s: %s"
                            % (type(exc).__name__, exc)]
    try:
        domains = list(adapter.app.domains)
    except Exception:
        lines.append("FAIL labels: app.domains raised (raw traceback follows)")
        lines.append(traceback.format_exc())
        return lines
    if not domains:
        return lines + ["NOTE labels: app.domains is empty (nothing to read)"]
    readable = 0
    for vm in sorted(domains, key=lambda v: getattr(v, "name", "")):
        name = getattr(vm, "name", "?")
        try:
            label = vm.label
            label_name = label.name if label is not None else None
        except Exception as exc:
            lines.append("FAIL label %s: %s: %s"
                         % (name, type(exc).__name__, exc))
            continue
        if label is None:
            lines.append("FAIL label %s: label is None (no label set)" % name)
            continue
        # Mirror the app exactly (D-22): a default label is answered by the
        # Nord map and needs no colour RPC at all, so the probe does not ask
        # for one either. Only a custom label reads qubesd's value — and that
        # read is where a policy refusal or a format surprise shows up.
        dot = dot_color_for_label(label_name)
        raw = "-"
        if dot is None:
            try:
                colour = label.color
            except Exception as exc:
                lines.append("FAIL label %s: %s: %s"
                             % (name, type(exc).__name__, exc))
                continue
            raw = repr(colour)
            dot = dot_color_for_label(label_name, colour)
            if dot is None:
                lines.append("FAIL label %s: label=%r colour=%r is neither "
                             "0xrrggbb nor #rrggbb"
                             % (name, label_name, colour))
                continue
        readable += 1
        # "-" in the colour column means "the name was mapped, no RPC needed";
        # a custom label shows the raw value it was read from
        lines.append("  label %-20s %-8s %-10s dot %s"
                     % (name, label_name, raw, dot))
    lines.append("%s labels: %d/%d domains resolve to a Qubes label colour"
                 % ("PASS" if readable == len(domains) else "FAIL",
                    readable, len(domains)))
    return lines


def qubes_release_check() -> List[str]:
    path = "/etc/qubes-release"
    if not os.path.isfile(path):
        return ["FAIL qubes-release: %s absent" % path]
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read().strip()
    lines = ["PASS qubes-release: %s present" % path]
    for line in raw.splitlines():
        lines.append("  raw: %s" % line)
    return lines


def python_version_line() -> str:
    """The ``python3 -V`` line (the probe's own interpreter)."""
    return "NOTE python: Python %s" % sys.version.split()[0]


def qubesadmin_version_line(check: Tuple[bool, str]) -> str:
    ok, raw = check
    if ok:
        return "NOTE qubesadmin-version: %s" % raw
    return "NOTE qubesadmin-version: unavailable (import failed: %s)" % raw


def gtk_check() -> Tuple[List[str], Optional[object]]:
    """The GTK 3.0 pin in the card's order: ``gi.require_version`` on
    both namespaces, Gtk first, then the ``gi.repository`` import.
    Returns (the check line, the imported ``Gdk`` module or None)."""
    try:
        import gi
    except Exception as exc:
        return ["FAIL gtk-pin: gi import failed: %s: %s"
                % (type(exc).__name__, exc)], None
    try:
        gi.require_version("Gtk", "3.0")  # Gtk first (the card)
        gi.require_version("Gdk", "3.0")
    except Exception as exc:
        return ["FAIL gtk-pin: require_version failed: %s: %s"
                % (type(exc).__name__, exc)], None
    try:
        from gi.repository import Gdk, Gtk
    except Exception as exc:
        return ["FAIL gtk-pin: gi.repository import failed: %s: %s"
                % (type(exc).__name__, exc)], None
    _ = Gtk  # the import is the check; the reference keeps pyflakes honest
    return ["PASS gtk-pin: Gtk 3.0 + Gdk 3.0 required (Gtk first) and "
            "imported"], Gdk


def composited_line(gdk: Optional[object]) -> str:
    """``Gdk.Screen.get_default().is_composited()`` -- a NOTE line:
    the compositor state is a fact to report, not a pass/fail check.
    Degrades to a NOTE (never a crash) when there is no display or the
    call raises."""
    if gdk is None:
        return "NOTE composited: n/a (the gtk-pin check failed)"
    try:
        screen = gdk.Screen.get_default()
    except Exception as exc:
        return "NOTE composited: get_default() raised: %s: %s" % (
            type(exc).__name__, exc)
    if screen is None:
        return "NOTE composited: no default screen (no display?)"
    return "NOTE composited: %s" % screen.is_composited()


def command_v_line() -> str:
    """``command -v qubes-top``, raw."""
    try:
        proc = subprocess.run(
            ["sh", "-c", "command -v qubes-top"],
            capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return "NOTE qubes-top-path: command -v failed: %s: %s" % (
            type(exc).__name__, exc)
    out = (proc.stdout or "").strip()
    return "NOTE qubes-top-path: %s" % (out if out else "not found")


def name_free_check(tools_dir: Optional[str] = None) -> List[str]:
    """The M2 name check (DESIGN.md section 3; the comment on card
    t_ae308ca8): ``qubes-top`` must be absent from the installed
    qubesadmin tools directory. A gate, not a formality.

    ``tools_dir`` is injectable for the off-box unit tests; by default
    it is derived from the installed qubesadmin."""
    if tools_dir is None:
        try:
            import qubesadmin
        except Exception:
            return ["NOTE name-free: skipped (the qubesadmin import failed)"]
        tools_dir = os.path.join(os.path.dirname(qubesadmin.__file__),
                                 "tools")
    if not os.path.isdir(tools_dir):
        return ["FAIL name-free: the tools directory was not found: %s"
                % tools_dir]
    entries = sorted(os.listdir(tools_dir))
    colliding = [e for e in entries
                 if e.startswith("qubes_top") or e.startswith("qubes-top")]
    if colliding:
        return ["FAIL name-free: name collision in %s: %s -- rename "
                "before the binary lands (DESIGN.md section 3)"
                % (tools_dir, ", ".join(colliding))]
    return ["PASS name-free: no qubes_top/qubes-top entry in %s (%d tool "
            "modules listed)" % (tools_dir, len(entries))]


def run_probe() -> int:
    """Run the whole probe, printing to stdout. Returns the exit code
    (0 completed / 2 refused / 1 crashed). The fail count in the
    completion marker is the number of FAIL lines printed."""
    fails = 0

    def say(line: str) -> None:
        nonlocal fails
        print(line)
        if line.startswith("FAIL"):
            fails += 1

    try:
        say("== qubes-top staged dom0 probe (card t_ae308ca8) ==")

        qa = qubesadmin_import_check()
        xs = libxenstore_check()
        if gate_refuse(qa[0], xs[0]):
            say("FAIL qubesadmin-import: %s" % qa[1])
            say("FAIL libxenstore-load: %s" % xs[1])
            say("FAIL dom0-gate: all Qubes imports failed -- this is not "
                "dom0")
            say("PROBE REFUSED: not dom0 (stop; a run on the wrong host "
                "must not read as green)")
            return EXIT_REFUSED
        say(("PASS" if qa[0] else "FAIL") + " qubesadmin-import: %s" % qa[1])
        say(("PASS" if xs[0] else "FAIL") + " libxenstore-load: %s" % xs[1])
        say("PASS dom0-gate: at least one Qubes import path works")

        for line in qube_row_phase():
            say(line)

        for line in label_colors_check():
            say(line)

        say("-- environment --")
        for line in qubes_release_check():
            say(line)
        say(python_version_line())
        say(qubesadmin_version_line(qa))
        gtk_lines, gdk = gtk_check()
        for line in gtk_lines:
            say(line)
        say(composited_line(gdk))
        say(command_v_line())
        for line in name_free_check():
            say(line)

        say("PROBE COMPLETE (fails=%d)" % fails)
        return EXIT_OK
    except Exception:
        say("FAIL probe-crash: raw traceback follows (paraphrasing loses "
            "the line that matters)")
        print(traceback.format_exc())
        say("PROBE CRASHED (fails=%d)" % fails)
        return EXIT_CRASHED
