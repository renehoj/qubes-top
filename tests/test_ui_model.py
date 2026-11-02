"""Left-pane model: unit tests (card t_4d9516cd, M3 gate).

``LeftPaneModel`` is the pure logic the widget renders from, driven entirely
by a fake feed (no collector, no ``gi``) — the card's "runs against a fake
feed for logic-level checks". Pinned here: the **in-place refresh contract**
(a persistent name updates the same ``RowSpec`` object; a gone name is
dropped), selection by *name* (survives reordering and add/remove; a vanished
selection is preserved as stale and restored on reappearance), the **stable
name tiebreak** on the CPU sort (a gap sorts last in both directions), the
filter, the halted toggle, and **dom0 always visible** (never gated by the
halted toggle).

Run standalone:

    python3 tests/test_ui_model.py

The sys.path shim below makes this module layout-independent.
"""
import pathlib
import sys
import unittest

# sys.path shim: puts the repo root (the parent of tests/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.core import MemState, SampleRecord  # noqa: E402
from qubes_top.ui.model import LeftPaneModel, DOM0_NAME  # noqa: E402
from qubes_top.ui.rowspec import RowData  # noqa: E402

MiB = 1024  # KiB per MiB


def rec(name: str, state: str) -> SampleRecord:
    return SampleRecord(name=name, domid=1, state=state, cpu_time_ns=100,
                       mem_assigned_kib=4 * MiB, mem_max_kib=4 * MiB,
                       mem_used_kib=None, swap_used_kib=None, ts=1.0)


def rd(name: str, state: str = "Running",
      cpu=(None, 10.0), mem=((1.0, 100), (2.0, 200)),
      state_ms=MemState.REPORTED) -> RowData:
    return RowData(name=name, record=rec(name, state), mem_state=state_ms,
                  cpu_values=tuple(cpu), mem_points=tuple(mem))


def spec_names(model: LeftPaneModel) -> list:
    return [s.name for s in model.visible_rows()]


def spec_object(model: LeftPaneModel, name: str):
    for s in model.visible_rows():
        if s.name == name:
            return s
    return None


class TestInPlaceRefresh(unittest.TestCase):
    def test_persistent_name_updates_same_object(self):
        m = LeftPaneModel()
        m.refresh([rd("a", cpu=(None, 5.0)), rd("b")])
        first_a = spec_object(m, "a")
        m.refresh([rd("a", cpu=(None, 9.0)), rd("b")])
        second_a = spec_object(m, "a")
        self.assertIsNotNone(second_a)
        self.assertIs(first_a, second_a)  # the same RowSpec, updated in place
        self.assertEqual(second_a.cpu_text, "9.0%")

    def test_new_name_gets_fresh_spec(self):
        m = LeftPaneModel()
        m.refresh([rd("a")])
        m.refresh([rd("a"), rd("c")])
        self.assertIsNotNone(spec_object(m, "c"))
        self.assertIsNot(spec_object(m, "c"), spec_object(m, "a"))

    def test_gone_name_is_dropped(self):
        m = LeftPaneModel()
        m.refresh([rd("a"), rd("b")])
        m.refresh([rd("a")])
        self.assertIsNone(spec_object(m, "b"))
        self.assertNotIn("b", m.names())

    def test_selection_config_preserved_across_refresh(self):
        m = LeftPaneModel()
        m.set_filter("a")
        m.toggle_sort("name")
        m.refresh([rd("a")])
        self.assertEqual(m.filter_text(), "a")
        self.assertEqual(m.sort_key(), "name")


class TestSelection(unittest.TestCase):
    def test_select_and_read(self):
        m = LeftPaneModel()
        m.refresh([rd("a"), rd("b")])
        m.select("b")
        self.assertEqual(m.selected(), "b")

    def test_selection_survives_reordering(self):
        m = LeftPaneModel()
        m.refresh([rd("a", cpu=(None, 1.0)), rd("b", cpu=(None, 9.0))])
        m.select("b")
        m.toggle_sort("cpu")  # flip the order
        self.assertEqual(m.selected(), "b")  # preserved by name

    def test_selection_preserved_when_qube_goes(self):
        m = LeftPaneModel()
        m.refresh([rd("a")])
        m.select("a")
        m.refresh([rd("b")])  # a vanishes
        self.assertEqual(m.selected(), "a")  # stale, preserved

    def test_selection_restored_on_reappearance(self):
        m = LeftPaneModel()
        m.refresh([rd("a")])
        m.select("a")
        m.refresh([rd("b")])
        m.refresh([rd("a")])  # a returns
        self.assertEqual(m.selected(), "a")
        self.assertIsNotNone(spec_object(m, "a"))

    def test_clear(self):
        m = LeftPaneModel()
        m.select("a")
        m.select(None)
        self.assertIsNone(m.selected())


class TestStepSelection(unittest.TestCase):
    """Up/Down/Page Up/Page Down (D-23): a step through the visible rows."""

    def setUp(self):
        self.m = LeftPaneModel()
        self.m.refresh([rd("a"), rd("b"), rd("c"), rd("off", "Halted")])

    def test_steps_through_the_visible_order(self):
        self.m.select("a")
        self.assertEqual(self.m.step_selection(1), "b")
        self.assertEqual(self.m.selected(), "b")
        self.assertEqual(self.m.step_selection(-1), "a")

    def test_clamps_and_never_wraps(self):
        self.m.select("c")
        self.assertEqual(self.m.step_selection(1), "c")
        self.assertEqual(self.m.step_selection(-10), "a")
        self.assertEqual(self.m.step_selection(-1), "a")

    def test_no_selection_starts_at_the_end_it_moves_from(self):
        self.assertEqual(self.m.step_selection(1), "a")
        self.m.select(None)
        self.assertEqual(self.m.step_selection(-1), "c")

    def test_a_hidden_selection_counts_as_none(self):
        # a halted qube is not visible while the toggle is off: the first
        # step must land on a row the user can see
        self.m.select("off")
        self.assertEqual(self.m.step_selection(1), "a")

    def test_follows_the_filter(self):
        self.m.set_filter("b")
        self.assertEqual(self.m.step_selection(1), "b")
        self.assertEqual(self.m.step_selection(1), "b")

    def test_nothing_visible_selects_nothing(self):
        self.m.set_filter("zzz")
        self.m.select("a")
        self.assertIsNone(self.m.step_selection(1))
        self.assertEqual(self.m.selected(), "a")


class TestSort(unittest.TestCase):
    def test_default_name_ascending(self):
        # Default order: by name, ascending — a list of qubes is scanned for
        # a name, not read as a CPU leaderboard. CPU values must not affect it.
        m = LeftPaneModel()
        m.refresh([rd("b", cpu=(None, 20.0)), rd("a", cpu=(None, 10.0)),
                   rd(DOM0_NAME, cpu=(None, 5.0))])
        self.assertEqual(spec_names(m), ["a", "b", DOM0_NAME])
        self.assertEqual(m.sort_key(), "name")
        self.assertEqual(m.sort_dir(), 1)

    def test_cpu_desc_when_selected(self):
        m = LeftPaneModel()
        m.set_sort("cpu", -1)
        m.refresh([rd("a", cpu=(None, 10.0)), rd("b", cpu=(None, 20.0)),
                   rd(DOM0_NAME, cpu=(None, 5.0))])
        self.assertEqual(spec_names(m), ["b", "a", DOM0_NAME])

    def test_stable_name_tiebreak(self):
        m = LeftPaneModel()
        m.set_sort("cpu", -1)
        m.refresh([rd("b", cpu=(None, 10.0)), rd("a", cpu=(None, 10.0))])
        self.assertEqual(spec_names(m), ["a", "b"])  # tie -> ascending name

    def test_gap_sorts_last_desc(self):
        m = LeftPaneModel()
        m.set_sort("cpu", -1)
        m.refresh([rd("a", cpu=(None,)), rd("b", cpu=(None, 10.0))])
        self.assertEqual(spec_names(m), ["b", "a"])

    def test_gap_sorts_last_asc(self):
        m = LeftPaneModel()
        m.set_sort("cpu", 1)
        m.refresh([rd("a", cpu=(None,)), rd("b", cpu=(None, 10.0))])
        self.assertEqual(spec_names(m), ["b", "a"])

    def test_toggle_active_flips_direction(self):
        m = LeftPaneModel()
        m.refresh([rd("a", cpu=(None, 1.0)), rd("b", cpu=(None, 9.0))])
        m.toggle_sort("cpu")                    # new column: CPU -> desc
        self.assertEqual(m.sort_dir(), -1)
        self.assertEqual(spec_names(m), ["b", "a"])
        m.toggle_sort("cpu")                    # active: flips to asc
        self.assertEqual(m.sort_dir(), 1)
        self.assertEqual(spec_names(m), ["a", "b"])

    def test_toggle_name_from_the_default_flips_it(self):
        # name is the default, so the first click on "Name" *flips* the
        # direction rather than re-selecting it
        m = LeftPaneModel()
        m.refresh([rd("a"), rd("b")])
        m.toggle_sort("name")
        self.assertEqual(m.sort_key(), "name")
        self.assertEqual(m.sort_dir(), -1)
        self.assertEqual(spec_names(m), ["b", "a"])
        m.toggle_sort("name")
        self.assertEqual(m.sort_dir(), 1)
        self.assertEqual(spec_names(m), ["a", "b"])


class TestFilter(unittest.TestCase):
    def test_substring(self):
        m = LeftPaneModel()
        m.refresh([rd("alpha-vm"), rd("bravo-vm"), rd(DOM0_NAME)])
        m.set_filter("alp")
        self.assertEqual(spec_names(m), ["alpha-vm"])

    def test_case_insensitive(self):
        m = LeftPaneModel()
        m.refresh([rd("alpha")])
        m.set_filter("A")
        self.assertEqual(spec_names(m), ["alpha"])

    def test_empty_shows_all(self):
        m = LeftPaneModel()
        m.refresh([rd("a"), rd("b")])
        m.set_filter("")
        self.assertEqual(spec_names(m), ["a", "b"])

    def test_dom0_never_filtered_away_by_name(self):
        # the filter applies to all rows including dom0 (by name)
        m = LeftPaneModel()
        m.refresh([rd("alpha"), rd(DOM0_NAME)])
        m.set_filter("zzz")
        self.assertEqual(spec_names(m), [])  # nothing matches
        m.set_filter("dom")
        self.assertEqual(spec_names(m), [DOM0_NAME])


class TestHaltedToggle(unittest.TestCase):
    def test_default_hides_halted(self):
        m = LeftPaneModel()
        m.refresh([rd("a", state="Running"), rd("b", state="Halted"),
                   rd(DOM0_NAME, state="Running")])
        self.assertEqual(set(spec_names(m)), {DOM0_NAME, "a"})

    def test_toggle_shows_halted(self):
        m = LeftPaneModel()
        m.refresh([rd("a", state="Running"), rd("b", state="Halted")])
        m.set_show_halted(True)
        self.assertEqual(sorted(spec_names(m)), ["a", "b"])

    def test_dom0_never_gated(self):
        # even if dom0's state were halted, it is never hidden by the toggle
        m = LeftPaneModel()
        m.refresh([rd(DOM0_NAME, state="Halted")])
        self.assertEqual(spec_names(m), [DOM0_NAME])  # show_halted is False


if __name__ == "__main__":
    unittest.main(verbosity=2)
