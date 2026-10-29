"""Gtk.Paned position persistence (card t_4d9516cd). Pure, no ``gi``.

The ``Gtk.Paned`` divider position is persisted to ``~/.config/qubes-top/`` so
the user's left/right split survives relaunch. The file is a small JSON
document (``{"paned_position": int}``). ``load`` returns the saved position,
or ``DEFAULT_POSITION`` when the file is missing, unreadable, malformed, or
non-integer (a corrupt config must never break startup). ``save`` is atomic
(write a temp file, then ``os.replace``) and best-effort: a failed save (e.g.
a read-only ``$HOME``) is swallowed — losing the divider position is a minor
inconvenience, not a crash.

The path is injectable, so this is testable off-box with a temp file (the
actual Gtk wiring — reading it at startup, writing it on drag/close — lives in
the gi layer).
"""
import json
import os
import pathlib
from typing import Optional

__all__ = ["PanedConfig", "DEFAULT_CONFIG_DIR", "DEFAULT_POSITION"]

#: Where the config lives (the card's ``~/.config/qubes-top/``).
DEFAULT_CONFIG_DIR = "~/.config/qubes-top"

#: The divider position (px) when no config exists. A sensible default for a
#: ~1100 px window: the left pane (the qube list) gets a little more than
#: half.
DEFAULT_POSITION = 560


def _default_path() -> pathlib.Path:
    return pathlib.Path(
        os.path.expanduser(os.path.join(DEFAULT_CONFIG_DIR, "config.json")))


class PanedConfig:
    def __init__(self, path: Optional[str] = None) -> None:
        self._path = pathlib.Path(path) if path is not None else _default_path()

    def path(self) -> pathlib.Path:
        return self._path

    def load(self) -> int:
        """The saved divider position, or ``DEFAULT_POSITION`` on any problem.
        Negative values are clamped to 0 (a Paned position is not negative)."""
        try:
            with open(self._path) as f:
                data = json.load(f)
            pos = int(data["paned_position"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return DEFAULT_POSITION
        return max(0, pos)

    def save(self, position: int) -> None:
        """Atomically persist the divider position. Best-effort: a failure is
        swallowed (a lost position must never kill the app)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            with open(tmp, "w") as f:
                json.dump({"paned_position": int(position)}, f)
            os.replace(tmp, self._path)
        except OSError:
            pass
