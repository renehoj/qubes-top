"""GTK3 frontend of qubes-top (scaffold — M3).

May import gi. Pin BOTH namespaces before importing, with Gtk first:

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gtk, ...

dom0 ships GTK4, so a bare ``from gi.repository import Gdk`` resolves Gdk to
4.0 and the later Gtk 3.0 import dies on the target — a failure a dev box
without GTK4 cannot reproduce.

Must NOT be imported from the package __init__.py or from qubes_top.core.
Nothing in this package has ever executed anywhere until the owner runs it
in dom0 (README).
"""
