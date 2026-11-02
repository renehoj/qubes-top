"""Visibility and honesty pins for the UI — AST checks, because no gi exists
off-box to execute the widgets.

The bug these pin: ``LeftPane.refresh`` called ``row.show()`` on each new row.
GTK3 visibility is not inherited, so ``show()`` revealed the row box while
every label and sparkline inside it stayed hidden — the window opened with an
empty list and no error. Rows are created *after* the window's ``show_all()``,
so nothing else was ever going to reveal them. This is the same trap as the
firewall project's dead window, from the other direction.
"""
import ast
import pathlib
import unittest

UI = pathlib.Path(__file__).resolve().parents[1] / "qubes_top" / "ui"


def _code_strings(path):
    """The string literals a module *executes* — docstrings excluded.

    A comment or a docstring explaining what was removed is not a defect; a
    label that still builds the removed text is.
    """
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]



def _methods(module_name, class_name):
    tree = ast.parse((UI / module_name).read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {n.name: n for n in node.body
                    if isinstance(n, ast.FunctionDef)}
    raise AssertionError("%s not found in %s" % (class_name, module_name))


def _called(func_node):
    """[(method name, is_on_self)] for every attribute call in the body."""
    out = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            on_self = (isinstance(node.func.value, ast.Name)
                       and node.func.value.id == "self")
            out.append((node.func.attr, on_self))
    return out


class TestRowsAreShownWithTheirChildren(unittest.TestCase):
    def test_refresh_never_uses_bare_show(self):
        refresh = _methods("left_pane.py", "LeftPane")["refresh"]
        bare = [name for name, _ in _called(refresh) if name == "show"]
        self.assertEqual(
            bare, [],
            "row.show() leaves the row's labels and sparklines hidden; "
            "use show_all()")

    def test_refresh_calls_show_all(self):
        refresh = _methods("left_pane.py", "LeftPane")["refresh"]
        names = [name for name, _ in _called(refresh)]
        self.assertIn("show_all", names,
                      "new rows are created after the window's show_all(), "
                      "so refresh() must show_all() each row itself")


class TestWindowShowsItselfAfterPopulating(unittest.TestCase):
    def test_show_all_on_self_comes_after_populate(self):
        init = _methods("window.py", "QubesTopWindow")["__init__"]
        calls = _called(init)
        populate = [i for i, (n, _) in enumerate(calls) if n == "_populate"]
        shown = [i for i, (n, on_self) in enumerate(calls)
                 if n == "show_all" and on_self]
        self.assertTrue(populate, "the window must populate before showing")
        self.assertTrue(shown, "QubesTopWindow.__init__ must call self.show_all()")
        self.assertGreater(shown[-1], populate[0],
                           "show_all() must follow _populate()")


class TestColumnsCannotDrift(unittest.TestCase):
    """The rows and the header must both build from the same column table.

    The old header had two buttons of its own (CPU first, Name second) and no
    status, memory or graph titles, so its labels sat over nothing in
    particular. Both sides now iterate ``qubes_top.ui.layout.COLUMNS``; a column
    added to one and not the other is the failure this catches, and it cannot
    be caught by executing anything off-box.
    """

    @staticmethod
    def _loops_over_columns(func_node):
        for node in ast.walk(func_node):
            if isinstance(node, ast.For) and isinstance(node.iter, ast.Name):
                if node.iter.id == "COLUMNS":
                    return True
        return False

    def test_rows_build_from_the_column_table(self):
        init = _methods("left_pane.py", "_QubeRow")["__init__"]
        self.assertTrue(self._loops_over_columns(init),
                        "the row must build one cell per COLUMNS entry")

    def test_header_builds_from_the_column_table(self):
        header = _methods("left_pane.py", "LeftPane")["_build_header"]
        self.assertTrue(self._loops_over_columns(header),
                        "the header must build one title per COLUMNS entry")

    def test_fixed_columns_share_a_size_group(self):
        # Without the shared group a wide value widens only its own row and
        # every column after it shifts — per-row layout alignment would be a
        # coincidence of the current content.
        init = _methods("left_pane.py", "LeftPane")["__init__"]
        names = set()
        for node in ast.walk(init):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
        self.assertIn("SizeGroup", names)
        self.assertIn("fixed_columns", names)

    def test_header_reserves_the_scrollbar_width(self):
        # The rows live inside the scroller and the header does not; when the
        # bar is visible the rows are narrower than the pane by its width.
        alloc = _methods("left_pane.py", "LeftPane")["_on_scroller_allocated"]
        names = [n for n, _ in _called(alloc)]
        self.assertIn("set_size_request", names)
        self.assertIn("get_child", names)


class TestMemoryGraphIsNeverADot(unittest.TestCase):
    """A one-point memory series must draw a line, not a dot.

    The memory series carries only observed changes (30 MiB deadband), so for
    a qube whose memory has not moved — most rows — the series has exactly one
    point. The owner reported it on dom0: "it does a dot". The geometry now
    returns a flat two-vertex line, and neither pane may re-special-case a
    single vertex back into an arc.
    """

    def test_no_pane_draws_a_single_vertex_as_a_dot(self):
        for module in ("left_pane.py", "right_pane.py"):
            tree = ast.parse((UI / module).read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                left = node.left
                is_len_one = (
                    isinstance(left, ast.Call)
                    and isinstance(left.func, ast.Name)
                    and left.func.id == "len"
                    and any(isinstance(op, ast.Eq) for op in node.ops)
                    and any(isinstance(c, ast.Constant) and c.value == 1
                            for c in node.comparators))
                if is_len_one:
                    self.fail("%s:%d special-cases a single vertex — the "
                              "geometry already returns a flat line"
                              % (module, node.lineno))


class TestStatusIsPlainText(unittest.TestCase):
    def test_set_status_uses_set_text(self):
        setter = _methods("left_pane.py", "LeftPane")["set_status"]
        names = [n for n, _ in _called(setter)]
        self.assertIn("set_text", names)

    def test_no_module_uses_set_markup(self):
        offenders = []
        for path in sorted(UI.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "set_markup"):
                    offenders.append("%s:%d" % (path.name, node.lineno))
        self.assertEqual(offenders, [],
                         "guest strings must never reach markup")


class TestRowsAreClickable(unittest.TestCase):
    """A row must own a GdkWindow or GTK never delivers a press to it.

    ``_QubeRow`` was a plain ``Gtk.Box``: rows rendered, clicks landed on an
    ancestor with a window (the scroller viewport) and propagated *up* from
    there, never reaching the row, so selection never fired.
    """

    def test_row_is_an_eventbox(self):
        tree = ast.parse((UI / "left_pane.py").read_text())
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "_QubeRow":
                self.assertIn("Gtk.EventBox", [ast.unparse(b) for b in node.bases],
                              "a plain container has no GdkWindow: clicks "
                              "never reach it")
                return
        self.fail("_QubeRow not found")

    def test_row_requests_button_press_events(self):
        init = _methods("left_pane.py", "_QubeRow")["__init__"]
        self.assertIn("add_events", [n for n, _ in _called(init)])

    def test_click_handler_consumes_the_press(self):
        handler = _methods("left_pane.py", "LeftPane")["_on_row_click"]
        returned = [n.value.value for n in ast.walk(handler)
                    if isinstance(n, ast.Return)
                    and isinstance(n.value, ast.Constant)]
        self.assertIn(True, returned,
                      "the press must be consumed, not propagated onward")


class TestChartsLiveUnderTheList(unittest.TestCase):
    """The large charts are fixed height and sit **under the qube list**.

    History: they were 140 px *and* expanding in the detail pane, which ate
    every spare pixel and squeezed the process table; the owner asked for a
    fixed 100 px. Then (D-17) they were moved below the qube list, which is
    where the owner wants them, and the detail pane is left to the header, the
    facts and the process table.
    """

    def test_the_drawing_areas_are_a_fixed_height(self):
        tree = ast.parse((UI / "charts.py").read_text())
        heights = {}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "set_size_request"
                    and len(node.args) == 2
                    and isinstance(node.args[1], ast.Name)):
                heights.setdefault(node.args[1].id, 0)
                heights[node.args[1].id] += 1
        self.assertEqual(heights.get("CHART_H"), 1,
                         "the chart height is one fixed constant")

    def test_the_chart_grid_does_not_expand(self):
        # D-27: the four charts sit in a 2 x 2 grid; the grid, not each
        # frame, is what the box packs, and it must not take the list's height
        tree = ast.parse((UI / "charts.py").read_text())
        packed = {}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "pack_start"
                    and node.args
                    and isinstance(node.args[0], ast.Attribute)):
                packed[node.args[0].attr] = [a.value for a in node.args[1:3]]
        self.assertEqual(packed, {"_grid": [False, False]},
                         "an expanding chart squeezes the list")

    def test_the_window_packs_the_info_box_then_the_charts_under_the_list(self):
        tree = ast.parse((UI / "window.py").read_text())
        order = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "pack_start"):
                continue
            owner = node.func.value
            if not (isinstance(owner, ast.Attribute)
                    and owner.attr == "_left_col"):
                continue
            first = node.args[0]
            if isinstance(first, ast.Attribute):
                order.append((first.attr,
                              [a.value for a in node.args[1:3]]))
        # the owner's order: the list, then the info group box (D-18), then
        # the graphs (D-17)
        self.assertEqual([name for name, _args in order],
                         ["_left", "_info", "_charts"])
        self.assertEqual(order[0][1], [True, True],
                         "the list takes the leftover height")
        for name, args in order[1:]:
            self.assertEqual(args, [False, False],
                             "%s must not steal the list's height" % name)

    def test_the_detail_pane_no_longer_builds_charts(self):
        src = (UI / "right_pane.py").read_text()
        for gone in ("_cpu_area", "_mem_area", "_CHART_H"):
            self.assertNotIn(gone, src,
                             "the charts moved to charts.py (D-17)")

    def test_the_charts_hide_until_something_is_selected(self):
        # show_all() in the window reveals them; with no selection they must
        # give the room back, and a later show_all() must not undo that
        tree = ast.parse((UI / "window.py").read_text())
        calls = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("show_all", "show_for")):
                calls.append(node.func.attr)
        self.assertIn("show_for", calls,
                      "the window must hide the charts after show_all()")
        self.assertLess(calls.index("show_all"), calls.index("show_for"),
                        "hiding before show_all is pointless")

    def test_the_app_feeds_the_charts_from_the_panes_spec(self):
        src = (UI / "app.py").read_text()
        self.assertIn("charts.update(pane.spec, io)", src,
                      "one selection must drive the pane and the charts")
        self.assertIn("charts.show_for(sel is not None)", src)

    def test_the_info_box_is_fed_and_toggled_the_same_way(self):
        src = (UI / "app.py").read_text()
        self.assertIn("info.update(pane.spec)", src,
                      "the info box must show the same spec as the charts")
        self.assertIn("info.show_for(sel is not None)", src)
        window = (UI / "window.py").read_text()
        self.assertIn("_info.show_for(False)", window,
                      "and start hidden, like the charts")

    def test_the_detail_pane_no_longer_builds_the_info_fields(self):
        # the two label/value grids moved into InfoBox (D-18); leaving either
        # behind would duplicate every value on the screen
        src = (UI / "right_pane.py").read_text()
        for gone in ("_meta_labels", "_fact_labels", "Gtk.Grid(column_spacing"):
            self.assertNotIn(gone, src,
                             "the info fields belong in infobox.py (D-18)")


class TestProcTableColumnsAlign(unittest.TestCase):
    """The process table's header and rows build from ``PROC_COLUMNS``.

    Three separate ways it failed to line up: the header's cells were plain
    labels with hardcoded widths (so nothing tied them to the rows), the sort
    controls were ``Gtk.Button``s in a toolbar above the table (theme padding
    offsets a button's text, and they sat over no column anyway), and the
    header — which lives outside the scroller — never reserved the vertical
    scrollbar's width, so every column shifted left by ~14 px the moment the
    bar appeared (on a 490-row table: always).
    """

    def test_header_and_rows_both_iterate_the_columns(self):
        tree = ast.parse((UI / "right_pane.py").read_text())
        builders = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name in ("_ProcRow",
                                                                "RightPane"):
                for fn in node.body:
                    if not isinstance(fn, ast.FunctionDef):
                        continue
                    for inner in ast.walk(fn):
                        if (isinstance(inner, ast.For)
                                and isinstance(inner.iter, ast.Name)
                                and inner.iter.id == "PROC_COLUMNS"):
                            builders.setdefault(fn.name, []).append(inner.lineno)
        self.assertIn("__init__", builders, "_ProcRow must build from PROC_COLUMNS")
        self.assertIn("_build_proc_header", builders,
                      "the header must build from PROC_COLUMNS")

    def test_the_proc_cells_join_size_groups(self):
        src = (UI / "right_pane.py").read_text()
        self.assertGreaterEqual(src.count("_proc_groups[col.key].add_widget"), 1,
                                "the header cell must join the column's group")
        self.assertIn("group.add_widget(label)", src,
                      "each row cell must join the same group")

    def test_the_proc_header_reserves_the_scrollbar(self):
        methods = _methods("right_pane.py", "RightPane")
        self.assertIn("_on_proc_scroller_allocated", methods,
                      "the header must reserve the scrollbar's width")
        src = ast.get_source_segment(
            (UI / "right_pane.py").read_text(),
            methods["_on_proc_scroller_allocated"]) or ""
        self.assertIn("get_allocated_width", src)
        self.assertIn("_proc_header_spacer", src)
        table = ast.get_source_segment((UI / "right_pane.py").read_text(),
                                       methods["_build_proc_table"]) or ""
        self.assertIn("_on_proc_scroller_allocated", table,
                      "the handler must be connected to the scroller")

    def test_the_title_ticks_are_not_buttons(self):
        # a Gtk.Button's padding puts its text off the values beneath it
        methods = _methods("right_pane.py", "RightPane")
        made = set()
        for node in ast.walk(methods["_build_proc_header"]):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "Gtk"):
                made.add(node.func.attr)
        self.assertNotIn("Button", made, "a button's padding offsets its title")
        self.assertIn("EventBox", made, "a clickable title is an EventBox")
        self.assertIn("Label", made)

    def test_the_toolbar_has_no_column_buttons(self):
        # the sort affordance moved into the table header; buttons up here
        # sat over no column and implied an alignment that did not exist
        methods = _methods("right_pane.py", "RightPane")
        src = ast.get_source_segment((UI / "right_pane.py").read_text(),
                                     methods["_build_detail"]) or ""
        self.assertNotIn("_proc_btn", src)

    def test_the_sort_arrow_moves_between_slots(self):
        methods = _methods("right_pane.py", "RightPane")
        update = ast.get_source_segment((UI / "right_pane.py").read_text(),
                                        methods["_update_proc_header"]) or ""
        # the arrows are set on their own labels — never appended to the
        # title text, which would change the title's width when it moved
        self.assertIn("_proc_header_arrows", update)
        self.assertNotIn("set_label", update)


class TestProcTableIsIndexedAndShown(unittest.TestCase):
    """dom0's process table: two defects the target found at once.

    ``_proc_rowbox[r.pid]`` indexed the container Box like the {pid: row}
    map (TypeError on selecting dom0), and the new-row branch used ``show()``,
    which would have left every process row an invisible strip — the qube-row
    bug repeated in the other pane.
    """

    def test_no_subscript_on_the_container(self):
        for node in ast.walk(ast.parse((UI / "right_pane.py").read_text())):
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "_proc_rowbox"):
                self.fail("right_pane.py:%d indexes the container Box as if "
                          "it were the row map" % node.lineno)

    def test_proc_table_uses_show_all_only(self):
        fn = _methods("right_pane.py", "RightPane")["_update_proc_table"]
        names = [n for n, _ in _called(fn)]
        self.assertNotIn("show", names,
                         "a bare show() leaves the row's labels hidden")
        self.assertIn("show_all", names)


class TestProcessPanelHasNoChrome(unittest.TestCase):
    """The panel's title bar and sort hint are gone (D-19).

    Owner: "on the process list there is a title says processes - guest...
    you can remove that title. I the table header line it says click to sort
    you can remove that text as well". Both were chrome above a table whose
    column titles already carry the sort affordance. The one thing that must
    survive is the *absence note* — with the title gone it is the only place
    left that says why a table is not being shown.
    """

    def test_the_title_api_is_gone(self):
        for name in ("right_pane.py", "app.py"):
            tree = ast.parse((UI / name).read_text())
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            attrs = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute)}
            self.assertNotIn("_proc_title", names | attrs,
                             "%s still builds the panel title" % name)
            self.assertNotIn("set_proc_title", attrs,
                             "%s still sets the panel title" % name)

    def test_the_sort_hint_is_gone(self):
        # the strings in *code* only: a comment may mention what it replaced
        for name in ("right_pane.py", "app.py"):
            for text in _code_strings(UI / name):
                self.assertNotIn("click a column", text, name)
                self.assertNotIn("processes —", text, name)

    def test_the_absence_note_survives(self):
        # removing the title must not remove the explanation
        self.assertIn("set_proc_note", (UI / "right_pane.py").read_text())
        app = (UI / "app.py").read_text()
        self.assertIn("pane.set_proc_note(", app)
        self.assertIn("reading processes from the guest", app)

    def test_the_header_still_reserves_the_scrollbar(self):
        # the hint used to absorb the slack in the header; removing it must
        # not remove the spacer that keeps the titles over their values. The
        # spacer has to be *packed* as well as set — creating it and never
        # adding it would leave the reserve silently doing nothing.
        methods = _methods("right_pane.py", "RightPane")
        packed = False
        for node in ast.walk(methods["_build_proc_header"]):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "pack_start"
                    and node.args
                    and isinstance(node.args[0], ast.Attribute)
                    and node.args[0].attr == "_proc_header_spacer"):
                packed = True
        self.assertTrue(packed, "the spacer must be packed into the header")
        self.assertIn("_on_proc_scroller_allocated", methods)
        src = ast.get_source_segment(
            (UI / "right_pane.py").read_text(),
            methods["_on_proc_scroller_allocated"]) or ""
        self.assertIn("get_allocated_width", src)


if __name__ == "__main__":
    unittest.main()
