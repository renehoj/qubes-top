"""Static audit of every gi symbol, widget method and cairo call the UI makes.

Why this exists: two invented GTK APIs in a row — ``StyleContext.get_default``
and ``Label.get_pango_attributes`` — each crashed the window on the target and
each cost a full staged run, because neither can fail on a box without ``gi``.
Twenty green test modules cannot execute a gi call, so the AST is checked
against the *real* modules instead: module symbols (``Gtk.Foo``,
``Gtk.Foo.bar``) and method calls on variables assigned from a widget
constructor (``self._l = Gtk.Label(); self._l.bar()``), which is exactly the
shape both bugs took.

The same blind spot applies to **cairo** (D-21): ``cr.text_extents`` and friends
cannot fail here either, and the sparkline label deliberately swallows its
errors so a drawing surprise costs a label rather than a row — which is the
right behaviour and precisely why the methods have to be checked: a wrong name
would silently draw nothing. ``audit_cairo`` resolves every ``cr.<method>``
against the host's real ``cairo.Context`` (skipped off-box, where there is no
pycairo).

Off-box there is no ``gi`` and the module reports SKIP rather than pretending.
"""
import ast

MODULE_NAMES = ("Gtk", "Gdk", "Pango", "GLib")

#: The name every draw callback uses for its cairo context. Cairo calls are
#: *unverifiable on a box without pycairo* — the same blind spot as a gi call,
#: and the same remedy: check them against the real class on the target. This
#: began with the sparkline scale label (D-21), whose ``text_extents`` /
#: ``set_font_size`` / ``show_text`` would otherwise fail into a swallowed
#: exception and simply not draw (``draw_graph_top_label`` cannot crash a row,
#: which is right, and is exactly why the failure needs a check of its own).
CAIRO_NAMES = frozenset({"cr"})


def _chain(node):
    """``Gtk.StyleContext.get_default`` -> ['Gtk','StyleContext','get_default']."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return []
    parts.append(node.id)
    parts.reverse()
    return parts


def _key(node):
    """The variable name a call/assignment refers to (``self._l`` -> '_l')."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def audit_sources(sources, modules):
    """Audit ``{label: source}`` against ``{module name: module-like}``.

    Returns a list of ``(label, lineno, message)`` problems; empty is clean.
    Injectable so the logic itself is testable on a box without gi.
    """
    problems = []
    for label, text in sources.items():
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            problems.append((label, getattr(exc, "lineno", 0) or 0,
                             "syntax error: %s" % exc))
            continue

        # Pass 1 — which Gtk class does each name hold?
        holds = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if not (isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)):
                continue
            chain = _chain(value.func)
            if len(chain) != 2 or chain[0] not in modules:
                continue
            for target in node.targets:
                key = _key(target)
                if key:
                    holds.setdefault(key, set()).add((chain[0], chain[1]))

        # Pass 2 — module symbols: Gtk.Foo and Gtk.Foo.bar
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            chain = _chain(node)
            if len(chain) == 2 and chain[0] in modules:
                if not hasattr(modules[chain[0]], chain[1]):
                    problems.append(
                        (label, node.lineno,
                         "%s.%s does not exist" % (chain[0], chain[1])))
            elif len(chain) == 3 and chain[0] in modules:
                holder = getattr(modules[chain[0]], chain[1], None)
                if holder is not None and not hasattr(holder, chain[2]):
                    problems.append(
                        (label, node.lineno,
                         "%s.%s has no attribute %s"
                         % (chain[0], chain[1], chain[2])))

        # Pass 3 — methods on a variable holding a widget
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            key = _key(node.func.value)
            if key not in holds:
                continue
            method = node.func.attr
            candidates = sorted(holds[key])
            known = False
            for module_name, class_name in candidates:
                cls = getattr(modules[module_name], class_name, None)
                if cls is not None and hasattr(cls, method):
                    known = True
                    break
            if not known:
                problems.append(
                    (label, node.lineno,
                     "%s has no method %s (holds %s)"
                     % (key, method,
                        ", ".join("%s.%s" % c for c in candidates))))

    # One line per distinct problem, first location wins.
    seen = set()
    unique = []
    for label, lineno, message in problems:
        if message in seen:
            continue
        seen.add(message)
        unique.append((label, lineno, message))
    return unique


def audit_cairo(sources, cairo_module):
    """Every ``cr.<method>`` in these sources must exist on ``cairo.Context``.

    Returns ``[(label, lineno, message)]``; empty is clean, and an absent
    ``cairo_module`` (no pycairo off-box) yields no problems rather than a
    fake pass — ``audit_dir`` reports SKIP in that case.
    """
    problems = []
    if cairo_module is None:
        return problems
    context = getattr(cairo_module, "Context", None)
    if context is None:
        return problems
    for label, text in sources.items():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue        # audit_sources already reports the syntax error
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            receiver = node.func.value
            if not (isinstance(receiver, ast.Name)
                    and receiver.id in CAIRO_NAMES):
                continue
            method = node.func.attr
            if not hasattr(context, method):
                problems.append(
                    (label, node.lineno,
                     "cairo.Context has no method %s" % method))
    return problems


def real_modules():
    """The host's real Gtk/Gdk/Pango/GLib, or None without gi."""
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import GLib, Gdk, Gtk, Pango
    except Exception:
        return None
    return {"Gtk": Gtk, "Gdk": Gdk, "Pango": Pango, "GLib": GLib}


def real_cairo():
    """The host's real pycairo, or None. (PyGObject needs it, so a box with
    ``gi`` should have it — but it is asked separately rather than assumed.)"""
    try:
        import cairo
    except Exception:
        return None
    return cairo


def audit_dir(ui_dir):
    """Audit every ``*.py`` in ``ui_dir``. Returns (status, problems).

    Covers gi symbols, widget methods *and* cairo methods on ``cr`` (D-21).
    status is "PASS", "FAIL" or "SKIP" (no gi on this host).
    """
    modules = real_modules()
    if modules is None:
        return "SKIP", []
    sources = {}
    for path in sorted(ui_dir.glob("*.py")):
        sources[path.name] = path.read_text()
    problems = audit_sources(sources, modules)
    problems += audit_cairo(sources, real_cairo())
    return ("FAIL" if problems else "PASS"), problems
