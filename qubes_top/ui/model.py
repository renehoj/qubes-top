"""The left-pane model (card t_4d9516cd). Pure, no ``gi``.

Owns the *persistent* row state (updated field-by-field in place on each
refresh — a row is never discarded and recreated), the selection (by qube
*name*, so it survives reordering and qube add/remove), the sort (column +
direction, with a **stable tiebreak on name**), the name filter, and the
halted toggle. ``visible_rows()`` returns the ordered, filtered, sorted list.

``refresh`` is the in-place contract the card demands: a name that appears for
the first time gets a fresh ``RowSpec``; a name that persists has its fields
updated in place (the same ``RowSpec`` object, so the widget updates the same
row widget); a name that is no longer present is dropped. The selection, sort,
filter, and toggle are preserved across refreshes and across qube add/remove —
they are configuration, never touched by ``refresh``.

**dom0 is always present and always visible** (it is never gated by the halted
toggle) and is sorted and rendered like any other row. The card's "halted
qubes hidden behind a toggle" is implemented as a visibility predicate on the
row state; with the default sampler (``running_states = ("Running",)``) the
frame carries only running qubes so the toggle is a no-op, but it is fully
wired and exercised by the tests' fake feed so a later Q-1 widening (e.g.
including ``Paused``/``Halted``) works without a redesign.
"""
from typing import Callable, Dict, List, Optional, Sequence

from qubes_top.core import label_color
from qubes_top.ui.rowspec import RowData, RowSpec, make_row_spec

__all__ = ["LeftPaneModel", "DOM0_NAME", "is_running"]

#: The host row's name. Always present, always visible, rendered like any
#: other row.
DOM0_NAME = "dom0"

#: A large sentinel for sorting: a row with no CPU value (a gap / first sample)
#: sorts *last* in both directions, never first.
_NO_CPU = float("inf")


def is_running(state: str) -> bool:
    """The visibility predicate for non-dom0 rows. The Q-1 seam: the default
    sampler sends only ``Running``; a wider ``running_states`` tuple (e.g.
    adding ``Paused``) changes what counts as a row without a redesign."""
    return state == "Running"


class LeftPaneModel:
    def __init__(self, color_for: Optional[Callable[[str], str]] = None) -> None:
        self._color_for = color_for if color_for is not None else label_color
        self._specs: Dict[str, RowSpec] = {}
        # Selection is a qube *name* (not a row index), so it is preserved
        # across reordering and across qube add/remove. If the selected qube
        # disappears, the name is kept (a stale selection — the widget shows no
        # highlight, the right pane shows "gone"); if it reappears, the
        # selection is restored.
        self._selected: Optional[str] = None
        # Default order: by name, ascending. A list of qubes is something you
        # scan for a name, not a leaderboard — CPU-descending reorders the
        # list under the pointer twice a second and makes a row hard to find.
        self._sort_key = "name"     # "name" | "cpu"
        self._sort_dir = 1          # +1 = ascending (the default), -1 = descending
        self._filter = ""
        self._show_halted = False

    # -- configuration (preserved across refresh) -----------------------
    def select(self, name: Optional[str]) -> None:
        self._selected = name

    def selected(self) -> Optional[str]:
        return self._selected

    def set_filter(self, text: str) -> None:
        self._filter = text or ""

    def filter_text(self) -> str:
        return self._filter

    def set_show_halted(self, show: bool) -> None:
        self._show_halted = bool(show)

    def show_halted(self) -> bool:
        return self._show_halted

    def set_sort(self, key: str, direction: int) -> None:
        self._sort_key = key
        self._sort_dir = -1 if direction < 0 else 1

    def sort_key(self) -> str:
        return self._sort_key

    def sort_dir(self) -> int:
        return self._sort_dir

    def toggle_sort(self, key: str) -> None:
        """A header click re-sorts: clicking the *active* column flips its
        direction; clicking a *new* column selects it with a sensible default
        (name -> ascending; CPU -> descending, because a CPU column is read
        for its top)."""
        if key == self._sort_key:
            self._sort_dir = -self._sort_dir
        else:
            self._sort_key = key
            self._sort_dir = -1 if key == "cpu" else 1

    # -- refresh (in place) -------------------------------------------
    def refresh(self, data: Sequence[RowData]) -> None:
        """Update the row state in place from this tick's ``RowData``. New
        names create specs, persistent names update in place, gone names are
        dropped. dom0 (if present in ``data``) is treated like any other row
        here; its *always-visible* property is enforced in ``visible_rows()``."""
        present = set()
        for d in data:
            present.add(d.name)
        # Drop rows whose qube is gone (the selection name is preserved on
        # purpose — see __init__).
        for name in list(self._specs):
            if name not in present:
                del self._specs[name]
        # Upsert in place.
        for d in data:
            spec = self._specs.get(d.name)
            if spec is None:
                self._specs[d.name] = make_row_spec(d, self._color_for)
            else:
                spec.refresh_from(d, self._color_for)

    # -- queries ------------------------------------------------------
    def has(self, name: str) -> bool:
        return name in self._specs

    def names(self) -> List[str]:
        """Every present row name (including halted, unfiltered), for
        diagnostics."""
        return sorted(self._specs)

    def visible_rows(self) -> List[RowSpec]:
        """The ordered, filtered, sorted visible rows. dom0 is always visible
        (never gated by the halted toggle); non-dom0 halted rows are hidden
        unless the toggle is on; the name filter applies to all; the sort
        applies to the survivors (name ascending by default; CPU descending
        with a stable tiebreak on name)."""
        flt = self._filter.strip().lower()
        out: List[RowSpec] = []
        for spec in self._specs.values():
            if spec.name != DOM0_NAME:
                if not self._show_halted and not is_running(spec.state):
                    continue
            if flt and flt not in spec.name.lower():
                continue
            out.append(spec)
        self._sort_in_place(out)
        return out

    def step_selection(self, delta: int) -> Optional[str]:
        """Move the selection ``delta`` visible rows (D-23: Up/Down and Page
        Up/Down, systemlist's keys) and return the new name, or None when
        there is nothing to select.

        Clamped at both ends, never wrapping. With no selection — or one that
        is not visible (filtered out, halted, gone) — a downward step starts
        at the first row and an upward one at the last, so the first keypress
        always lands on a row you can see.
        """
        order = [s.name for s in self.visible_rows()]
        if not order:
            return None
        if self._selected in order:
            i = order.index(self._selected) + delta
        else:
            i = 0 if delta > 0 else len(order) - 1
        self._selected = order[max(0, min(len(order) - 1, i))]
        return self._selected

    def _sort_in_place(self, out: List[RowSpec]) -> None:
        if self._sort_key == "name":
            # The name column sorts by name; the direction flips it. There is
            # no secondary key (the name *is* the key).
            out.sort(key=lambda s: s.name, reverse=(self._sort_dir == -1))
            return
        # "cpu": sort by the latest CPU value; a gap (None) sorts last in both
        # directions (the sentinel is direction-aware). The tiebreak is a
        # *stable* ascending name, independent of the CPU direction (the
        # card's "stable tiebreak on name").
        def key(s: RowSpec):
            c = s.latest_cpu()
            if c is None:
                c = -_NO_CPU if self._sort_dir == -1 else _NO_CPU
            return (self._sort_dir * c, s.name)
        out.sort(key=key)
