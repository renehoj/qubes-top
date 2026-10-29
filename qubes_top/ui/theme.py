"""The window's look: qubes-systemlist's, which is qubes-launcher's (D-23).
GTK3, dom0-only.

``is_theme_light``, ``load_stock_theme`` and ``prepare_transparency`` are the
same as in qubes-systemlist's frontend, which mirrors ``qubes_menu.appmenu``
(the Qubes OS Project, LGPL-2.1+): the stock Qubes menu's light or dark
stylesheet defines the colour names (``@outer-background``,
``@left-background``, ``@separator``, ``@buttons-bg``, ``@text-color``,
``@text-color-contrast``), and ``qubes-top.css`` is written against them. Off
dom0, where ``qubes_menu`` is not installed, ``fallback-theme.css`` defines the
same names with Nord values.

What is *not* taken from systemlist is the launcher behaviour: no
keep-above, no skip-taskbar, no hide on focus loss — qubes-top is a monitor
you leave open (D-8), so ``set_chrome`` only names the window and drops the
title bar.

The cairo series colours (``*_RGBA``) are the Nord tones the stylesheets use,
so the charts and the CSS cannot drift apart (D-22's revisit note). Chart
*text* and gridlines are not here: they take the theme's own text colour
(``charts._ink``), so they follow a light theme too.
"""
import importlib.resources

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk  # noqa: E402  (pin above is intentional)

from qubes_top.core import NORD8, NORD11, NORD13, NORD14

__all__ = [
    "WINDOW_NAME", "PANE_CLASS", "WINDOW_ALPHA",
    "CPU_RGBA", "MEM_RGBA", "ALLOC_RGBA", "MAX_RGBA", "rgba", "set_chrome", "prepare_transparency", "apply",
]

#: The CSS name of the top-level window (``#qubes-top-window``).
WINDOW_NAME = "qubes-top-window"
#: The rounded inner panels the content sits on (systemlist's ``.systemlist-pane``).
PANE_CLASS = "qubes-top-pane"
#: Window background opacity on a compositing screen; systemlist's default.
WINDOW_ALPHA = 0.92
ALPHA_PLACEHOLDER = "@ALPHA@"

def rgba(color: str, alpha: float = 1.0) -> tuple:
    """``"#rrggbb"`` -> an ``(r, g, b, a)`` tuple for ``cr.set_source_rgba``."""
    value = color.lstrip("#")
    return (int(value[0:2], 16) / 255.0, int(value[2:4], 16) / 255.0,
            int(value[4:6], 16) / 255.0, alpha)


CPU_RGBA = rgba(NORD8)            # CPU: Frost
MEM_RGBA = rgba(NORD14)           # memory: Aurora green
ALLOC_RGBA = rgba(NORD13, 0.9)    # the live-allocation line: Aurora yellow
MAX_RGBA = rgba(NORD11, 0.9)      # the max line: Aurora red (the launcher red)


def is_theme_light(widget: Gtk.Widget) -> bool:
    """Is the current system theme light? (mirrors qubes_menu.appmenu)"""
    context = widget.get_style_context()
    background = context.get_background_color(Gtk.StateFlags.NORMAL)
    text = context.get_color(Gtk.StateFlags.NORMAL)
    background_intensity = background.red + background.blue + background.green
    text_intensity = text.red + text.blue + text.green
    return text_intensity < background_intensity


def _add_provider(provider) -> None:
    # GTK3 has no ``Gtk.StyleContext.get_default()`` — providers attach to a
    # *screen* (the target raised AttributeError on exactly that once).
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def _add_css(css: str) -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode("utf-8"))
    _add_provider(provider)


def load_stock_theme(widget: Gtk.Widget) -> bool:
    """Apply the stock menu's light or dark stylesheet; False if not installed.

    The light/dark files ``@import`` qubes-menu-base.css and define the colour
    names qubes-top.css is written against.
    """
    try:
        package = importlib.resources.files("qubes_menu")
    except ImportError:
        return False
    name = "qubes-menu-light.css" if is_theme_light(widget) else "qubes-menu-dark.css"
    resource = package / name
    if not resource.is_file():
        return False
    with importlib.resources.as_file(resource) as path:
        provider = Gtk.CssProvider()
        provider.load_from_path(str(path))
    _add_provider(provider)
    return True


def _own_css(name: str) -> str:
    return (importlib.resources.files("qubes_top.ui") / name).read_text(
        encoding="utf-8")


def prepare_transparency(window: Gtk.Window) -> bool:
    """Give the window an RGBA visual so CSS alpha is honoured.

    Must run before the window is first realised, or it stays opaque for good.
    """
    screen = window.get_screen()
    visual = screen.get_rgba_visual() if screen.is_composited() else None
    if visual is None:
        return False
    window.set_visual(visual)
    window.set_app_paintable(True)
    return True


def set_chrome(window: Gtk.Window) -> None:
    """systemlist's undecorated chrome, minus the launcher behaviour: the
    window stays in the taskbar and the pager and is not kept above."""
    window.set_name(WINDOW_NAME)
    window.set_decorated(False)


def apply(window: Gtk.Window, translucent: bool) -> None:
    """The stock (or fallback) colour names, then qubes-top.css on top."""
    if not load_stock_theme(window):
        _add_css(_own_css("fallback-theme.css"))
    alpha = WINDOW_ALPHA if translucent else 1.0
    _add_css(_own_css("qubes-top.css").replace(ALPHA_PLACEHOLDER,
                                             "%.3f" % alpha))
