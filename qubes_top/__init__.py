"""qubes-top: "top for Qubes" — a GTK3 window app that runs in Qubes dom0.

This package root is deliberately inert: it imports nothing (no subpackage,
no gi, no qubesadmin). That is what keeps ``import qubes_top`` working on a
dev box with no gi and no qubesadmin — the core import contract, enforced
by tests/test_import_guard.py.

Layout:
    qubes_top/core/  pure python; no gi/qubesadmin imports
    qubes_top/dom0/  dom0 adapters (qubesadmin, read-only libxenstore)
    qubes_top/ui/    GTK3 frontend (gi only; pin Gtk 3.0 + Gdk 3.0, Gtk first)
"""

__version__ = "0.0.0"
