"""Right-pane detail spec + process display: unit tests (card t_b14eabf7,
M3 gate).

``DetailSpec.refresh`` builds the display strings from the core formatters
(never recomputing metric arithmetic) and the display bounds (the CPU
100% floor, the memory max/assigned ceiling). The card's "say the mute
case in words, never as 100%", the "n/a (unresolved)" domid sentinel, the
hard string cap, and the ``proc_display`` sort (top-like ``%CPU``
descending; a missing CPU value sorts last in both directions; stable pid
tiebreak; an unknown sort key degrades to the default, never a crash)
are pinned here.

Run standalone:

    python3 tests/test_ui_rightspec.py

The sys.path shim below makes this module layout-independent.
"""
import pathlib
import sys
import unittest
from typing import List, Optional

# sys.path shim: puts the repo root (the parent of tests/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.core import MemState, SampleRecord  # noqa: E402
from qubes_top.ui.processes import ProcessInfo  # noqa: E402
from qubes_top.ui.qubemeta import QubeMeta  # noqa: E402
from qubes_top.ui.rightspec import (  # noqa: E402
    NA,
    NO_VALUE,
    STRING_CAP,
    DetailSpec,
    cap,
    domid_text,
    mem_state_text,
    proc_display,
    swap_text,
    used_text,
)

MiB = 1024  # KiB per MiB
GiB = 1024 * MiB  # KiB per GiB


def rec(name="test-vm", state="Running", domid=7, used: Optional[int] = None,
        assigned: int = 4 * MiB, max_kib: int = 4 * MiB,
        swap: Optional[int] = None, ts=1.0) -> SampleRecord:
    return SampleRecord(
        name=name, domid=domid, state=state, cpu_time_ns=100,
        mem_assigned_kib=assigned, mem_max_kib=max_kib,
        mem_used_kib=used, swap_used_kib=swap, ts=ts)


META = QubeMeta(template="fedora-40", netvm="work-net", vcpus=2)


def refresh(name="test-vm", record=None, state=MemState.REPORTED,
           cpu=(None, 12.0), mem=((1.0, 100), (2.0, 200)),
           meta=None, color=lambda n: "#aabbcc") -> DetailSpec:
    spec = DetailSpec()
    spec.refresh(name, record if record is not None else rec(),
                state, list(cpu), list(mem), meta, color)
    return spec


class TestCap(unittest.TestCase):
    def test_missing_is_na(self):
        self.assertEqual(cap(None), NA)

    def test_short_passes_through(self):
        self.assertEqual(cap("work"), "work")

    def test_long_is_hard_capped(self):
        long = "x" * (STRING_CAP + 10)
        out = cap(long)
        self.assertEqual(len(out), STRING_CAP)
        self.assertEqual(out, "x" * STRING_CAP)

    def test_non_string_is_stringified(self):
        self.assertEqual(cap(1234), "1234")


class TestMemStateText(unittest.TestCase):
    def test_reported(self):
        self.assertEqual(mem_state_text(MemState.REPORTED), "reported")

    def test_mute_says_the_words_and_never_100(self):
        t = mem_state_text(MemState.MUTE)
        self.assertTrue(t.startswith("not reported"))
        # the card: "mute means the qube's meminfo-writer is absent or
        # dead, and must say so in words, never as 100%" — the *value*
        # is never the number 100%; the worded state may mention that
        # 100% is withheld (saying it in words is the point)
        self.assertIn("meminfo-writer", t)
        self.assertIn("absent or dead", t)
        self.assertNotEqual(t.strip(), "100%")

    def test_unknown_says_no_usable_observation(self):
        self.assertIn("no usable observation",
                     mem_state_text(MemState.UNKNOWN))


class TestValueTexts(unittest.TestCase):
    def test_used_reported_is_a_number(self):
        self.assertEqual(used_text(2 * MiB, MemState.REPORTED), "2.0 MiB")

    def test_used_mute_is_not_reported(self):
        self.assertEqual(used_text(None, MemState.MUTE), "not reported")

    def test_used_unknown_is_unknown(self):
        self.assertEqual(used_text(1 * MiB, MemState.UNKNOWN), "unknown")

    def test_swap_none_is_na_never_zero(self):
        self.assertEqual(swap_text(None), NA)

    def test_swap_value_is_formatted(self):
        self.assertEqual(swap_text(512), "0.5 MiB")

    def test_domid_normal(self):
        self.assertEqual(domid_text(7), "7")

    def test_domid_unresolved_sentinel(self):
        self.assertEqual(domid_text(-1), "n/a (unresolved)")


class TestDetailSpecRefresh(unittest.TestCase):
    def test_header_with_meta(self):
        s = refresh(meta=META)
        self.assertEqual(s.name, "test-vm")
        self.assertEqual(s.state, "Running")
        self.assertEqual(s.color, "#aabbcc")
        self.assertEqual(s.template, "fedora-40")
        self.assertEqual(s.netvm, "work-net")
        self.assertEqual(s.vcpus, "2")
        self.assertEqual(s.mem_max_text, "4.0 MiB")

    def test_header_without_meta_is_na(self):
        s = refresh(meta=None)
        self.assertEqual(s.template, NA)
        self.assertEqual(s.netvm, NA)
        self.assertEqual(s.vcpus, NA)

    def test_meta_fields_capped(self):
        meta = QubeMeta(template="t" * (STRING_CAP + 5), netvm=None, vcpus=1)
        s = refresh(meta=meta)
        self.assertEqual(len(s.template), STRING_CAP)
        self.assertEqual(s.netvm, NA)
        self.assertEqual(s.vcpus, "1")

    def test_name_is_capped_and_color_uses_full_name(self):
        long = "y" * (STRING_CAP + 20)
        seen = []

        def color(n: str) -> str:
            seen.append(n)
            return "#000000"

        s = refresh(name=long, meta=None, color=color)
        self.assertEqual(len(s.name), STRING_CAP)
        # the colour is mapped from the *full* name (stable across the
        # cap), not the capped one
        self.assertEqual(seen, [long])

    def test_max_unset_is_na(self):
        s = refresh(record=rec(max_kib=0))
        self.assertEqual(s.mem_max_text, NA)

    def test_facts_reported(self):
        s = refresh(record=rec(used=2 * MiB, swap=1 * MiB))
        # the live allocation, not the configured memory (the meta row's
        # "mem max" carries that) — DESIGN.md D-13
        self.assertEqual(s.allocated_text, "4.0 MiB")
        self.assertEqual(s.used_text, "2.0 MiB")
        self.assertEqual(s.swap_text, "1.0 MiB")
        self.assertEqual(s.report_text, "reported")
        self.assertEqual(s.domid_text, "7")

    def test_facts_mute(self):
        s = refresh(record=rec(used=None), state=MemState.MUTE)
        self.assertEqual(s.used_text, "not reported")
        self.assertEqual(s.report_text,
                         mem_state_text(MemState.MUTE))
        # the worded state is in the block; the *value* is a word, never
        # the number 100%
        self.assertNotEqual(s.used_text, "100%")
        self.assertFalse(s.used_text[:1].isdigit())

    def test_facts_unknown(self):
        s = refresh(record=rec(used=1 * MiB), state=MemState.UNKNOWN)
        self.assertEqual(s.used_text, "unknown")

    def test_gi_bounding(self):
        # a 4 GiB max renders as GiB (the core formatter's unit switch)
        s = refresh(record=rec(assigned=4 * GiB, max_kib=4 * GiB))
        self.assertEqual(s.mem_max_text, "4.0 GiB")


class TestChartBounds(unittest.TestCase):
    def test_cpu_hi_is_at_least_100(self):
        s = refresh(cpu=(None, 12.0))
        self.assertEqual(s.cpu_hi, 100.0)

    def test_cpu_hi_follows_values_above_100(self):
        # % of one core may exceed 100 (D-3): the chart must show it, so
        # the 100% reference line is inside, not clamped away
        s = refresh(cpu=(None, 150.5))
        self.assertEqual(s.cpu_hi, 150.0 + 0.5)

    def test_cpu_hi_empty_series_is_100(self):
        s = refresh(cpu=())
        self.assertEqual(s.cpu_hi, 100.0)

    def test_mem_hi_is_the_max(self):
        s = refresh(record=rec(assigned=2 * MiB, max_kib=4 * MiB))
        self.assertEqual(s.mem_hi, float(4 * MiB))
        self.assertEqual(s.assigned_kib, 2 * MiB)
        self.assertEqual(s.max_kib, 4 * MiB)

    def test_mem_hi_falls_back_to_assigned_when_max_unset(self):
        s = refresh(record=rec(assigned=2 * MiB, max_kib=0))
        self.assertEqual(s.mem_hi, float(2 * MiB))
        self.assertEqual(s.max_kib, 0)  # the band is not drawn (<= 0)

    def test_mem_hi_degenerate_is_one(self):
        s = refresh(record=rec(assigned=0, max_kib=0))
        self.assertEqual(s.mem_hi, 1.0)


class TestProcDisplay(unittest.TestCase):
    def rows(self) -> List[ProcessInfo]:
        return [
            ProcessInfo(pid=3, name="b", cpu_pct=5.0, rss_kib=2 * MiB),
            ProcessInfo(pid=1, name="a", cpu_pct=None, rss_kib=1 * MiB),
            ProcessInfo(pid=2, name="c", cpu_pct=100.5, rss_kib=3 * MiB),
        ]

    def test_cpu_desc_default(self):
        out = proc_display(self.rows(), "cpu", -1)
        self.assertEqual([r.pid for r in out], [2, 3, 1])
        self.assertEqual(out[0].cpu_text, "100.5%")
        # a first reading (no baseline) shows the placeholder, never a
        # number
        self.assertEqual(out[2].cpu_text, NO_VALUE)
        self.assertEqual(out[0].rss_text, "3.0 MiB")
        self.assertEqual(out[0].name, "c")

    def test_cpu_asc_missing_sorts_last(self):
        out = proc_display(self.rows(), "cpu", 1)
        # ascending: 5.0, 100.5, then the missing value *last*
        self.assertEqual([r.pid for r in out], [3, 2, 1])

    def test_missing_sorts_last_in_both_directions(self):
        desc = proc_display(self.rows(), "cpu", -1)
        asc = proc_display(self.rows(), "cpu", 1)
        self.assertEqual(desc[0].pid, 2)  # the highest first
        self.assertEqual(desc[-1].pid, 1)  # the missing one last
        self.assertEqual(asc[0].pid, 3)  # the lowest first
        self.assertEqual(asc[-1].pid, 1)  # the missing one last

    def test_pid_key(self):
        out = proc_display(self.rows(), "pid", -1)
        self.assertEqual([r.pid for r in out], [3, 2, 1])
        out = proc_display(self.rows(), "pid", 1)
        self.assertEqual([r.pid for r in out], [1, 2, 3])

    def test_rss_key(self):
        # rss: pid1=1 MiB, pid3=2 MiB, pid2=3 MiB
        out = proc_display(self.rows(), "rss", 1)
        self.assertEqual([r.pid for r in out], [1, 3, 2])
        out = proc_display(self.rows(), "rss", -1)
        self.assertEqual([r.pid for r in out], [2, 3, 1])

    def test_rss_none_sorts_last_and_text_is_placeholder(self):
        rows = [
            ProcessInfo(pid=1, name="a", cpu_pct=None, rss_kib=None),
            ProcessInfo(pid=2, name="b", cpu_pct=None, rss_kib=1 * MiB),
        ]
        out = proc_display(rows, "rss", 1)
        self.assertEqual([r.pid for r in out], [2, 1])
        self.assertEqual(out[1].rss_text, NO_VALUE)  # never a silent zero
        out = proc_display(rows, "rss", -1)
        self.assertEqual([r.pid for r in out], [2, 1])  # last in both dirs

    def test_tiebreak_is_stable_ascending_pid(self):
        rows = [
            ProcessInfo(pid=9, name="x", cpu_pct=5.0, rss_kib=1 * MiB),
            ProcessInfo(pid=2, name="y", cpu_pct=5.0, rss_kib=1 * MiB),
        ]
        out = proc_display(rows, "cpu", -1)
        self.assertEqual([r.pid for r in out], [2, 9])
        out = proc_display(rows, "cpu", 1)
        self.assertEqual([r.pid for r in out], [2, 9])

    def test_unknown_key_degrades_to_cpu_not_crash(self):
        out = proc_display(self.rows(), "bogus", -1)
        self.assertEqual([r.pid for r in out], [2, 3, 1])

    def test_name_is_capped(self):
        rows = [ProcessInfo(pid=1, name="n" * (STRING_CAP + 5),
                           cpu_pct=None, rss_kib=1)]
        out = proc_display(rows, "cpu", -1)
        self.assertEqual(len(out[0].name), STRING_CAP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
