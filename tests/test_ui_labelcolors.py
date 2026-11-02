"""Qubes label colours for the row dots (D-20): unit tests + wiring pins.

The dot beside a qube's name used to be ``zlib.crc32(name)`` into a fixed
palette — stable and meaningless. D-20 makes it the qube's **real Qubes
label**, read as ``vm.label`` (an ``admin.vm.property.Get label`` RPC that
``PropertyHolder.__getattr__`` turns into a ``label.Label`` object) then
``Label.color`` (``admin.label.Get``, ``#rrggbb``). Both facts are read from
``QubesOS/qubes-core-admin-client@release4.3``; neither has executed on the
target, so the staged probe prints every domain's label before the GUI is
trusted (``qubes_top/dom0/probe.py``, ``label_colors_check``).

Pinned here:

1. a readable label wins over the hash, and both sources agree with what the
   row and the detail pane are handed (they take the same ``color_for``);
2. the **cost contract** — one ``label.color`` read per *distinct label* per
   pass (the real property is an RPC cached on the Label instance, which
   ``app.labels.get_blind`` shares per name), and one domain-list read per
   TTL;
3. degradation, never a crash: no label, a malformed colour, a raising
   ``label.color``, a raising ``vm.label``, an unavailable app — each falls
   back to the hash, and one bad domain does not poison the others;
4. **a failed read is not a removal** — a transient failure keeps the colour
   already known; a qube gone from ``app.domains`` is dropped, so a recreated
   qube cannot inherit the old one's colour;
5. the wiring actually happens: ``app.py`` hands ``color_for`` to
   ``LeftPaneModel`` *and* the window, and the window hands it to
   ``RightPane``. The seam defaults to the hash, so a dropped argument would
   leave every dot hashed with the whole UI still green — that is the bug
   these three AST pins exist to catch.

The fakes are deliberately *narrower* than reality (the real Label also has
``icon`` and ``index``; the real VM has dozens of properties): a stub that
offers more than the real library certifies whatever the code assumes, which
is how this project's first live run died.

Run standalone:

    python3 tests/test_ui_labelcolors.py

The sys.path shim below makes this module layout-independent.
"""
import ast
import pathlib
import sys
import unittest
from typing import Optional

# sys.path shim: puts the repo root (the parent of tests/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.core import (  # noqa: E402
    NORD_LABEL_COLORS, dot_color_for_label, label_color, parse_color,
)
from qubes_top.dom0.qubesadmin_adapter import QubesAdminUnavailable  # noqa: E402
from qubes_top.ui.labelcolors import LabelColorProvider  # noqa: E402

#: A label Qubes does **not** ship, with its own colour in qubesd's wire format.
#: The mechanics tests (caching, cadence, fallback) use it deliberately: the
#: Nord map answers the eight default labels without reading a colour, so a
#: test that wants the *read* path — or wants a malformed colour to reach the
#: fallback — has to use a custom name. Keeping these tests off the palette also
#: means a future nord tweak cannot cascade through them.
TEAL, TEAL_HEX = "teal", "#008080"
MAUVE, MAUVE_HEX = "mauve", "#8f5f8f"


# ---------------------------------------------------------------------------
# Narrow fakes: only the surface the provider is allowed to touch.
# ---------------------------------------------------------------------------
class FakeLabel:
    """A stand-in for ``qubesadmin.label.Label``.

    Narrower than the real class on purpose (no ``icon``, no ``index``), and
    ``color`` counts its reads: the real one is an ``admin.label.Get`` RPC
    cached on the instance, so "one read per distinct label per pass" is a
    cost contract, not a style preference.
    """

    def __init__(self, name, colour):
        self._name = name
        self._colour = colour
        self.color_reads = 0

    @property
    def name(self):
        return self._name

    @property
    def color(self):
        self.color_reads += 1
        if isinstance(self._colour, Exception):
            raise self._colour
        return self._colour


class FakeVM:
    """A domain: ``name`` + ``label``. Nothing else, as the provider needs."""

    def __init__(self, name, label):
        self.name = name
        self._label = label

    @property
    def label(self):
        if isinstance(self._label, Exception):
            raise self._label
        return self._label


class FakeApp:
    def __init__(self, domains=None):
        self.domains_list = list(domains or [])
        self.domains_reads = 0
        self.boom: Optional[BaseException] = None

    @property
    def domains(self):
        self.domains_reads += 1
        if self.boom is not None:
            raise self.boom
        return self.domains_list


class FakeAdapter:
    """A stand-in for ``QubesAdapter`` (the ``.app`` property only)."""

    def __init__(self, app):
        self._app = app

    @property
    def app(self):
        if isinstance(self._app, Exception):
            raise self._app
        return self._app


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def provider(domains=(), ttl=60.0, clock=None, app=None):
    a = app if app is not None else FakeApp(domains)
    p = LabelColorProvider(qubes=FakeAdapter(a), refresh_interval=ttl,
                           clock=clock if clock is not None else Clock())
    return p, a


# ---------------------------------------------------------------------------
# 1. the label decides the dot colour (D-20 → D-22)
# ---------------------------------------------------------------------------
class TestReadsTheQubesLabel(unittest.TestCase):
    def test_a_default_label_gets_its_nord_tone(self):
        # qubesd's REAL values (0x…) behind the REAL palette names: this is the
        # test that would have caught the 0x/# mismatch that shipped, and it
        # now pins D-22 — the dot carries the label's Nord tone, not the
        # saturated Qubes one.
        p, _ = provider([FakeVM("work", FakeLabel("purple", "0x75507b")),
                         FakeVM("untrusted", FakeLabel("red", "0xcc0000")),
                         FakeVM("yellowvm", FakeLabel("yellow", "0xedd400"))])
        self.assertEqual(p.color_for("work"), NORD_LABEL_COLORS["purple"])
        self.assertEqual(p.color_for("untrusted"), NORD_LABEL_COLORS["red"])
        self.assertEqual(p.color_for("yellowvm"), NORD_LABEL_COLORS["yellow"])
        # ...and it is *not* what the hash would have produced
        self.assertNotEqual(p.color_for("work"), label_color("work"))

    def test_a_default_label_costs_no_colour_rpc(self):
        # the Nord map answers the eight defaults, so admin.label.Get is never
        # called for them — and a refused colour RPC cannot blank a known label
        boom = QubesAdminUnavailable("admin.label.Get refused (test)")
        red = FakeLabel("red", boom)
        p, _ = provider([FakeVM("a", red)])
        self.assertEqual(p.color_for("a"), NORD_LABEL_COLORS["red"])
        self.assertEqual(red.color_reads, 0,
                         "a mapped label must not read its colour from qubesd")

    def test_a_custom_label_uses_the_colour_qubesd_reports(self):
        # a label Qubes does not ship is not in the Nord map: its dot comes
        # from qubesd, in either wire format
        for raw in ("0x008080", "#008080", "0X008080", "#008080  "):
            p, _ = provider([FakeVM("work", FakeLabel(TEAL, raw))])
            self.assertEqual(p.color_for("work"), TEAL_HEX, raw)

    def test_a_custom_label_is_read_once_per_pass(self):
        # three qubes, two distinct custom labels: one colour RPC per label
        teal = FakeLabel(TEAL, "0x008080")
        mauve = FakeLabel(MAUVE, "0x8f5f8f")
        p, _ = provider([FakeVM("a", teal), FakeVM("b", teal),
                         FakeVM("c", mauve)])
        self.assertEqual(p.color_for("a"), TEAL_HEX)
        self.assertEqual(p.color_for("b"), TEAL_HEX)
        self.assertEqual(p.color_for("c"), MAUVE_HEX)
        self.assertEqual(teal.color_reads, 1)
        self.assertEqual(mauve.color_reads, 1)

    def test_distinct_label_objects_with_the_same_name_are_one_colour_read(self):
        # app.labels shares one Label per name, but a client that hands back
        # two objects for the same label must still cost one read per pass:
        # the pass keys its cache by label *name*.
        one = FakeLabel(TEAL, "0x008080")
        two = FakeLabel(TEAL, "0x008080")
        p, _ = provider([FakeVM("a", one), FakeVM("b", two)])
        self.assertEqual(p.color_for("a"), TEAL_HEX)
        self.assertEqual(p.color_for("b"), TEAL_HEX)
        self.assertEqual(one.color_reads + two.color_reads, 1)


# ---------------------------------------------------------------------------
# 1b. the Nord mapping itself (D-22)
# ---------------------------------------------------------------------------
class TestNordMapping(unittest.TestCase):
    """The eight Qubes labels → the official Nord palette.

    The hex values are pinned *literally* here, not read back from the mapping:
    this is the provenance check (nordtheme/nord@develop ``src/nord.scss``), so
    a typo in the table is a red test rather than a subtly wrong dot.
    """

    #: The 16 official tones, verbatim from src/nord.scss.
    OFFICIAL = {
        "#2e3440", "#3b4252", "#434c5e", "#4c566a",          # Polar Night
        "#d8dee9", "#e5e9f0", "#eceff4",                     # Snow Storm
        "#8fbcbb", "#88c0d0", "#81a1c1", "#5e81ac",          # Frost
        "#bf616a", "#d08770", "#ebcb8b", "#a3be8c", "#b48ead",  # Aurora
    }

    def test_the_mapping_is_the_official_palette(self):
        self.assertEqual(NORD_LABEL_COLORS, {
            "red": "#bf616a",      # nord11 Aurora
            "orange": "#d08770",   # nord12 Aurora
            "yellow": "#ebcb8b",   # nord13 Aurora
            "green": "#a3be8c",    # nord14 Aurora
            "purple": "#b48ead",   # nord15 Aurora
            "blue": "#5e81ac",     # nord10 Frost (deepest blue)
            "black": "#3b4252",    # nord1 — not nord0, which is the background
            "gray": "#d8dee9",     # nord4 — Nord has no neutral grey
        })

    def test_every_default_qubes_label_is_covered(self):
        # pins the set, so a Qubes release adding a ninth label is noticed here
        self.assertEqual(set(NORD_LABEL_COLORS),
                         {"red", "orange", "yellow", "green",
                          "gray", "blue", "purple", "black"})

    def test_every_value_is_a_nord_tone(self):
        self.assertTrue(set(NORD_LABEL_COLORS.values()) <= self.OFFICIAL)

    def test_the_neutrals_stay_visible_and_distinct(self):
        # nord0 is the Nord *background*: a black dot of that colour vanishes,
        # and black vs gray must not be one step apart on a 14 px dot
        self.assertNotEqual(NORD_LABEL_COLORS["black"], "#2e3440")
        self.assertNotEqual(NORD_LABEL_COLORS["black"],
                            NORD_LABEL_COLORS["gray"])

    def test_all_eight_are_distinct(self):
        self.assertEqual(len(set(NORD_LABEL_COLORS.values())), 8)

    def test_the_name_lookup_is_case_and_space_tolerant(self):
        for name in ("yellow", "YELLOW", "  Yellow  "):
            self.assertEqual(dot_color_for_label(name), NORD_LABEL_COLORS["yellow"])

    def test_an_unknown_name_with_no_colour_has_no_dot_colour(self):
        self.assertIsNone(dot_color_for_label("teal"))
        self.assertIsNone(dot_color_for_label(None))
        self.assertIsNone(dot_color_for_label(42))


# ---------------------------------------------------------------------------
# 2. the pass is not run per row (the cost contract)
# ---------------------------------------------------------------------------
class TestRefreshCadence(unittest.TestCase):
    def test_one_domain_read_within_the_ttl(self):
        clock = Clock()
        p, app = provider([FakeVM("a", FakeLabel(TEAL, "0x008080"))],
                          ttl=60.0, clock=clock)
        for _ in range(20):
            p.color_for("a")
        self.assertEqual(app.domains_reads, 1)

    def test_ttl_expiry_refreshes(self):
        clock = Clock()
        p, app = provider([FakeVM("a", FakeLabel(TEAL, "0x008080"))],
                          ttl=60.0, clock=clock)
        p.color_for("a")
        clock.now += 61.0
        p.color_for("a")
        self.assertEqual(app.domains_reads, 2)

    def test_an_unknown_name_refreshes_immediately(self):
        # a qube that starts after the last pass must not wear a hashed dot
        # for up to a whole TTL
        clock = Clock()
        app = FakeApp([FakeVM("a", FakeLabel(TEAL, "0x008080"))])
        p, _ = provider(clock=clock, app=app)
        self.assertEqual(p.color_for("a"), TEAL_HEX)
        app.domains_list.append(FakeVM("b", FakeLabel(MAUVE, "0x8f5f8f")))
        self.assertEqual(p.color_for("b"), MAUVE_HEX)
        self.assertEqual(app.domains_reads, 2)

    def test_a_missing_name_does_not_refresh_on_every_call(self):
        # the unknown-name refresh must not become a per-frame RPC once the
        # pass has just run (a name that is simply not a qube)
        clock = Clock()
        p, app = provider([FakeVM("a", FakeLabel(TEAL, "0x008080"))],
                          ttl=60.0, clock=clock)
        p.color_for("ghost")
        reads = app.domains_reads
        for _ in range(10):
            p.color_for("ghost")
        self.assertEqual(app.domains_reads, reads)

    def test_a_wedged_daemon_is_not_asked_per_row(self):
        # the timestamp is stamped before the read: a failing app.domains must
        # not turn every dot into an RPC attempt
        p, app = provider([], ttl=60.0)
        app.boom = QubesAdminUnavailable("qubesd down (test)")
        for _ in range(10):
            p.color_for("a")
        self.assertEqual(app.domains_reads, 1)


# ---------------------------------------------------------------------------
# 3. degradation: always a colour, never a crash
# ---------------------------------------------------------------------------
class TestFallback(unittest.TestCase):
    def test_unknown_qube_is_the_hash(self):
        p, _ = provider([FakeVM("work", FakeLabel("red", "#cc0000"))])
        self.assertEqual(p.color_for("not-a-qube"), label_color("not-a-qube"))

    def test_no_label_set_is_the_hash(self):
        p, _ = provider([FakeVM("work", None)])
        self.assertEqual(p.color_for("work"), label_color("work"))

    def test_malformed_colour_is_the_hash(self):
        # a CUSTOM label name, so the colour read actually happens: a mapped
        # default label never reads one, and would mask this entirely
        for bad in ("green", "", "#cc00", "#ccccccx", "#GGGGGG", None, 42,
                    b"#cc0000"):
            p, _ = provider([FakeVM("work", FakeLabel(TEAL, bad))])
            self.assertEqual(p.color_for("work"), label_color("work"),
                             "colour %r was not rejected" % (bad,))

    def test_raising_label_colour_is_the_hash(self):
        boom = QubesAdminUnavailable("admin.label.Get refused (test)")
        p, _ = provider([FakeVM("work", FakeLabel(TEAL, boom))])
        self.assertEqual(p.color_for("work"), label_color("work"))

    def test_one_bad_domain_does_not_poison_the_others(self):
        p, _ = provider([
            FakeVM("broken", QubesAdminUnavailable("property.Get failed")),
            FakeVM("odd", FakeLabel(TEAL, RuntimeError("no colour"))),
            FakeVM("good", FakeLabel(MAUVE, "0x8f5f8f")),
        ])
        self.assertEqual(p.color_for("good"), MAUVE_HEX)
        self.assertEqual(p.color_for("broken"), label_color("broken"))
        self.assertEqual(p.color_for("odd"), label_color("odd"))

    def test_unavailable_app_falls_back_for_everything(self):
        p = LabelColorProvider(
            qubes=FakeAdapter(QubesAdminUnavailable("not dom0 (test)")))
        for name in ("a", "b", "dom0"):
            self.assertEqual(p.color_for(name), label_color(name))

    def test_domains_without_a_name_are_skipped(self):
        class Nameless:
            label = FakeLabel(TEAL, "0x008080")

            @property
            def name(self):
                return None

        p, _ = provider([Nameless(),
                         FakeVM("good", FakeLabel(TEAL, "0x008080"))])
        self.assertEqual(p.color_for("good"), TEAL_HEX)

    def test_empty_and_none_names_use_the_fallback(self):
        p, app = provider([])
        self.assertEqual(p.color_for(None), label_color(""))
        self.assertEqual(p.color_for(""), label_color(""))
        # ...without asking the app once per frame
        sees = app.domains_reads
        for _ in range(5):
            p.color_for(None)
        self.assertEqual(app.domains_reads, sees)


# ---------------------------------------------------------------------------
# 4. a failed read is not a removal
# ---------------------------------------------------------------------------
class TestStaleData(unittest.TestCase):
    def test_a_transient_failure_keeps_the_known_colour(self):
        clock = Clock()
        p, app = provider([FakeVM("work", FakeLabel(TEAL, "0x008080"))],
                          ttl=10.0, clock=clock)
        self.assertEqual(p.color_for("work"), TEAL_HEX)
        app.boom = QubesAdminUnavailable("qubesd restarting (test)")
        clock.now += 11.0
        self.assertEqual(p.color_for("work"), TEAL_HEX,
                         "a failed pass must not flip a known dot to the hash")
        app.boom = None
        clock.now += 11.0
        self.assertEqual(p.color_for("work"), TEAL_HEX)

    def test_a_gone_qube_is_dropped_so_a_recreated_one_is_clean(self):
        clock = Clock()
        app = FakeApp([FakeVM("work", FakeLabel(TEAL, "0x008080"))])
        p, _ = provider(ttl=10.0, clock=clock, app=app)
        self.assertEqual(p.color_for("work"), TEAL_HEX)
        # the qube is deleted and a new one is created under the same name
        # with a different label: the old colour must not survive
        app.domains_list = [FakeVM("work", FakeLabel(MAUVE, "0x8f5f8f"))]
        clock.now += 11.0
        self.assertEqual(p.color_for("work"), MAUVE_HEX)

    def test_a_removed_qube_falls_back_and_leaves_the_snapshot(self):
        clock = Clock()
        app = FakeApp([FakeVM("a", FakeLabel(TEAL, "0x008080")),
                       FakeVM("b", FakeLabel(MAUVE, "0x8f5f8f"))])
        p, _ = provider(ttl=10.0, clock=clock, app=app)
        self.assertEqual(sorted(p.colors()), ["a", "b"])
        app.domains_list = [FakeVM("a", FakeLabel(TEAL, "0x008080"))]
        clock.now += 11.0
        self.assertEqual(sorted(p.colors()), ["a"])
        self.assertEqual(p.color_for("b"), label_color("b"))


# ---------------------------------------------------------------------------
# 5. the dot colour is one source for both dots (the wiring)
# ---------------------------------------------------------------------------
def _call_keywords(filename, func_name):
    """Every call to ``func_name`` in a module, as a list of keyword-name sets."""
    path = pathlib.Path(__file__).resolve().parents[1] / "qubes_top" / "ui" / filename
    tree = ast.parse(path.read_text(), str(path))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == func_name):
            out.append((node.lineno, {kw.arg for kw in node.keywords}))
    return out


class TestWiring(unittest.TestCase):
    """The seam defaults to the hash, so a dropped argument leaves every dot
    hashed while the whole UI still works — the silent-fallback shape. These
    pins are what make that a red test instead of a wrong screenshot."""

    def test_app_passes_color_for_to_the_list_model(self):
        calls = _call_keywords("app.py", "LeftPaneModel")
        self.assertTrue(calls, "app.py never constructs LeftPaneModel")
        self.assertTrue(all("color_for" in keys for _line, keys in calls),
                        "LeftPaneModel built without color_for: %r" % (calls,))

    def test_app_passes_color_for_to_the_window(self):
        calls = _call_keywords("app.py", "QubesTopWindow")
        self.assertTrue(calls, "app.py never constructs QubesTopWindow")
        self.assertTrue(all("color_for" in keys for _line, keys in calls),
                        "QubesTopWindow built without color_for: %r" % (calls,))

    def test_window_passes_color_for_to_the_right_pane(self):
        calls = _call_keywords("window.py", "RightPane")
        self.assertTrue(calls, "window.py never constructs RightPane")
        self.assertTrue(all("color_for" in keys for _line, keys in calls),
                        "RightPane built without color_for: %r" % (calls,))

    def test_the_app_builds_the_label_provider(self):
        calls = _call_keywords("app.py", "LabelColorProvider")
        self.assertEqual(len(calls), 1,
                         "expected exactly one LabelColorProvider in app.py")


# ---------------------------------------------------------------------------
# 6. the colour parser — and the format that broke it
# ---------------------------------------------------------------------------
class TestParseColor(unittest.TestCase):
    """``parse_color``: qubesd returns ``0xRRGGBB``, a widget needs ``#rrggbb``.

    This is the regression that shipped: the first version accepted only
    ``#rrggbb``, every real label colour on 4.3 arrives as ``0x…``, so every
    dot fell back to the hash — a qube labelled *yellow* drew a random colour
    and the owner caught it. The fakes in this file now speak the real format
    for the same reason the fakes are narrow: a stub that speaks a format
    reality does not is a stub that certifies the bug.
    """

    def test_qubesd_format_is_normalised(self):
        # the exact values from qubes-core-admin@release4.3 qubes/app.py
        for raw, expected in (("0xcc0000", "#cc0000"),
                              ("0xf57900", "#f57900"),
                              ("0xedd400", "#edd400"),
                              ("0x73d216", "#73d216"),
                              ("0x555555", "#555555"),
                              ("0x3465a4", "#3465a4"),
                              ("0x75507b", "#75507b"),
                              ("0x000000", "#000000")):
            self.assertEqual(parse_color(raw), expected, raw)

    def test_hash_format_is_accepted_and_lowercased(self):
        self.assertEqual(parse_color("#EDD400"), "#edd400")
        self.assertEqual(parse_color("  #edd400  "), "#edd400")

    def test_uppercase_0x_prefix_and_digits(self):
        self.assertEqual(parse_color("0XEDD400"), "#edd400")

    def test_everything_else_is_none(self):
        for bad in ("", "yellow", "#edd40", "0xedd40", "#gggggg", "edd400",
                    None, 42, b"0xedd400", ("0xedd400",)):
            self.assertIsNone(parse_color(bad), bad)


# ---------------------------------------------------------------------------
# 7. the deferred import (this module must import on a qubesadmin-less box)
# ---------------------------------------------------------------------------
class TestDeferredImport(unittest.TestCase):
    def test_importing_the_provider_never_imports_qubesadmin(self):
        import qubes_top.ui.labelcolors as _lc  # noqa: F401
        self.assertNotIn("qubesadmin", sys.modules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
