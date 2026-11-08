<img width="1153" height="815" alt="qubes-top2" src="https://github.com/user-attachments/assets/b13adc69-56d1-4d8d-b17b-22cf73efe30d" />

# qubes-top

A `top`-style system monitor for [Qubes OS](https://www.qubes-os.org/). It is a
GTK3 window that runs in dom0 and shows live CPU, memory, disk and network
activity for every running qube, together with the process list of the qube
you select.

## Features

- **Qube list** with one row per running qube: label-coloured dot, name,
  status, CPU %, CPU graph, memory (used / max, in GiB) and memory graph.
  Each small graph shows the upper bound of its scale, so you can tell at a
  glance whether a spike is 5 % or 200 %.
- **Search and filter**: start typing to filter the list; press Ctrl+H to
  include halted qubes.
- **Details for the selected qube**: template, netvm, vCPUs, configured
  maximum memory, current allocation, used memory, swap and domain ID.
- **History charts** for the selected qube, covering the last five minutes:
  CPU, memory (with the live allocation and the configured maximum marked),
  disk I/O and network I/O.
- **Process table**: dom0's own processes, or the processes inside the
  selected qube (read from the qube's `top` output). Click a column header to
  sort.
- **Qubes look and feel**: follows the Qubes menu's light or dark theme, and
  uses the qube's label colour (in Nord tones) for the dot beside its name.
- **Read-only**: qubes-top never changes anything. It reads the Qubes Admin
  API and xenstore, and the only command it runs inside a qube is `top`
  (via `qvm-run --no-shell`) for the process table of the selected qube.

## Requirements

- Qubes OS 4.3 (tested on R4.3.2)
- dom0 with Python 3 and GTK3 / PyGObject (both present on a standard Qubes
  install)
- For installing via the script: a qube with `git` that can reach the
  repository

No extra Python packages are needed.

## Usage

Start **Qubes Top** from the menu, or from a dom0 terminal:

```sh
qubes-top
```

You can also run it straight from a source checkout in dom0:

```sh
bin/qubes-top
```

### Keyboard shortcuts

| Key | Action |
|---|---|
| *typing* | filter the qube list |
| Up / Down, Page Up / Page Down | move the selection |
| Ctrl+H | show or hide halted qubes |
| Escape | clear the search; press again to close the window |
| Ctrl+Q | close the window |

The window has no title bar. Drag it by its background and resize it from
its outer edge.

### Command-line options

| Option | Description |
|---|---|
| *(none)* or `--gui` | open the window |
| `--probe` | check that every data source works and print PASS/FAIL per check, without opening the window |
| `--stage` | run the full self-test on this host (probe, imports, test suite), without opening the window |
| `--api-audit` | check that every GTK, GLib, Pango and cairo call the UI makes exists on this system |

If the window does not start or shows odd values, run `qubes-top --probe`
first; its output usually shows the cause.

### Configuration

The position of the divider between the two panes is saved in
`~/.config/qubes-top/config.json`. There are no other settings.

## Uninstall

In dom0:

```sh
rm -rf ~/.local/share/qubes-top ~/.config/qubes-top
rm -f ~/.local/bin/qubes-top ~/.local/share/applications/qubes-top.desktop
```
