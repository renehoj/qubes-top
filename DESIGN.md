# qubes-top — design note

**Status:** M0 foundation, written 2026-10-17 on dev box `ai`.
**Environment fact (probed, §9):** this box has no gi, no qubesadmin, no
`/etc/qubes-release`, no xl/xenstore. Nothing GUI-side or dom0-side has been
executed on this box. The README states the same for the GUI: it has never
executed anywhere until the owner runs it in dom0.

## 1. What the system is

A GTK3 window app that runs in Qubes dom0 — "top for Qubes":

- **Left pane:** every *running* qube as a six-column table — name (with the
  label-colour dot), status, CPU %, the CPU sparkline, memory used / assigned
  in GiB, the memory sparkline. Ordered by name by default; the CPU and name
  headings re-sort. The column widths are shared by the header and every row
  through a `Gtk.SizeGroup`, and the header reserves the vertical scrollbar's
  width so the columns stay aligned when the list scrolls.
- **Right pane:** detail for the selected qube, including a process table:
  dom0's own `/proc` when the host is selected, and the selected guest's own
  `top` (via `qvm-run`, D-15) when a guest is.

### Non-goals (frozen)

- No guest-side *installed* code, no helper service, no root: read-only, as
  the invoking dom0 user, and nothing is written into a guest. **One
  deliberate exception (D-15):** the selected guest's process table is read by
  running the guest's own `top` through `qvm-run` — a fixed argv, no shell, no
  writes, nothing installed.
- **No xenstore writes, ever.** The dom0 user is in group `qubes` and the
  socket is `srw-rw---- root:qubes`, so write capability *exists* and must
  stay unused (frozen by adapter card t_e55e68ef).
- No command-execution mode *for the user*: there is no "run this" box, and
  the only command the app ever runs is the fixed `top` argv above.
- No resident dbus singleton, no hotkey summoning: plain foreground window
  (D-8).
- No network: the app reads local dom0 state only.

## 2. Frozen metric contract — measured, recorded verbatim

Do not re-derive. Do not "improve". (Source: task card, from live dom0
measurements.)

| Metric | Source | Note |
|---|---|---|
| used | xenstore `/local/domain/<id>/memory/meminfo` | KiB; written by the qube's meminfo-writer; **30 MiB deadband**; includes swap |
| allocated now | xenstore `memory/target` | KiB; qmemman's live balloon target — the only "what it currently has" figure Qubes 4.3 exposes (D-14) |
| max (configured) | xenstore `memory/hotplug-max`, else `memory/static-max` | KiB; what Settings shows as **Max memory**; the memory column's denominator (D-14) |
| swap | xenstore `memory/swapinfo` | |

1. **Never use xenstore `target` as the memory denominator.** qmemman writes
   it as `xen_target/1024 - 16*1024` on purpose ("VM sees about 16MB memory
   less"). A qube at full allocation shows `static-max - target == 16384` KiB
   exactly, so target-as-assigned pins every healthy qube at ~99.6%. It is
   still the right value for "what it is holding right now" (D-14); it is
   never the yardstick to divide "used" by.
2. Memory is a **step series** at 30 MiB quantisation; CPU (`cpu_time` delta)
   is a **continuous** series. % of ONE core, may exceed 100.
3. **Mute case:** with no meminfo, qubesd reports used == assigned. Render
   "not reported", never 100%.
4. `cpu_time` resets when a qube restarts; **domids are reused** — re-resolve
   name→domid every sample.
5. Write `/local/domain/<id>/memory/meminfo` is guest-authored and untrusted:
   ASCII digits only.

## 3. Name collision check — recorded 2026-10-17 (dev box `ai`)

| Check | Result |
|---|---|
| `command -v qvm-top` | not found (`shutil.which` → None) |
| `qvm-*` binaries on PATH | none |
| `qvm-top` in `qubesadmin/tools` | **checked upstream 2026-10-17 by the orchestrator**: the `qubesadmin/tools` listing on `QubesOS/qubes-core-admin-client@master` carries 30 tools (`qvm_check`, `qvm_clone`, `qvm_ls`, `qvm_run`, …) and **no `qvm_top`**; both `raw.githubusercontent.com` and `api.github.com` were reachable from this box. Public web search also finds no Qubes tool named `qvm-top` (only an unrelated S&P index). |

**Verdict: name free on this box; the upstream re-check is a mandatory step of
the M2 gate** (list the installed qubesadmin's tools directory on dom0 and
confirm `qvm-top` absent). If it turns out to be taken: rename before any code
lands — only `packaging/` names the binary, so the rename is a one-line
change.

**Re-check for the new name, 2026-10-29 (D-26):** the project is now
`qubes-top`. The `qubesadmin/tools` listing on
`QubesOS/qubes-core-admin-client@main` (`api.github.com`) carries 30 tool
modules; `qubes_prefs` is the only `qubes_*` module, and there is **no
`qubes_top`**. The dom0 probe's `name_free_check` now looks for `qubes_top` /
`qubes-top` in the installed tools directory, so the M2 gate keeps checking
the name that ships.

## 4. Repo layout (frozen)

```
/home/riley/qubes-top/
├── DESIGN.md            this note
├── README.md            status + how to run tests
├── run_tests.py         per-module script runner (stdlib only)
├── qubes_top/
│   ├── __init__.py      inert: imports nothing
│   ├── core/            pure python; no gi/qubesadmin imports
│   ├── dom0/            adapters (qubesadmin, read-only ctypes libxenstore)
│   └── ui/              GTK3 frontend (gi only; pin Gtk 3.0 + Gdk 3.0, Gtk first)
├── tests/               stdlib unittest modules, each with its own sys.path shim
└── packaging/           placeholder for copy-in script + install docs (M3)
```

Import contract (enforced by the AST guard in `tests/test_import_guard.py`):

- Nothing in `qubes_top/core/` imports `gi` or `qubesadmin` (or xdg).
- `qubes_top/__init__.py` imports nothing at all — one subpackage import there
  and the package stops importing on a gi-less box.
- `dom0/` and `ui/` may import the heavy dependencies; neither may be imported
  by `__init__.py` or `core/`.
- Data flow (the real seams): `dom0/` reads raw values per sample → hands them
  to the `core/` model (which owns the per-qube ring buffers — frozen,
  t_c2120185) → `ui/` renders the `core/` series and selection state.

## 5. Frozen decisions

| ID | Decision | Options | Why | Reopen if |
|----|----------|---------|-----|-----------|
| D-1 | Metric sources per §2 table | — (frozen by measurement) | measured on dom0; re-derivation risks re-encoding the 16 MiB target offset | never, absent new live measurement |
| D-2 | `assigned` = `admin.vm.CurrentState.mem` (qubesadmin `get_mem()`); never xenstore `target` (amended 2026-10-18: `mem` is the **live allocation**, not the configured memory — see D-13) | target / CurrentState | `target` is `xen_target/1024 - 16*1024` by design; target-as-assigned pins every healthy qube at ~99.6% | never |
| D-3 | Memory: step series, 30 MiB quantisation, points carry the timestamp of observed change. CPU: continuous `cpu_time` delta, % of ONE core (may exceed 100) | continuous both / step both | meminfo-writer quantises in 30 MiB; `cpu_time` is a cumulative ns counter | never (measured) |
| D-4 | Mute case (no meminfo, or used == assigned to the KiB) → render "not reported", never 100%. State machine REPORTED / MUTE / UNKNOWN | default to 100% / bare dash | qubesd's fallback value is indistinguishable from a genuinely full allocation; synthesising 100% lies | never |
| D-5 | Re-resolve name→domid every sample (domids are reused across restarts); on `cpu_time` reset emit a gap — never a negative, never a zero | trust a domid cache | a reused domid would attach a restarted qube's history to its previous owner silently; a reset delta is negative | never (measured) |
| D-6 | Parse guest-authored meminfo/swapinfo as ASCII digits only, reject everything else | trust the string / float parse | values are guest-authored and untrusted; upstream does exactly this | never |
| D-7 | Layout + import contract of §4 | monolith / split | keeps the core importable and testable off-box; the guard is AST, not text | owner wants different layering |
| D-8 | Plain foreground window; no resident dbus singleton, no hotkey | resident singleton (dom0 launcher skill shape) / plain window | "top" is a window the user opens; simplest path, fewest moving parts; no hotkey is requested by the card | owner wants a hotkey summon |
| D-9 | Test runner: run each `tests/test_*.py` as a standalone script with its own `sys.path` shim, stdlib only | `unittest discover` | discover refuses a non-importable start dir and reports it opaquely; per-module execution survives a partial copy | never |
| D-10 | Sample cadence 2 s; ring capacity 150 points = 5 min window | 1 s / 5 s | frozen by the core card's API (t_c2120185); the UI sparkline draws the ring directly | owner wants a different cadence (a parameter, not a redesign) |
| D-11 | dom0 adapters read-only: one reused libxenstore handle, no fork per qube per tick, no `xs_write`/`xs_rm`; qubesadmin adapter constructs with the **top-level** `qubesadmin.Qubes()` attribute (`qubesadmin.app.Qubes` does not exist) and skips stubdomains | per-call fork / helper service | the monitor must not perturb what it measures; the `app.Qubes` bug killed the previous project's first live run while 575 tests passed | never |
| D-12 | Left pane is a six-column table (name, status, CPU %, CPU graph, memory, memory graph), **name ascending by default**; memory is rendered in GiB with one unit for the used/assigned pair and a dash for "not reported" (the prose stays in the detail pane); a one-point memory series (no change past the 30 MiB deadband, D-3) draws a flat line, never a dot; the memory sparkline is scaled to the qube's own ceiling (max, else assigned), not to its observed maximum | a free-form row, CPU-descending default, per-value units, prose in the cell | a list of qubes is scanned for a name, and a CPU-sorted list reorders under the pointer every 2 s; mixed units make the two halves of the memory cell incomparable; a lone dot (or a flat line pinned to the top by auto-scaling) reads as a broken or full graph | owner wants a different default order or more columns |
| D-13 | **SUPERSEDED 2026-10-18 BY D-14** (same day: its evidence was read from qubes-core-admin `main`, but the target runs `release4.3`, where the two fields mean something else). Left for the history: it decided the memory column divides by `mem_static_max` and that `CurrentState.mem` is the live balloon | divide by the live allocation / divide by `mem_static_max` | on 4.3 those two fields are the *same value*, so this changed nothing on the target | — |
| D-14 | Memory sources, for **Qubes 4.3**: the denominator is the configured maximum from xenstore (`memory/hotplug-max`, else `memory/static-max`); "allocated now" is xenstore `memory/target`; `used` is xenstore `meminfo`. `admin.vm.CurrentState`'s `mem` and `mem_static_max` are **not** used for either figure (fallback only). Ratified 2026-10-18 from the owner's second report — *"It's still 1000 MiB, you are reading initial memory, not the assigned memory"* — with the three upstream facts that prove it: (1) `release4.3` `qubes/vm/qubesvm.py` — `get_mem()` = `libvirt_domain.info()[1]`, `get_mem_static_max()` = `libvirt_domain.maxMemory()`; (2) libvirt's libxl driver fills **both** from `virDomainDefGetMemoryTotal()`, i.e. the XML `<memory>` element; (3) `release4.3` `templates/libvirt/xen.xml` writes `<memory>` = `vm.memory` (**initial**) and `<maxMemory>` = `vm.maxmem` whenever `use_memory_hotplug` (the 4.3 default for PVH/PV qubes with a Qubes kernel), and `qubes/vm/qubesvm.py` mirrors `maxmem*1024` into xenstore `memory/hotplug-max`. So on a hotplug qube both API fields carry the **initial** memory — a 4 GiB qube reads 1000 MiB — and neither knows the real maximum | keep dividing by the API figure (initial) / read `vm.maxmem` per qube every tick (an extra qubesd round trip per qube) / read xenstore | the API cannot express the configured maximum on 4.3; the property would cost N RPCs per tick, while the two xenstore keys ride the handle the tick already reads; the old figure also made "used" exceed "total" (initial) for any qube qmemman had grown | never (source-level fact, pinned by tests); revisit the *labels* if Qubes exposes the live balloon through the API again |
| D-15 | The selected guest's process table is read by running the guest's own `top -b -n 2 -d 1 -w -o %CPU` through `qvm-run -q -p --no-shell <vm>`, from one background poller, for the selected qube only. Read-only, fixed argv, no shell, the qube name validated against the Qubes alphabet. This is the deliberate exception to the "no command execution" non-goal, granted by the owner on 2026-10-18 ("qvm-run -p <vm> 'top -b -w' will give you the top info") | a guest-side qrexec agent (installs code in every guest) / `ps` (a lifetime-average %CPU under the same "%CPU" heading as dom0's tick delta) / no guest process table | `qvm-run` is the only way to see a guest's processes without installing anything; `--no-shell` removes the injection surface; the **second** `top` frame is a real recent %CPU (the first is an average since boot); batch mode prints the whole table (measured 493 rows), so the reading is complete rather than the busiest few; one process per ~2 s for one qube, not a fleet-wide sweep | owner wants per-tick tables for every qube (a different cost model) or accepts a qrexec agent |
| D-16 | The process table is a **column table on the same mechanism as the qube list** (D-12): `PROC_COLUMNS` in `ui/layout.py` (PID, NAME, %CPU, RSS) is the single description; the header and every row build from it; one `Gtk.SizeGroup` per column is shared by the header cell and all row cells; the sortable titles are labels in `EventBox`es (not `Gtk.Button`s) with a fixed-width arrow slot; the header sits outside the scroller and reserves the scrollbar's width. Clicking a title sorts; the toolbar buttons that used to do it are gone. Ratified 2026-10-18 from the owner's report *"can you turn the process list into columns so the values are aligned"* | keep the three toolbar buttons and the hand-built header labels, and add per-row padding to fake the alignment / a real `Gtk.TreeView` | the three defects behind the report are all structural: a title cannot sit over a column it is not in (buttons in a separate toolbar), a button's theme padding offsets its text from the values below it, and a header outside a scroller is ~14 px wider than the rows the moment the vertical bar appears — which on a ~490-row table is always. A TreeView would fix alignment but replaces the in-place row reuse and the monospace/ellipsis rules the qube list already proved on the target | the row count per tick grows far past a few thousand (then a TreeView/`Gtk.ListBox` with a model pays off) |
| D-17 | The two large history charts are **under the qube list**, in a fixed-height block below it (`ui/charts.py`, `HistoryCharts`), not at the top of the detail pane. They render the detail pane's `DetailSpec` — the same object the pane refreshes in place, so one selection drives both — are hidden while nothing is selected, and stay visible but frozen when the selected qube disappears. The list keeps `expand`, so the charts cost exactly 2 × 100 px + the two frame labels. Ratified 2026-10-18 from the owner: *"move the right side memory and cpu graphs ... so when a VM is selected the large graphs are shown below the VM list"* | keep them in the detail pane above the facts and process table / give them their own window | the charts are the history of the **selected row**, so they belong beside the list; in the detail pane they pushed the process table — the one thing that should get every spare pixel — down. Splitting the widget out of `right_pane.py` also means the pane no longer owns two drawing areas it never reads, and the charts are fed from the same spec instead of a second copy of the data | the owner prefers them side by side (one row of two half-width charts, ~125 px total) rather than stacked; that is a change to `charts.py`'s box orientation only |
| D-18 | The detail pane's two label/value grids — template / netvm / vcpus / mem max, and the memory facts (allocated now, used, swap, report state, domid) — are one titled **info group box** under the qube list and **above the charts** (`ui/infobox.py`, `InfoBox`, field list in `FIELDS`). It renders the same `DetailSpec` with the same hide-until-selected / frozen-when-gone contract as D-17. What stays in the detail pane is the identity line (colour dot, name, state) and the process table. Ratified 2026-10-18 from the owner: *"Before the bottom graphs make a info group box, and move the info fields from the right side to the box"* | leave the grids in the detail pane / move them to a dialog or a tooltip | the fields describe the selected row and are read *with* the graphs, so they belong on the same side of the divider; and the pane's remaining job is the process table, which wants every pixel. The identity line stays above the table so the table always says which qube it belongs to | the owner wants the identity line moved too (one more block out of `_build_detail`) or the fields laid out one per row instead of two |
| D-19 | The process panel carries **no chrome**: no title bar ("processes — dom0" / "processes — guest (top, N s window)") and no "(click a column to re-sort)" hint in the table header. `set_proc_title` is gone from the pane and the app. Ratified 2026-10-18 from the owner: *"on the process list there is a title says processes - guest... you can remove that title. I the table header line it says click to sort you can remove that text as well"* | keep the title so the dom0/guest source is labelled / keep the hint as discoverability for the sort | the column titles are themselves the sort control (D-16), so the hint explains something visible; and the title cost a full row of height above a table that wants it. **Accepted cost:** the panel no longer says whether the table came from dom0's `/proc` or the guest's `top` — the only remaining tell is the absence note, which still names the guest-read ("reading processes from the guest…") when there is no table | the owner wants the source back as a one-word suffix on the identity line, or a column/row count in its place |
| D-20 | **Every dot — the row's and the detail identity line's — is the qube's real Qubes OS label colour**, not a hash. *(Amended by **D-22**: for Qubes' eight default labels the dot now paints the label's **Nord** tone. Everything below — the API chain, the cache, the fallbacks, the ring — is D-20's; only the colour source changed.)* Source: `vm.label` (resolved by `PropertyHolder.__getattr__` into a `label.Label` object via `admin.vm.property.Get label`) then `Label.color` (`admin.label.Get` → **`0xRRGGBB`**), read from `QubesOS/qubes-core-admin-client@release4.3`: `vm/__init__.py` defines no `label` at all, `base.py` `_parse_type_value` returns `app.labels.get_blind(value)` for a `type=label` property and `label.py` caches the colour RPC on the instance, which `get_blind` shares per name. `qubes_top/ui/labelcolors.py` caches name→colour, re-reads the domain list on a **60 s TTL** and immediately for a name it has never seen, and falls back to `core.label_color` (crc32) for anything unreadable — a dot is always drawn, never a crash. **The colour format is corrected below (amended 2026-10-18, same day, after the owner's report):** qubesd serialises labels as `0xRRGGBB` (`qubes-core-admin@release4.3` `qubes/app.py`: `Label(3, "0xedd400", "yellow")`; `admin.api.admin.label_get` returns `label.color` verbatim), while a widget needs `#rrggbb` — `core.parse_color` accepts both and normalises to `#`, and rejects anything that is neither *before* it reaches `Gdk.RGBA.parse` (which reports failure by painting black, not by raising). The first version accepted only `#rrggbb`, so on the target **every** colour was rejected and every dot silently fell back to the hash: the owner's *"if the label has the text yellow the dot should have the colot yellow"* was exactly that failure. The dot gains a faint grey ring so a `black` label (dom0's default) is visible on a dark theme. Ratified 2026-10-18 from the owner: *"The dot should be the Qubes OS domain color for the VM, it makes more sense then a random color"* | keep the deterministic crc32 hash (the v1 behaviour, and what the `color_for` seam defaulted to) / read `app.labels[...]` up front and map names ourselves / derive the colour from the label *name* (`yellow` → yellow) | the label **is** the security classification the user already reads by reflex everywhere else in Qubes (red = untrusted, purple = work); a hash dresses an arbitrary colour in the same visual language and teaches the wrong one. The colour cannot ride the existing per-tick read: `CurrentState` carries no label, so this costs N `admin.vm.property.Get` calls per pass — bounded by the TTL, i.e. N per minute against the sampler's N per 2 s tick, and **zero** on the per-row path. Mapping the *name* to a hardcoded palette was rejected: qubesd's value is authoritative (it carries the 0x555753→0x555555 grey migration and any custom colour), and the name is only coincidentally a colour | Qubes exposes the label through `CurrentState` (then it rides the tick and the TTL can go), or the owner wants label changes reflected live — then drop the TTL to a few seconds or invalidate on the `domain-pre-delete`/`property-set:label` events instead of polling |
| D-21 | **Every sparkline states its own interval**: a centred label in each row graph carrying the **upper bound the graph is drawn against** (`RowSpec.cpu_top_text` = the series maximum from `own_bounds`; `RowSpec.mem_top_text` = the ceiling the memory step is scaled to), drawn by `left_pane.draw_graph_top_label` at 9 px, near-white over a 4-way dark halo (`set_font_size` / `text_extents` / `show_text`, centred on the glyphs' ink). Empty string — no label — when the series has nothing to draw (no CPU value yet, no memory point yet). `--api-audit` grew `audit_cairo`, which resolves every `cr.<method>` against the host's real `cairo.Context`. Ratified 2026-10-18 from the owner: *"Can you add text to the small graph? If there was a center text with the highest value from the data set it would make it easier to read. As an example if it says 10% it would mean the graph currently has 0-10 as the interval."* | keep the auto-scaling mute / re-scale the CPU graph to a fixed 0–100 instead of labelling it / a Pango layout for the text | per-row auto-scaling was a deliberate D-12 choice (a 30 MiB step on a 400 MiB qube must stay visible) but it made the CPU graph's height mean *variability*, not load: a qube idling at 3% and one pegged at 400% drew the same shape. The owner's label is the cheaper and more informative half of that fix — it keeps the useful scaling and makes the interval explicit. The text is the bound the geometry uses, **not** a re-derived number, so the label cannot describe an interval the graph is not drawn against (hence memory labels its ceiling, not its observed maximum). Cairo toy-text rather than Pango: four pycairo calls that have existed since cairo 1.0, versus a pile of gi names that cannot be executed off-box — the exact failure mode of `get_pango_attributes`. The halo exists because a single grey pass over a bright polyline read as hard to see in the row mock, and it needs no theme knowledge (unlike a filled backing box). Drawn last and wrapped — which is right (a label is not worth a blank row) and is *why* the cairo audit exists: a swallowed cairo error would otherwise draw nothing, silently | the owner prefers a fixed 0–100 CPU scale (the label becomes redundant and can go), or the label is still illegible on the real theme — then move it to a corner, or give it a filled backing in the widget's own background colour |

| D-22 | **The dot paints the label's Nord tone, not Qubes' saturated one.** `core.NORD_LABEL_COLORS` maps Qubes' eight default labels onto the official Nord palette (`nordtheme/nord@develop` `src/nord.scss`): red→nord11, orange→nord12, yellow→nord13, green→nord14, purple→nord15 (Aurora one-for-one), blue→nord10 (Frost; nord9/nord8 read cyan beside green), black→**nord1** (nord0 is the Nord *background* — a dot of it vanishes), gray→**nord4** (Nord has no neutral grey; nord3 would land one step from black's nord1 and the two 14 px dots would be indistinguishable). `core.dot_color_for_label(name, qubesd_color)` is the single rule: a default label is answered from the map and **needs no `admin.label.Get` call at all**, so the common case costs no colour RPC and a refused RPC cannot blank a known label; a *custom* label falls back to the colour qubesd reports, normalised by `parse_color`. Ratified 2026-10-18 from the owner: *"can you find the nord color palette and pick nord color for the dots"* | keep the literal Qubes colours (D-20 as first shipped) / derive a Nord tone by desaturating the Qubes colour programmatically / a light-vs-Nord theme switch | the dom0 desktop is Nord-themed and the saturated Qubes palette (red `#cc0000`, yellow `#edd400`) clashes with it; Aurora happens to carry five of the eight labels one-for-one, so the mapping keeps its meaning (red still reads as red) while matching the theme. It is also *cheaper* than D-20 as shipped: no colour RPC for the eight defaults. The two neutrals are the only opinionated entries and are documented above precisely because Nord has no neutral grey | the owner finds blue/gray/black too close on the real screen — the single-word fix is gray→nord3, or black→nord0 with the ring carrying the visibility; or wants the literal Qubes colours back (delete the map: D-20's path is untouched), or wants the big charts re-toned too (they are not themed by this row) |
| D-23 | **qubes-systemlist's look and keys.** `ui/theme.py` loads the stock Qubes menu stylesheet (`qubes_menu`'s `qubes-menu-light.css` / `qubes-menu-dark.css`, chosen by the theme's light/dark intensity) or, off dom0, `ui/fallback-theme.css` (the same colour names in Nord). It then loads `ui/qubes-top.css`, which is systemlist's `systemlist.css` adapted to this window: `#qubes-top-window` translucent at 0.92 on a compositing screen, both columns on the rounded `.qubes-top-pane`, the launcher red `#BF616A` at 35% for the selected row, quiet `@buttons-bg` column titles over a `@separator` hairline, systemlist's `.search_entry` and `.footer` (red `.error` while a frame is degraded). The window is undecorated (`theme.set_chrome`). It is moved by dragging its own surface and resized from a 6 px edge (`layout.edge_at`). The keys are systemlist's list keys: typing searches, Up/Down/Page Up/Page Down step the selection (`LeftPaneModel.step_selection`, clamped, starting from the visible end), Ctrl+H toggles *All qubes*, Escape clears and then closes, Ctrl+Q closes. The cairo colours come from `theme.*_RGBA` (Nord), which answers D-22's "re-tone the charts" revisit note. Ratified 2026-10-29 from the owner: *"Skriv projektet om så det har samme 'look and feel' som qubes-systemlist"*, with "look + keyboard, undecorated, but in the taskbar, not kept above, not hidden on focus loss" chosen over the full launcher chrome and over the look alone | full launcher chrome (keep-above, skip-taskbar, hide on focus loss) / the look only, keeping the title bar and the old controls | the owner's Qubes tools now share one visual language (launcher, systemlist, settings, firewall …), and a window that looks different reads as a different, foreign tool. The *behaviour* of a launcher is wrong for a monitor: a window that hides when you click the qube you are watching cannot be watched, so D-8 is kept and only the look and the list keys are borrowed. The theme code is systemlist's, line for line where it can be, so a fix in one carries to the other. A move/resize grip replaces the title bar. Without it an undecorated window could only be moved with the window manager's Alt-drag, which nobody discovers | the owner wants the full launcher behaviour after all (then D-8 goes too: resident instance, D-Bus activation, a hotkey), or the stock stylesheet changes its colour names (the off-box guard checks `qubes-top.css` against `fallback-theme.css` only) |
| D-24 | **Graphs drawn to be read.** Every graph draws against one **fixed five-minute window, now at the right edge**: `core.window_segments` (CPU: the newest value at the right, each older one 2 s (`TICK_S`) further left, values older than `HISTORY_WINDOW_S` dropped) and `core.window_steps` (memory: each level held to its change and the last held to now; the level in force when the window opens, or the first level when the series began inside it, enters at the left edge, so a steady qube stays a flat line, never a dot). These replace `x_positions` / `polyline_segments` / `step_vertices`. The large charts (`CHART_H` 100 → 120) get a left gutter of y labels on rounded gridlines (`core.axis_ticks`: steps of 1/2/2.5/5 × 10^k, at most four intervals, the top rounded up to a whole step; memory in GiB from 1 GiB, MiB below) and minute ticks underneath. The series is a 2 px round-joined line over a 14 % wash with a dot on the newest value, which is also written top right. CPU's 100 % line is stronger than the gridlines. Memory's live allocation and max are 1 px yellow and red **lines** with a legend (used / allocated / max) instead of 35 % bands. Chart text and gridlines take the theme's text colour (`charts._ink`). The row sparklines use the same window, a 1.5 px line and an 18 % wash, and keep D-21's centred scale label. Owner, 2026-10-29: *"kan du forbedre graferne? de er ikke pæne, og er heller ikke så nemme at læse"* | keep the stretch-to-fit x placement and only restyle / a time axis per chart / bars for CPU | the stretch was the main reading problem: while the ring filled (the first five minutes of every session, and every newly started qube), a graph's x-scale changed every tick, so a shape could not be read as time. Memory, mapped over its own first-to-last change, was on a different scale from the CPU graph right above it. A fixed window is how top-style monitors draw history, and it lines the two charts up second for second. Axes turn "high" into a number; the newest value saves reading the right edge against the gutter. Named lines replace bands because a 35 % band of a series colour read as a second series. Restyling alone would have kept the stretch | the owner wants a longer or selectable window (`HISTORY_WINDOW_S` and the ring capacity must move together, D-10), or the first memory level drawn back to the left edge reads as invented history: then start it at its own time and accept a short line |
| D-25 | **The qube list shows five to six rows with a qube selected.** The default window height goes from 640 to `layout.WINDOW_HEIGHT` = 760 (lowered to the monitor's work area minus 40 px on a shorter screen, never below 480), and the list's scroller gets a minimum content height of `layout.list_min_height()` = `LIST_MIN_ROWS` (5) × (`GRAPH_HEIGHT` + `ROW_SPACING`) + padding, so the info box and the charts that open on a selection cannot squeeze it below five rows. When they would, GTK grows the window instead. Owner, 2026-10-29: *"man kan se ca. 2.5 qubes i listen hvor man vælger dem, man skal kunne se 5-6"* | only raise the default height / persist the window size | the default alone is lost to a manual resize, and D-24's taller charts (120 px) took more of the column; the minimum is what keeps the list usable whatever happens around it. 760 is the estimate for about six rows at the stock theme's metrics; it has not been measured on dom0 | the real theme's row or frame heights differ enough that 760 shows fewer than five rows (raise `WINDOW_HEIGHT`), or the owner wants the size remembered like the divider |
| D-26 | **The program is called `qubes-top`, not `qvm-top`.** Package `qubes_top`, launcher `packaging/qubes-top-run.sh`, PATH wrapper `~/.local/bin/qubes-top`, menu entry `qubes-top.desktop` (shown as *Qubes Top*), stylesheet `qubes-top.css` with `#qubes-top-window` / `.qubes-top-pane`, config `~/.config/qubes-top/`, install tree `~/.local/share/qubes-top`, repository `homelab/qubes-top`. `install-on-dom0.sh` retires an install under the old name: it removes the old tree and menu entry, removes the old wrapper only when it is the one the script wrote, and moves `~/.config/qvm-top/` to the new name when the new one does not exist yet. The historical records in §3 keep the old name, because they record what was checked then. Owner, 2026-10-29, asked which name fits Qubes OS naming and chose the rename | keep `qvm-top` | Qubes uses `qvm-*` for dom0 command-line tools that act on qubes (`qvm-ls`, `qvm-start`, `qvm-prefs`, from qubes-core-admin-client) and `qubes-*` for graphical programs and system components (`qubes-qube-manager`, `qubes-vm-settings`, `qubes-global-config`). This is a GTK window that takes no VM argument. Its siblings are already `qubes-systemlist`, `qubes-settings`, `qubes-firewall` and the rest, and it now shares their look (D-23). A `qvm-*` name would also claim a spot in upstream's admin-client namespace | upstream ships a `qubes-top` (the probe's name check fails): rename again before installing |
| D-27 | **Disk and network I/O of the selected qube, and four charts in the room of two.** The charts under the list become a 2 × 2 grid of equal columns (`ui/charts.py`): CPU and memory on top, **disk I/O** and **network** under them, each at the unchanged `CHART_H`, so the block is as tall as the two stacked charts were (half as wide each). Both I/O charts draw two rate lines (read / written, in / out; one Nord pair, purple and orange) on D-24's fixed window, with the newest rates in the legend, a one-letter unit on the y labels (`core.rate_unit`: `500K`) and an axis floor of 4 KiB/s so an idle qube does not read as busy; an empty chart writes why. **Disk** comes from dom0 itself: Qubes' blkback counts each frontend's disks in `/sys/bus/xen-backend/devices/vbd-<domid>-<devid>/statistics/{rd,wr}_sect` (512-byte sectors, the files `xentop` reads), summed over the qube's disks, once per tick, a partly readable sum refused. **Network** cannot come from dom0: the vif backend is in the qube's **netvm**, and dom0's `/proc/net/dev` (what `xentop` and libvirt's `interfaceStats` read) shows 0. It is read there by `qvm-run -q -p --no-shell <netvm> cat /sys/class/net/vif<domid>.0/statistics/{rx,tx}_bytes`, on a second `GuestProcPoller` at one read per tick, for the selected qube only — D-15's exception extended to a second fixed argv; both now build on one no-shell prefix, `guestproc.qvm_run_prefix`, which is the one place the invariant guard allows `qvm-run`. The netvm writes those numbers, so they pass D-6's digits-only check (and a u64 length cap); backend rx/tx are swapped to the qube's side. Counters become rates in `iostats.RateMeter` (a decrease, a new domid or an unreadable tick is a gap — D-5), and `iostats.IoHistory` keeps one value per frame for **one** qube and starts over on a new selection. dom0 shows neither (its disks are not blkback devices; it has no network). Owner, 2026-11-02: *"Det skal kun være for den qube der er valgt, og det skal bruges til at lave en graf. De to store grafer med CPU og ram kan laves mindre så der er plads til 4 grafer i samme område"* | four stacked charts at half height / I/O for every qube in the list / the guest's own `/proc/net/dev` / libxenstat | half-height charts (≈ 50 px of plot) cannot carry an axis and a legend; halving the width keeps D-24's readable axes and lines the four charts up two by two on one time axis. Selected-only bounds the cost to one sysfs walk and one `qvm-run` per tick, like the process table; keeping a history per qube would need every qube's counters every tick (a `qvm-run` per netvm per tick). The netvm's vif counters are the backend's view, which the qube cannot falsify; its own `/proc/net/dev` is guest-authored and would need a second `qvm-run` into every selected qube. libxenstat would add a C dependency for the same two sysfs files and would still read 0 for the network | the vif name or the `vbd-*` statistics are not what this assumes on the target (nothing here was measured on dom0 yet), a qube has more than one vif, the owner wants an I/O column in the list (then all qubes' counters every tick), or a non-root user cannot read the vbd statistics |

### Frozen interface boundaries (what downstream lanes code against — from cards t_c2120185 and t_e55e68ef, read 2026-10-17)

- **Sample record fields:** `name, domid, state, cpu_time_ns, mem_assigned_kib, mem_max_kib, mem_used_kib|None, swap_used_kib|None, ts`.
- **CPU delta:** `(cpu_time2 - cpu_time1) / dt_ns * 100` → % of one core; first
  sample has no value.
- **Ring buffers:** fixed capacity (150 @ 2 s), appended in place, dropped when
  the qube disappears, retained across selection changes.
- **State machine:** REPORTED (meminfo present) / MUTE (absent, or used ==
  assigned to the KiB) / UNKNOWN. Never synthesise 100%.
- **Adapter read set:** `admin.vm.CurrentState` once per qube per tick (one
  call returns all four), exposing `mem`, `mem_static_max`, `cputime`,
  `power_state` (qubesadmin, per running domain, stubdomains skipped);
  xenstore: `xs_directory("/local/domain")` + `xs_read` of `name`,
  `memory/meminfo`, `memory/swapinfo`.
- **Formatters** (bytes→MiB/GiB, % one decimal) live in `core/`, not in the
  UI, so they are testable off-box.

## 6. Open questions (deferred, with the cost stated)

| ID | Question | Cost of deferral |
|----|----------|------------------|
| Q-1 | Which power states get a row — `Running` only, or `Running` + `Paused` dimmed? The card says "every running qube"; `Paused` is undecided. | Low: the row filter is one predicate in `ui/`; `core/` carries `state` either way. Does not block M1/M2. |
| Q-2 | Packaging form on dom0: copy-in tarball + wrapper, or a pip user install? | `packaging/` stays a placeholder; no impact on M1–M2. Blocks M3 only. |
| Q-3 | Whether the row draws a separate swap sparkline. The row shows memory used (which includes swap per D-1); the detail pane shows the swap value broken out — that part is decided. | None: `core/` already carries a swap series. |

## 7. Verification path — what runs here vs what only dom0 can prove

**Runs on the dev box (the M0 gate; all of it executed and passed in this
commit):**

- `import qubes_top` on a box with no gi and no qubesadmin (the acceptance
  line; verified via the equivalent script form because this session's
  terminal blocks `python3 -c`).
- `run_tests.py` infrastructure: it runs, and its "no test modules found"
  failure mode is the expected state until the follow-up card below lands
  the first two modules.
- `python3 -m py_compile` over every file; pyflakes if installable.

**Lane boundary (recorded):** the architect lane's write guard blocks
`tests/*` (test modules are another lane's files) and blocks `git commit`
(staging is allowed; the commit write is not). The two M0 test modules
(import contract + AST import guard, each with its own `sys.path` shim) are
designed and fully specified by this lane and land via a child card to the
`core` profile; "suite green" and **the first commit + a clean tree** are
that card's gate, not M0's. The git side is already prepared: `git init
-b main`, repo-local identity (`Architect` / `architect@localhost`), and
the full tree staged in the index. When the modules land: `run_tests.py`
runs the full stdlib suite (each module in a fresh process), and one module
runs standalone from `/tmp` to prove the shim is layout-independent.

**Only dom0 can prove (staged handover, the only tier that counts):**

- **M2:** adapters import on the target's own interpreter; one real sample
  round-trip returns a complete record with None-safe mute values; grep proves
  zero write calls; the §3 tools-directory re-check passes. Failures require
  the **raw traceback** — paraphrasing loses the line that matters.
- **M3:** app starts in the foreground (raw traceback); window renders with
  real running qubes; the GTK 3.0 pin check passes; the 5-min history renders.
- Copy-in is a fresh extract (tarball with sha256), never an overlay onto an
  existing checkout — an overlay leaves deleted files behind and the staged
  test then runs against a union of two revisions.
- A run on the wrong host must not read as green: the staged check prints
  PASS/FAIL/NOTE lines plus an explicit "this is not dom0" branch when all
  Qubes imports fail.

**Honesty rule:** untested code is not working code. The README says, in its
first section, that no GUI code has ever executed anywhere until the owner
runs it in dom0 — and that the dom0 adapters are equally unexecuted.

## 8. Milestones and gates (each gate can fail)

| M | Work (card) | Gate | Fails on |
|---|-------------|------|----------|
| M0 | Foundation: repo, this note, README, layout, runner, git init + staged tree (this card); first commit lands with the test modules in the follow-up card (lane boundary, §7) | `import qubes_top` clean on a gi-less box; §3 check recorded; `run_tests.py` infrastructure in place (its "no test modules" state expected); git initialised, identity set, full tree staged | any import failure, a missing §3 record, a missing staged tree |
| M1 | Core sample model (t_c2120185) in `core/` | the card's unit list passes off-box (first sample, counter reset mid-series, `dt == 0`, deadband, exact-equality mute, ring eviction at capacity, qube removal, formatter rounding); `py_compile` + pyflakes clean; AST guard green | any test failure, a pyflakes finding, a banned import |
| M2 | dom0 adapters (t_e55e68ef) | staged dom0 check: target-interpreter import OK; one real round-trip complete and None-safe; grep: zero write calls; §3 re-check: `qubes-top` absent from the installed tools dir | import failure, an incomplete record, a write call, a name collision |
| M3 | UI + packaging | staged dom0 check: foreground start (raw traceback), window with real running qubes, GTK 3.0 pin check, 5-min history; fresh-extract copy-in | startup crash, missing rows, wrong namespace, stale overlay |

## 9. Findings (cited)

- **Environment probe, 2026-10-17, dev box `ai`** (probe script `qvm_probe.py`):
  gi / qubesadmin / qubes_menu / PyGObject all absent; `/etc/qubes-release`
  absent; `xl`, `xenstore-ls`, `xenstore` all absent; no `qvm-*` binary on
  PATH; `git 2.53.0`; `Python 3.14.7`.
- **Name search, 2026-10-17:** public web search for `"qvm-top"` found no Qubes
  tool by that name (only an unrelated S&P index). The orchestrator then
  enumerated `qubesadmin/tools` upstream directly (`api.github.com` listing of
  `QubesOS/qubes-core-admin-client@master`, 2026-10-17): 30 tool modules,
  **no `qvm_top`**, so the name is free against master. The M2 gate still
  re-checks the *installed* release on dom0, since master ≠ the shipped
  package (this box's egress can reach `raw.githubusercontent.com` and
  `api.github.com`; an earlier claim of a 404 was wrong).
- **Child cards read 2026-10-17:** t_c2120185 (frozen core API — §5
  boundaries), t_e55e68ef (frozen adapter contract — §5 boundaries).
- **Skills loaded before design:** `qubes-dom0-app-development`,
  `python-gtk3-apps`. Their test-runner and import-contract guidance is D-7 and
  D-9; their staged-handover recipe is §7.
