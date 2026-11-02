# qubes-top

"top for Qubes": a GTK3 window app that runs in Qubes dom0. Left pane: every
running qube as a six-column table — name, status, CPU %, CPU sparkline,
memory (used / configured, GiB), memory sparkline — sorted by name; right
pane: detail for the selected qube, including a process table (dom0's `/proc`,
or the selected guest's own `top` through `qvm-run`). Under the list, four
history charts for the selected qube: CPU, memory, disk I/O and network.

Read-only by construction (no xenstore write call, no per-qube RPC; the only
commands the app ever runs are the guest's `top` for the process panel, D-15,
and a `cat` of the selected qube's vif counters in its netvm, D-27) and
dom0-local: the only inputs are `admin.vm.CurrentState` through
qubesadmin, `xs_read` of each domain's `memory/*` keys, dom0's own blkback
counters for the selected qube's disks, and those two `qvm-run` outputs.

## Status — read this first

**Owner-verified working on real dom0: Qubes 4.3.2 (R4.3), Python 3.13.9,
2026-10-17.** The window opens, lists every running qube with live CPU and
memory sparklines, selection works, and the dom0 process table renders and
sorts. What ran was the working tree via `python3 -m qubes_top --gui`, preceded
by `-m qubes_top --probe` and `-m qubes_top --stage` on the same host.

### Verified on the target

- `--probe`: eleven running qubes, live `meminfo` per qube, `sys-net`
  correctly `mute` (no meminfo key) rather than a synthesised 100%.
- `--api-audit`: every `Gtk`/`Gdk`/`Pango`/`GLib` symbol and every method
  called on a widget variable resolves against the host's real GTK.
- GUI: rows, sparklines, click-selection, right pane, dom0 process table.

### Defects the target found that no off-box test could

All fixed and guarded; each one was invisible on a box without `gi` and
without a real Qubes.

1. The libxenstore soname here is `libxenstore.so.4`; the hardcoded candidate
   list missed it. Resolution now asks the dynamic loader first and falls back
   to the known names.
2. `free()` was bound without `argtypes`, so ctypes passed the 64-bit pointer
   from `xs_read` as a C `int` — truncated, then `free()` on garbage: SIGSEGV
   (rc 139) on the first successful read.
3. `get_cputime()` does not exist in qubesadmin 4.3.2. `QubesVM.__getattr__`
   resolves an unknown attribute as a qubesd *property* lookup, so it fails as
   `QubesNoSuchPropertyError`. CPU time comes from `admin.vm.CurrentState`,
   which supplies `mem` and `mem_static_max` in the same call.
4. `Gtk.StyleContext.get_default()` and `Gtk.Label.get_pango_attributes()` do
   not exist in GTK3.
5. Rows were created with `show()`, which reveals a widget and none of its
   children: every row rendered as an empty strip.
6. `_QubeRow` was a `Gtk.Box` — a plain container has no `GdkWindow`, so GDK
   never delivered a click to it and nothing was selectable.
7. The dom0 process table indexed its container `Box` as if it were the
   `{pid: row}` map (TypeError on selecting dom0) and used `show()` for new
   rows.

### Changed since that run (verified off-box, not yet re-run on the target)

The left pane was rebuilt as a real table: six columns (name, status, CPU %,
CPU graph, memory, memory graph) with a header that is generated from the same
column table as the rows, default order **by name ascending**, memory always
in **GiB** ("0.39 / 1.0 GiB", with "—" for a qube that does not report memory —
the detail pane spells the state out), and a memory sparkline that draws a flat
line instead of a dot when a qube's memory has not moved past the 30 MiB
deadband, scaled to the qube's own ceiling so a steady qube is not drawn pinned
to the top. The large history charts are a fixed-height block **under the qube
list** (**D-17**), not in the detail pane: they describe the selected row, and
in the detail pane they pushed the process table down. Since **D-27** they are
four charts in two columns — CPU and memory, then disk I/O and network — in the
room the two stacked charts took (see below). Above them sits
an **info group box** (**D-18**) holding the fields the detail pane used to
carry — template, netvm, vcpus, mem max, then allocated now, used, swap, report
state and domid. Both blocks are hidden while nothing is selected (the list gets
the room back) and stay visible but frozen if the selected qube disappears.

The process table is a column table on the same mechanism (**D-16**): one
`PROC_COLUMNS` description (PID, NAME, %CPU, RSS) that the header and every row
build from, a size group per column shared between them, right-aligned
monospaced numbers, and a header that reserves the scrollbar's width — the
scrollbar is what used to shift every column left by ~14 px. Clicking a column
title sorts; there is no title bar and no sort hint above it (**D-19**), so the
table starts immediately under the identity line.

Verified off-box by `tests/test_ui_wiring.py` (executes `LeftPane` and
`_QubeRow` against a narrow fake `gi`: column order, header drift, size-group
registration, in-place refresh, scrollbar reserve), `tests/test_ui_layout.py`
(the six columns and a width budget that fits the default divider) and the
static pins in `tests/test_ui_visibility.py`. That is **not** a target run:
GTK geometry and theme metrics can only be judged on dom0, so an owner run of
`python3 -m qubes_top --gui` after `--api-audit` is still the remaining step.

### The label dot is the real Qubes label (D-20 → D-22)

The dot beside each name — in the list and in the detail identity line — is the
qube's **label colour**, never a hash. Since **D-22** the paint is that label's
**Nord** tone rather than Qubes' saturated one: the desktop is Nord-themed and
`#cc0000`/`#edd400` clash with it. Owner: *"The dot should be the Qubes OS domain
color for the VM, it makes more sense then a random color"*, then *"can you find
the nord color palette and pick nord color for the dots"*.

| Qubes label | qubesd | dot | |
|---|---|---|---|
| red | `0xcc0000` | `#bf616a` | nord11 (Aurora) |
| orange | `0xf57900` | `#d08770` | nord12 (Aurora) |
| yellow | `0xedd400` | `#ebcb8b` | nord13 (Aurora) |
| green | `0x73d216` | `#a3be8c` | nord14 (Aurora) |
| purple | `0x75507b` | `#b48ead` | nord15 (Aurora) |
| blue | `0x3465a4` | `#5e81ac` | nord10 (Frost — nord9/8 read cyan by green) |
| black | `0x000000` | `#3b4252` | nord1 (nord0 *is* the background) |
| gray | `0x555555` | `#d8dee9` | nord4 (Nord has no neutral grey; nord3 collides with black) |

A **custom** label — one Qubes does not ship — has no Nord tone, so its dot uses
the colour qubesd reports. The eight defaults need **no colour RPC at all**:
`core.dot_color_for_label` answers them from the map, so a refused
`admin.label.Get` cannot blank a known label.

`qubes_top/ui/labelcolors.py` reads `vm.label` (which `PropertyHolder.__getattr__`
turns into a `label.Label` object through `admin.vm.property.Get label`) and then
`Label.color` (`admin.label.Get`), both pinned against
`qubes-core-admin-client@release4.3` source. It caches name → colour, re-reads
the domain list on a 60 s TTL and immediately for a name it has never seen,
and falls back to the old deterministic hash for anything it cannot read — an
unknown name, a failed read, a client without labels. A dot is always drawn;
nothing here can crash a frame.

**The colour arrives as `0xRRGGBB`, not `#RRGGBB`** — that is how qubesd
serialises labels (`qubes-core-admin@release4.3` `qubes/app.py`:
`Label(3, "0xedd400", "yellow")`, and `admin.label.Get` returns `label.color`
verbatim). `core.parse_color` normalises either form to the `#rrggbb` a widget
needs and rejects anything that is neither. This was wrong on the first try: the
parser accepted only `#rrggbb`, so on the target every colour was rejected and
every dot fell back to the hash — a qube labelled *yellow* got a random colour,
which the owner caught with *"if the label has the text yellow the dot should
have the colot yellow"*. The probe now prints the raw value beside the
normalised one (`0xedd400 -> #edd400`) precisely so a format surprise is one
line instead of a colour argument, and the tests speak the real `0x` format —
a fake that speaks a format reality does not is a fake that certifies the bug.

Two details worth knowing. The dot now carries a faint grey ring, because
`black` is one of Qubes' labels and dom0 wears it by default — a black disc on a
dark theme reads as *no label*, which is a different statement. And a **failed
label read is not a removal**: a transient `property.Get` failure keeps the
colour already known for that qube instead of flipping its dot to the hash and
back, while a qube genuinely gone from `app.domains` is dropped so a recreated
qube cannot inherit its predecessor's colour.

The colour cannot ride the existing per-tick read — `CurrentState` carries no
label — so this costs one `property.Get` per domain per pass, i.e. N per minute
against the sampler's N per 2 s tick, and nothing at all on the per-row path.
`--probe` gained a `labels:` phase that prints every domain's label name, the
raw colour when one is read (custom labels), and the tone the dot will paint —
so a colour surprise is one line of output, not a screenshot argument. That
phase is the pre-GUI proof that the label path works on the target.

### Each sparkline states its own interval (D-21)

The small graphs auto-scale per row, which is what keeps a 30 MiB step visible
on a 400 MiB qube — and what made the CPU graph's height mean *variability*
rather than load. The owner's fix keeps the scaling and removes the ambiguity:
**a centred grey label in each row graph carries the upper bound the graph is
drawn against.** Owner: *"If there was a center text with the highest value from
the data set it would make it easier to read. As an example if it says 10% it
would mean the graph currently has 0-10 as the interval."*

The text is built in `RowSpec` (`cpu_top_text`, `mem_top_text`) from the same
bounds the geometry uses, so it cannot describe an interval the graph is not
drawn against: the CPU label is the series maximum (`own_bounds`), the memory
label is the **ceiling the memory step is scaled to** — the configured max, the
same denominator the memory column divides by — not the highest point in the
series. A series with nothing to draw carries an empty string and no label is
painted; a flat 0% series says `1.0%`, because that is the range
`own_bounds` widens a degenerate series to and therefore what is on screen.

`draw_graph_top_label` centres on the glyphs' ink via `text_extents` (dropping
the text at the width/height midpoint would sit it visibly high and right),
draws at 9 px in near-white over a four-way dark halo — a single grey pass over
a bright polyline read as hard to see in the row mock, and a halo needs no
theme knowledge the way a filled backing box would — and draws after the line,
so a cairo surprise costs the label, never the sparkline.

Because that wrapping is deliberate and would otherwise let a wrong cairo method
fail *silently*, `--api-audit` grew an `audit_cairo` pass: every `cr.<method>`
in the UI is resolved against the host's real `cairo.Context` on dom0, the same
treatment gi symbols already get. That also covers `charts.py`'s `show_text`,
which had never been checked either.

### Guest process table (D-15)

Selecting a guest now fills the process panel from that guest's own `top`,
run through `qvm-run -q -p --no-shell <vm> top -b -n 2 -d 1 -w -o %CPU` by a
background poller (the selected qube only, one fetch at a time, ~2 s apart).
`-n 2` is deliberate: the first batch frame's `%CPU` is an average since boot,
the second one's is a real delta over the 1 s interval — the same "recent
activity" meaning dom0's per-tick delta has.

This is the one place the monitor runs a command inside a guest, and the
DESIGN non-goal was amended for it rather than quietly broken: read-only, a
fixed argv, no shell (`--no-shell` means the guest execs `top` directly), the
qube name validated against the Qubes alphabet first, nothing written and
nothing installed. `tests/test_invariants.py` still fails on any *other*
`qvm-run` occurrence in the tree — the single permitted one is inside
`qvm_run_prefix`, the no-shell prefix this argv and the network chart's (D-27)
are both built on.

Two honest limits: the guest table is the guest's own `top` output — its
`%CPU` is a 1 s delta, so a process that peaks between polls can look idle —
and a guest without `top` (a Windows qube, a minimal template), or one whose
`qvm-run` fails, shows that reason in the panel instead of an empty list.
Batch-mode `top` prints the whole process table (measured: 493 rows on a
500-process host), so this is not a window onto the busiest few.

### Memory figures — corrected twice; this section is the second correction

The first version of this section claimed the memory units were unresolved. The
second claimed the API's `mem` was the live balloon and `mem_static_max` the
configured maximum, and divided by the latter. Both were wrong, and the owner's
second report exposed it: *"It's still 1000 MiB, you are reading initial
memory, not the assigned memory."*

What Qubes **4.3** actually does — three facts, each read from source:

1. `release4.3` `qubes/vm/qubesvm.py`: `get_mem()` returns
   `libvirt_domain.info()[1]`; `get_mem_static_max()` returns
   `libvirt_domain.maxMemory()`.
2. libvirt's libxl driver fills **both** from
   `virDomainDefGetMemoryTotal()` — the XML `<memory>` element.
3. `release4.3` `templates/libvirt/xen.xml` writes `<memory>` = `vm.memory`
   (the **initial** size) and `<maxMemory>` = `vm.maxmem` whenever
   `use_memory_hotplug`, which is the default for PVH/PV qubes with a Qubes
   kernel. Qubes also mirrors `maxmem*1024` into xenstore
   `memory/hotplug-max`.

So on a hotplug qube — which browser-streaming is — **both API fields carry the
initial memory**, and neither knows the configured maximum. That is why the
cell kept reading 1000 MiB no matter which of the two it divided by, and why
"used" could exceed it.

The memory figures now come from xenstore, on the handle the tick already
reads: **max** = `memory/hotplug-max`, else `memory/static-max`; **allocated
now** = `memory/target` (qmemman's live balloon); **used** = `memory/meminfo`.
The `CurrentState` values remain only as a fallback for a domain with no such
keys. The list therefore reads `1.1 / 3.9 GiB` for a qube configured at
4000 MiB, the detail pane's "allocated now" is genuinely the live allocation,
and the yellow "allocated" line in the memory chart shows how far qmemman has squeezed it.
See DESIGN.md D-14 (which supersedes D-13).

Two footnotes worth keeping: 4.3 defaults `maxmem` to **10×** the initial memory
(`default_maxmem = self.memory * 10`), which is what the original "10×
disagreement" really was; and `used` includes swap (D-1), so a heavily
swapping qube can still report more than its configured memory.

### qubes-systemlist's look and keys (D-23)

The window now looks and behaves like [qubes-systemlist](../qubes-systemlist)'s
list. It has no title bar and uses the stock Qubes menu's light or dark
stylesheet, with Nord stand-ins off dom0. On a compositing screen the
background is translucent. Both columns sit on systemlist's rounded inner pane,
and the selected row is the launcher red. The column titles are quiet grey
labels over a hairline, and the status line is systemlist's footer, red while
the collector is degraded. The search field has the red border, and the
**All qubes** check replaces the old "show halted" button. The large charts
use the same Nord tones (Frost CPU, green memory, yellow/red reference lines; D-24).

| key | |
|---|---|
| *typing* | goes into the search field, wherever focus is |
| Up / Down, Page Up / Page Down | move the selection |
| Ctrl+H | toggle *All qubes*: show halted qubes too |
| Escape | clear the search; a second Escape closes the window |
| Ctrl+Q | close the window |

Without a title bar, drag the window by its background (the margin, the gap
between the columns, an empty part of a pane) and resize it by its outer
6 px edge. Unlike systemlist, it stays in the taskbar, is not kept above other
windows and does not hide on focus loss: it is a monitor you leave open (D-8
still holds, so there is no resident process and no hotkey).

Verified off-box only: `tests/test_ui_wiring.py` (the controls, the footer's
error state, clear/toggle/move, and every colour name `qubes-top.css` uses is
defined), `tests/test_ui_model.py` (`step_selection`) and
`tests/test_ui_layout.py` (`edge_at`). How it looks, the translucency and the
drag/resize can only be judged on dom0.

### Graphs you can read (D-24)

Every graph, large or small, now uses the same **fixed five-minute window**
with "now" at the right edge. Before, a CPU graph spread whatever history it
had over the whole width, so it stretched while the history filled, and the
memory graph used a time scale of its own. Now the same second is the same
spot in every graph. A qube you have watched for a minute has a line in the
right fifth only, and that is the truth.

The large charts have:

- a y-axis with rounded gridlines and values (`0% … 200%`, `0 GiB … 4 GiB`,
  MiB below 1 GiB);
- minute ticks underneath (`−5 min … now`);
- a 2 px line over a faint wash of its colour, with a dot on the newest value,
  and the newest value written top right;
- for CPU, the 100% line (one full core) a step stronger than the gridlines;
- for memory, the live allocation and the configured max as named yellow and
  red lines with a legend (`used`, `allocated`, `max`), instead of the faint
  bands.

Chart text uses the theme's own text colour, so it follows a light theme too.
The row sparklines draw on the same window with a heavier line and the same
wash, and they keep their centred scale label (D-21).

Verified off-box: `tests/test_ui_core_geometry.py` (the window, the steps, the
ticks) and `tests/test_ui_wiring.py` (the axis labels, legend and newest value
are drawn). It has not been checked on dom0.

### Room for five to six qubes (D-25)

The window now opens 760 px tall instead of 640. On a shorter screen it opens
smaller, so it never runs past the bottom of the screen. The qube list keeps
room for at least five rows even when the info box and the charts open under
it: if they would squeeze it, the window grows instead. Six rows at the default
size is an estimate from the stock metrics and has not been measured on dom0.

### Renamed from qvm-top (D-26)

The program was called `qvm-top`. In Qubes, `qvm-*` names are dom0
command-line tools that act on qubes, and graphical programs are `qubes-*`,
like this project's siblings (qubes-systemlist, qubes-settings, …). The
package is now `qubes_top`, the command `qubes-top`, and the menu entry
*Qubes Top*. Running `install-on-dom0.sh` removes an install under the old
name and moves the saved divider position from `~/.config/qvm-top/` to
`~/.config/qubes-top/`.

### Disk and network graphs (D-27)

Owner: *"Det skal kun være for den qube der er valgt, og det skal bruges til at
lave en graf. De to store grafer med CPU og ram kan laves mindre så der er plads
til 4 grafer i samme område."*

The charts under the list are now a 2 × 2 grid of equal columns:

| | left | right |
|---|---|---|
| top | CPU — % of one core | memory |
| bottom | disk I/O — read / written per second | network — in / out per second |

Each chart keeps its 120 px height and gets half the list's width, so the block
is as tall as the two stacked charts were and the list keeps its five rows
(D-25). All four share D-24's five-minute window, so a disk burst lines up with
the CPU spike above it second for second.

The two I/O charts draw two lines each, purple for read / in and orange for
written / out, with the newest rates in the legend (`read 300.0 KiB/s`). The
y labels use a one-letter unit (`500K`, `2M`) to fit the narrower gutter, and
the axis never tops out below 4 KiB/s, so an idle qube's few hundred bytes a
second do not fill the chart and look busy. An empty chart says why in words:
*not measured for dom0*, *no netvm: no network to measure*, *not running*, or
the netvm's own error.

Where the numbers come from, and why they are honest:

- **Disk** is read in dom0 itself. Qubes serves every qube's virtual disks from
  dom0 (blkback), and the kernel counts each one under
  `/sys/bus/xen-backend/devices/vbd-<domid>-<devid>/statistics/` — the same
  files `xentop` reads. qubes-top sums `rd_sect` and `wr_sect` (512-byte
  sectors) over the qube's disks once per tick. No command runs, and the guest
  cannot change these numbers.
- **Network** cannot be read in dom0. A qube's vif backend lives in its netvm
  (usually sys-firewall), which is why `xentop`'s NETRX/NETTX show 0 on Qubes.
  qubes-top reads it there with
  `qvm-run -q -p --no-shell <netvm> cat /sys/class/net/vif<domid>.0/statistics/{rx,tx}_bytes`,
  on a background poller, once per tick, for the selected qube only. The
  backend's rx is what the qube *sent*, so the two are swapped to the qube's
  side. The netvm writes this output, so only plain digits of a u64's length
  are accepted (D-6).

Counters become rates per tick. A counter that goes down (a re-attached disk, a
recreated vif) or a new domid (a restarted qube) is a gap in the line, never a
negative or a fake zero (D-5). The history covers the selected qube only and
starts over when you select another one: it is only sampled while selected,
so keeping an older stretch would draw it as if nothing happened in between.
dom0 shows neither chart — its disks are not blkback devices and it has no
network.

Verified off-box: `tests/test_ui_iostats.py` (the sysfs layout, the argv, the
rx/tx swap, the digit check, the rate and gap rules) and `tests/test_ui_wiring.py`
(four charts in the grid, the I/O axis, legend and empty-chart text). **Not yet
checked on dom0**: whether the vif is named `vif<domid>.0` in your netvm,
whether a non-root user can read the `vbd-*` statistics, and how the half-width
charts read with the real theme.

## Running it

```sh
bin/qubes-top                     # the window, straight from a checkout
python3 -m qubes_top --gui        # the window (dom0, graphical session)
python3 -m qubes_top --probe      # data-path check: PASS/FAIL rows, no GUI
python3 -m qubes_top --stage      # the full staged test on this host
python3 -m qubes_top --api-audit  # every gi symbol the UI calls, resolved here
```

`bin/qubes-top` puts the tree on `PYTHONPATH` and runs the module, the same
way qubes-cpu-affinity's `bin/qubes-vcpu` does; it takes the same flags. The
installer links `~/.local/bin/qubes-top` to it and the menu entry runs it.

`packaging/` holds wrappers for the staged probe and the staged test, a
launcher, a `.desktop` file, the copy-in notes, and `install-on-dom0.sh`,
which installs or updates qubes-top on dom0 from the git server via a staging
qube (see `packaging/README.md`).

## Running the tests (dev box, stdlib only)

```
python3 run_tests.py
```

Twenty-seven modules, green off-box; the guards are mutation-proven
(re-introducing the bug in a throwaway copy fails the specific assertion). The
runner executes each `tests/test_*.py` as its own script in a fresh
interpreter, each module carrying its own `sys.path` shim, so `import qubes_top`
works regardless of where it is run from. `unittest discover` is deliberately
not used: it refuses a start directory that is not an importable package and
reports it opaquely.

Two habits the codebase encodes, both learned the hard way: every fake must be
*narrower* than the real library (a stub that offers a method the real client
lacks certifies the bug instead of catching it), and anything only `gi` or a
real Qubes can answer is either verified on dom0 or not claimed at all — hence
`--api-audit` and the staged test.

## Layout

```
qubes_top/
├── __init__.py      inert: imports nothing
├── core/            pure python; no gi/qubesadmin imports
├── dom0/            adapters (qubesadmin, read-only ctypes libxenstore) + staged probe + staged test
└── ui/              GTK3 frontend (gi only) + api_audit.py (static gi check)
bin/qubes-top        start the app from the tree (the installed PATH link and the menu entry point here)
tests/               stdlib unittest modules, each with its own sys.path shim
packaging/           staged dom0 probe + staged test + launcher wrapper + .desktop + dom0 installer + copy-in docs
```

Import contract: nothing in `qubes_top/core/` or in `qubes_top/__init__.py` imports
`gi` or `qubesadmin` — enforced by an AST test, not by text matching
(docstrings may legitimately mention the names).

DESIGN.md holds the frozen metric contract, the decision table, the open
questions, and the milestone gates.
