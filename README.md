# qubes-top

<img width="1243" height="862" alt="qubes-top" src="https://github.com/user-attachments/assets/c27d3cb1-f9a6-4f2d-aa80-e16b323670bc" />


`top` for Qubes OS: a small GTK3 window in dom0 that shows what every running
qube is doing right now, with CPU, memory and processes.

- **Qube list.** Each running qube appears with its label colour, status, CPU %
  and memory, plus a five-minute sparkline for each.
- **Search.** Type to filter the list. *All qubes* also shows the halted ones.
- **Detail for the selected qube.** You get its template, netvm, vCPUs and
  memory figures, and five-minute CPU and memory charts with axes. The memory
  chart marks the live allocation and the configured max.
- **Process table.** For dom0 it reads `/proc`. For a guest qube it runs `top`
  inside that qube via `qvm-run`. Click a column title to sort.
- **Looks like the other Qubes tools.** It uses the stock Qubes menu theme, light
  or dark, and the same look as qubes-systemlist.

## Requirements

- Qubes OS 4.3, in dom0. It was tested on R4.3.2 with Python 3.13.
- Python 3 with PyGObject (GTK 3) and `qubesadmin`. dom0 ships all of them.
- No other packages are needed, and nothing is built.

## Install

dom0 has no network, so the installer gets the code through a qube that does.
Copy the script into dom0 from a qube that has a checkout:

```bash
qvm-run -p <qube> 'cat qubes-top/packaging/install-on-dom0.sh' > install-on-dom0.sh
```

Then run it in dom0, naming the qube that should clone the repository:

```bash
QUBES_TOP_REPO=<clone URL of this repository> bash install-on-dom0.sh <qube>
```

The qube clones the repository and streams it to dom0 as a tarball. dom0
checks the archive and that GTK resolves, and then installs:

| what | where |
|---|---|
| the code | `~/.local/share/qubes-top` |
| the command | `~/.local/bin/qubes-top` |
| the menu entry *Qubes Top* | `~/.local/share/applications/` |

Run the installer again to update. It installs whatever the repository's
default branch holds.

Read the script before you run it. Code that a qube hands to dom0 runs with
dom0's privileges.

## Usage

Start *Qubes Top* from the menu, or run `qubes-top` in a dom0 terminal.

| key | |
|---|---|
| typing | search |
| Up / Down, Page Up / Page Down | move the selection |
| Ctrl+H | show halted qubes too |
| Escape | clear the search; press it again to close |
| Ctrl+Q | close |

The window has no title bar. Drag its background to move it, and drag its outer
edge to resize it.

## What it touches

qubes-top only reads. It never writes to xenstore and never changes a qube.
It reads:

- each qube's state and label through the Admin API (`qubesadmin`);
- the memory each qube reports through xenstore (`memory/meminfo`);
- dom0's own `/proc`.

The one command it runs is `top` in batch mode in the qube you select,
through `qvm-run --no-shell`, to fill the process table. It makes no network
connections.

## Development

The tests use only the Python standard library and run anywhere:

```bash
python3 run_tests.py
```

These checks only mean something on a real dom0:

```bash
python3 -m qubes_top --probe      # data path: one PASS/FAIL line per check
python3 -m qubes_top --api-audit  # every GTK symbol the UI uses exists here
python3 -m qubes_top --stage      # the full staged test, then the window
```

[DESIGN.md](DESIGN.md) holds the design decisions (D-1 … D-26).
[NOTES.md](NOTES.md) holds the detailed status and history notes.
