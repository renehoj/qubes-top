"""Dom0-side adapters for qubes-top (M2, card t_e55e68ef).

Modules:
    ``qubesadmin_adapter`` -- per-domain metrics via ``qubesadmin.Qubes()``
                             (top-level package attribute), stubdoms skipped
    ``xenstore_adapter``   -- read-only ``libxenstore`` over ctypes, one
                             reused handle, no fork per qube per tick
    ``sampler``            -- per-tick flow assembling
                             ``qubes_top.core.SampleRecord``
    ``probe``              -- the staged dom0 probe (card t_ae308ca8),
                             run as ``python3 -m qubes_top --probe``

Read-only, frozen (DESIGN.md D-11, non-goals): one reused libxenstore
handle, no fork per qube per tick, and the dom0 user's write capability on
the xenstore socket must stay unused. The forbidden-call list is recorded
in DESIGN.md, not here, so the call-oriented grep over this package stays
clean.

May import qubesadmin (deferred to first use) and ctypes. Must NOT be
imported from the package ``__init__.py`` or from ``qubes_top.core`` — that
would break the core import contract (AST guard,
``tests/test_import_guard.py``).
"""
