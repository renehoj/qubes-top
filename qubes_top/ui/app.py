"""The app runner (card t_4d9516cd; the right pane landed in card
t_b14eabf7). GTK3, dom0-only.

Wires the dom0 sampling loop (``qubes_top.dom0.loop.build_collector``) to the
GLib main loop and the window. The **subscriber contract** (the collector's,
not this module's): ``collector.subscribe(cb)`` invokes ``cb`` on the *worker
thread*; ``cb`` hands each frame to the GLib main loop via ``GLib.idle_add``
— the worker thread **never touches a widget**. The idle handler (main thread)
pulls the qube series from the collector's ``SampleModel`` (a GIL-benign
cross-thread read), folds the dom0 host reading into a dedicated host
``SampleModel`` (so dom0 is rendered like any other row), updates the pure
``LeftPaneModel`` in place, refreshes the left pane, and feeds the **right
pane** (card t_b14eabf7) one ``update()`` with the selected qube's record,
effective mem state, series, the metadata (the ``QubeMetaProvider`` seam —
one ``qubesadmin.Qubes()`` instance, shared with the collector's sampler, so
there is one libvirt connection, never a second), and the dom0 process
reading (the ``ProcessProvider`` seam — v1: dom0 only; guests render as
"not available").

The selection seam (``on_select``) updates the right pane **immediately** from
the last known data (so the detail switches on the click, not on the next
2 s tick); the next frame then reconciles. A selection that the last frame
does not know shows "gone" until a frame does — the pane's own contract.

Fails **loudly and cleanly** on the wrong environment: no display -> an exit
code and a message, not a traceback from deep inside the stack. The no-``gi``
case is handled one level up (``__main__`` defers this import and catches the
ModuleNotFoundError).
"""
import os
import sys
import time
import traceback
from typing import Dict

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, GLib  # noqa: E402  (pin above is intentional)

from qubes_top.core import TICK_S, MemState, SampleModel
from qubes_top.dom0.loop import build_collector
from qubes_top.dom0.qubesadmin_adapter import QubesAdapter
from qubes_top.ui.guestproc import GuestProcProvider, GuestProcPoller
from qubes_top.ui.hostprovider import ProcHostProvider, host_to_record
from qubes_top.ui.iostats import (IoHistory, NetIoProvider, RateMeter,
                                read_vbd_bytes)
from qubes_top.ui.labelcolors import LabelColorProvider
from qubes_top.ui.model import LeftPaneModel
from qubes_top.ui.processes import Dom0ProcProvider
from qubes_top.ui.qubemeta import QubesAdminMetaProvider
from qubes_top.ui.rowspec import RowData, dom0_rowdata
from qubes_top.ui.window import QubesTopWindow

DOM0 = "dom0"


def _hz() -> int:
    """CLK_TCK (jiffies per second) for the jiffies->ns conversion. Falls back
    to 100 (the common Linux default) if the query is unavailable."""
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        return 100
    return hz if hz and hz > 0 else 100


def _frame_status(frame) -> str:
    """The status line's text for one frame. Never raises: it is called from
    the main loop, where an exception would be as invisible as the bug this
    line exists to expose."""
    stamp = time.strftime("%H:%M:%S")
    if not getattr(frame, "ok", True):
        note = (getattr(frame, "error", None)
                or getattr(frame, "note", None) or "degraded")
        return "degraded at %s: %s" % (stamp, note)
    return "%d qube(s) · last sample %s" % (len(frame.qubes), stamp)


def run() -> int:
    # Fail cleanly if there is no display (a wrong host / a headless run) — an
    # exit code and a message, not a traceback from deep inside the stack.
    if not Gtk.init_check():
        sys.stderr.write(
            "qubes-top: cannot initialise GTK (no display?). Set DISPLAY or run "
            "in a graphical session.\n")
        return 2

    # Build the dom0 wiring (the thin seam; every collaborator injectable).
    # One Qubes() instance (via the adapter): shared by the collector's
    # sampler and the metadata provider — one libvirt connection, never a
    # second.
    qubes = QubesAdapter()
    collector = build_collector(qubes=qubes)
    # The dot colour per qube: the real Qubes label (D-20), over the same
    # adapter — one ``Qubes()`` instance, shared with the sampler and the
    # metadata provider. ``color_for`` is the seam the row and the detail pane
    # already take, so both dots read the same source and cannot disagree.
    color_for = LabelColorProvider(qubes=qubes).color_for
    model = LeftPaneModel(color_for=color_for)
    host_model = SampleModel()
    host_provider = ProcHostProvider()
    meta_provider = QubesAdminMetaProvider(qubes=qubes)
    proc_provider = Dom0ProcProvider()
    # The guest process source (D-15): qvm-run + the guest's own top, on a
    # background poller for the *selected* qube only (a fetch costs ~1 s and
    # must never run in the GLib loop).
    guest_proc_provider = GuestProcProvider()
    # Disk and network I/O of the selected qube (D-27): the disk counters
    # are dom0's own blkback files, read once per tick here; the network
    # counters live in the qube's netvm and come from a second background
    # poller (qvm-run + cat). Both become one rate per tick in ``io``.
    io = IoHistory()
    disk_meter = RateMeter()
    net_meter = RateMeter()
    hz = _hz()
    # A mutable cell shared by the idle handler: the last known dom0 record
    # (reused when a host read fails so the dom0 row persists), the last
    # dom0 process reading (the provider's baseline is warmed every tick, so
    # selecting dom0 never shows a stale baseline), and the last frame (a
    # consistent snapshot — a stale frame keeps the previous frame's
    # qubes/present, flagged). ``proc_guest`` is the last guest process
    # reading and ``proc_guest_name`` the qube it belongs to.
    state: Dict[str, object] = {"last_dom0": None, "proc_dom0": None,
                                "last_frame": None, "proc_guest": None,
                                "proc_guest_name": None, "proc_note": "",
                                "net_key": None, "net_rates": None,
                                "net_at": 0.0, "net_note": ""}

    def _update_pane() -> None:
        """Feed the right pane for the current selection from the last
        known data (the frame / the host model / the host record). Called
        on every frame and on every selection (so the detail switches on
        the click, not on the next 2 s tick). The pane is a pure renderer;
        this owns only the presence decision and the data pull."""
        sel = model.selected()
        frame = state["last_frame"]
        present = False
        record = None
        mem_state = None
        cpu_values = []
        mem_points = []
        meta = None
        if sel == DOM0:
            d0 = state["last_dom0"]
            if d0 is not None:
                present = True
                record = d0
                mem_state = MemState.REPORTED  # the host's own reading
                cpu_values = [p.pct for p in host_model.cpu_series(DOM0)]
                mem_points = [(p.ts, p.used_kib)
                             for p in host_model.mem_series(DOM0)]
        elif (sel is not None and frame is not None
              and sel in frame.qubes):
            sample = frame.qubes[sel]
            present = True
            record = sample.record
            mem_state = sample.mem_state  # the effective display state
            cpu_values = [p.pct for p in collector.model.cpu_series(sel)]
            mem_points = [(p.ts, p.used_kib)
                         for p in collector.model.mem_series(sel)]
        if present:
            # Best-effort: a metadata failure degrades to "n/a" (the
            # provider never raises into the tick).
            meta = meta_provider.meta(sel)
        # The process panel: dom0 reads its own /proc every tick; a guest is
        # read by the background poller (~1 s per fetch) and may still be
        # empty on the first frames after a click — the note says so rather
        # than showing a bare "not available".
        proc_rows = None
        if sel == DOM0:
            proc_rows = state["proc_dom0"] if present else None
            pane.set_proc_note("not available for this qube")
        elif sel is not None:
            same = state["proc_guest_name"] == sel
            if present and same and state["proc_guest"] is not None:
                proc_rows = state["proc_guest"]
            note = str(state["proc_note"] or "") if same else ""
            pane.set_proc_note(note or "reading processes from the guest…")
        pane.update(sel, present, record, mem_state, cpu_values,
                    mem_points, meta, proc_rows)
        # The info box and the charts under the list: the same spec (the pane
        # refreshed it in place), shown only while something is selected —
        # with no selection the list takes the room. A "gone" selection keeps
        # them visible and frozen, the same way the detail page keeps its
        # data.
        charts.update(pane.spec, io)
        charts.show_for(sel is not None)
        info.update(pane.spec)
        info.show_for(sel is not None)

    def _on_guest_proc(name, reading) -> bool:
        """Main-loop callback: one guest process reading arrived.

        The row is applied only when the qube is still selected — a fetch
        takes ~1 s and the user may have clicked elsewhere in the meantime.
        """
        state["proc_guest_name"] = name
        state["proc_guest"] = reading.rows
        state["proc_note"] = reading.note
        if model.selected() == name:
            _update_pane()
        return False  # one-shot idle

    proc_poller = GuestProcPoller(guest_proc_provider, emit=_on_guest_proc,
                                  post=GLib.idle_add)

    def _on_net_io(target, reading) -> bool:
        """Main-loop callback: one netvm reading of the vif counters arrived.
        The rate is kept with the ``(netvm, domid)`` it belongs to; the next
        frame takes it only if that is still the selected qube's."""
        state["net_key"] = target
        state["net_rates"] = net_meter.feed(target, reading.ts,
                                            reading.counters)
        state["net_at"] = reading.ts
        state["net_note"] = reading.note
        return False  # one-shot idle

    # One read per tick: the vif counters change continuously, so a faster
    # poll would only add qvm-run calls the 2 s chart cannot show.
    net_poller = GuestProcPoller(NetIoProvider(), emit=_on_net_io,
                                 post=GLib.idle_add, interval=TICK_S)

    def _sample_io(frame) -> None:
        """One I/O value per frame for the selected qube (D-27), so the
        series lines up with the CPU line tick for tick. dom0 has neither: its
        disks are not blkback devices and it has no network. A selection the
        frame does not know keeps its history frozen, like the other charts.
        """
        sel = model.selected()
        if sel is None or sel == DOM0:
            net_poller.set_target(None)
            io.reset(sel)
            io.disk_note = io.net_note = "not measured for dom0"
            return
        if sel not in frame.qubes:
            net_poller.set_target(None)
            return
        record = frame.qubes[sel].record
        domid = record.domid
        key = (sel, domid)
        if domid < 0:
            net_poller.set_target(None)
            io.push(key, None, None)
            io.disk_note = io.net_note = "not running"
            return
        now = time.monotonic()
        counters = read_vbd_bytes(domid)
        disk = disk_meter.feed(key, now, counters)
        meta = meta_provider.meta(sel)
        netvm = meta.netvm if meta is not None else None
        target = (netvm, domid) if netvm else None
        net_poller.set_target(target)
        net = None
        fresh = now - float(state["net_at"]) <= 2.5 * TICK_S
        if target is not None and state["net_key"] == target and fresh:
            net = state["net_rates"]
        io.push(key, disk, net)
        io.netvm = netvm or ""
        io.disk_note = ("" if counters is not None else
                        "no blkback counters for domid %d" % domid)
        if target is None:
            io.net_note = "no netvm: no network to measure"
        elif state["net_key"] == target and state["net_note"]:
            io.net_note = str(state["net_note"])
        else:
            io.net_note = "reading the vif in %s…" % netvm

    def on_select(name: str) -> None:
        # The selection seam: the left pane already selected in its model;
        # the right pane (t_b14eabf7) is wired here, and the guest process
        # poller is pointed at the new target (a guest is fetched at once,
        # dom0 is read from /proc in the tick). The print stays — a staged
        # run can observe selection.
        print("qubes-top: selected %s" % name, flush=True)
        proc_poller.set_target(name)
        # the I/O charts start over for the new qube (``IoHistory`` holds
        # one); the next frame fills them
        io.reset(name)
        _update_pane()

    window = QubesTopWindow(model, on_select=on_select, color_for=color_for)
    # The pane (bound before any `_update_pane` call can happen: the first
    # call is a selection click or a frame, both after this line).
    pane = window.right_pane
    # The large charts under the list render the pane's spec (refreshed in
    # place by `pane.update`), so they are fed right after it — as is the info
    # box above them.
    charts = window.charts
    info = window.info

    def on_frame(frame) -> None:
        # The subscriber contract: hand the frame to the GLib main loop; the
        # worker thread never touches a widget.
        GLib.idle_add(_on_frame_main, frame)

    def _render_frame(frame) -> bool:
        # This frame is the consistent snapshot for the right pane (a
        # stale frame keeps the previous frame's qubes/present, flagged).
        state["last_frame"] = frame
        # Build this tick's RowData list: the frame's qubes (series pulled
        # from the collector's model — a GIL-benign cross-thread read) + dom0
        # (from the host model, which the host provider feeds below).
        cmodel = collector.model
        data = []
        for name, sample in frame.qubes.items():
            data.append(RowData(
                name=name, record=sample.record, mem_state=sample.mem_state,
                cpu_values=tuple(p.pct for p in cmodel.cpu_series(name)),
                mem_points=tuple((p.ts, p.used_kib)
                                for p in cmodel.mem_series(name)),
            ))
        # dom0: read the host, fold into the host model, pull its series. On a
        # failed read, reuse the last known record so the row persists.
        raw = host_provider.read()
        if raw is not None:
            d0 = host_to_record(raw, hz, DOM0)
            host_model.ingest(d0)
            host_model.synchronize([DOM0])
            state["last_dom0"] = d0
        elif state["last_dom0"] is None:
            # No host reading yet (first tick failed): emit nothing for dom0
            # this tick (there is no last row to show); the next good read
            # creates it. dom0 is still "always present" in the *model* sense
            # once it has data; before the first good read there is nothing.
            pass
        if state["last_dom0"] is not None:
            data.append(dom0_rowdata(
                state["last_dom0"],
                list(p.pct for p in host_model.cpu_series(DOM0)),
                list((p.ts, p.used_kib)
                     for p in host_model.mem_series(DOM0)),
            ))
        # Warm the dom0 process baseline every tick (a read-only /proc
        # scan), so selecting dom0 never shows a stale or missing baseline
        # (the first displayed reading has a cpu value, not "—").
        proc = proc_provider.list(DOM0)
        if proc is not None:
            state["proc_dom0"] = proc
        _sample_io(frame)
        model.refresh(data)
        window.refresh()
        # a degraded frame reads in the footer's error red, as systemlist's
        window.set_status(_frame_status(frame),
                          not getattr(frame, "ok", True))
        if not state.get("seen_first"):
            # One line on the terminal: proof the loop is delivering frames.
            state["seen_first"] = True
            print("qubes-top: first frame: %d qube(s)" % len(frame.qubes),
                  file=sys.stderr, flush=True)
        _update_pane()
        return False  # one-shot idle

    def _on_frame_main(frame) -> bool:
        """Render one frame, never letting an exception vanish.

        This runs inside the GLib main loop: an uncaught exception prints a
        traceback to a terminal the user may not be watching and otherwise
        leaves the list empty with no sign — which is how the row-visibility
        bug presented. The status line reports it instead.
        """
        try:
            return _render_frame(frame)
        except Exception:
            traceback.print_exc()
            window.set_status("internal error: %s (traceback on the terminal)"
                              % (sys.exc_info()[1],), True)
            return False

    collector.subscribe(on_frame)
    collector.start()
    # The guest process poller runs for the whole session and only works
    # when a guest is selected (dom0 needs no qvm-run).
    proc_poller.start()
    net_poller.start()
    # The window's __init__ already called show_all() (the hard requirement);
    # nothing further is needed to make it visible.
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
        proc_poller.stop()
        net_poller.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
