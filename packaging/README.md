# packaging/

Card t_2e8dda49 (M3). The app is a plain foreground window (DESIGN.md
D-8): no systemd unit, no dbus activation file, no autostart. So there
is no unit `ExecStart` copy of the code — the copies that exist are:
the **working tree** (what the staged test runs, via `PYTHONPATH`),
and nothing on PATH until the owner puts the wrapper somewhere (there
is no `qubes-top` entry point on PATH by default; the probe's
`command -v qubes-top` line reports it).

## Files

- `dom0-probe.sh` — the M2 staged dom0 probe (card t_ae308ca8):
  `python3 -m qubes_top --probe`, the data-path check before the UI.
- `stage-test.sh` — the M3 staged test (this card): the probe's
  superset (`python3 -m qubes_top --stage` — imports, both GTK
  namespaces, `is_composited()`, py_compile, the stdlib suite
  including the guards, the headless CLI against real qubes), then —
  only when the fail count is zero — the GUI in the foreground for the
  human. Read-only w.r.t. `$HOME` (a `mktemp -d` scratch cwd, removed
  on every exit path; the test writes no files — the only `$HOME` write
  a staged run can cause is the GUI's own paned-position file, the
  app's designed behaviour).
- `qubes-top-run.sh` — the launcher: `python3 -m qubes_top "$@"` by module,
  **never** a module by file path. Resolves the tree (its parent),
  puts it on `PYTHONPATH`, exec's the module.
- `install-on-dom0.sh` — install or update on dom0 in one command,
  the qubes-systemlist route: a staging qube (default `personal`,
  first argument) clones origin/main, stamps `BUILD-COMMIT`, streams a
  tarball back over `qvm-run -p`; dom0 checks the archive and runs
  `--api-audit` against it before swapping the tree into
  `~/.local/share/qubes-top`, then writes `~/.local/bin/qubes-top` and
  `~/.local/share/applications/qubes-top.desktop`. Running it again is an
  update. Only pushed commits deploy.
- `qubes-top.desktop` — the menu entry, `OnlyShowIn=Qubes` so it does not
  pollute a menu it was not asked to appear in. `Exec` carries the
  `@QUBES_TOP_ROOT@` placeholder; one line resolves it (below).

## The artifact

The tarball is built from the **working tree** at HEAD
(`tar --exclude=.git --exclude=__pycache__`), never `git archive` —
`git archive` silently omits untracked files, including the staging
script written for this handover. The `sha256sum` is printed alongside
the tarball in the handover; the review card (t_4a05468a) checks the
tarball against the commit.

## Install on dom0

```
bash install-on-dom0.sh [staging-qube]
```

Get the script into dom0 the usual way (`qvm-run -p <qube> 'cat .../install-on-dom0.sh' > install-on-dom0.sh`).
The copy-in below stays the route for a staged test of an unpushed tree.

## Copy-in on dom0 (the owner's run)

Fresh extract, never an overlay onto an existing checkout (an overlay
leaves deleted files behind and a staged test then runs against a union
of two revisions):

```
cd $(mktemp -d)          # or a fixed path the owner chooses
tar xzf /path/to/qubes-top-<commit>.tar.gz
sha256sum <tarball>      # match the handover's printed sum before running
bash qubes-top/packaging/stage-test.sh
```

`stage-test.sh` resolves the tree as its parent (the extract root) and
prints `NOTE copy:` naming which copy of the code the test ran.

## The menu entry (optional, one line)

```
sed "s|@QUBES_TOP_ROOT@|$EXTRACT_DIR|" "$EXTRACT_DIR/qubes-top/packaging/qubes-top.desktop" \
  > ~/.local/share/applications/qubes-top.desktop
```

## Off-box state

Nothing in this directory has executed on dom0. `dom0-probe.sh`'s
dom0-side path, `stage-test.sh` in full, and the GUI have all run
only as far as the dev box can prove (refusal branch, off-box suite
degradation — see README.md). The staged test is the tier that counts.
