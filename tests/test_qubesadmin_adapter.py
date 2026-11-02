"""qubesadmin adapter (card t_e55e68ef): package-shape fidelity of the
fake, environment selection with *no* app injection (the card's trap
item — ``qubesadmin.app.Qubes`` does not exist, and a construction path
that uses it must be impossible to edit into existence), the stubdom
skip, the deferred-import degradation, and the metric surface.

The fake mirrors the real package (verified against source,
QubesOS/qubes-core-admin-client@master ``qubesadmin/__init__.py``):
``Qubes`` is a package-level attribute bound per environment (dom0 ->
``QubesLocal``, off-box -> ``QubesRemote``); the ``app`` submodule
defines ``QubesBase`` / ``QubesLocal`` / ``QubesRemote`` and
deliberately has **no** ``Qubes`` attribute — the fidelity pin that a
convenience edit cannot re-open the ``app.Qubes`` blind spot. The real
package binds ``Qubes`` at import time from the existence of
``/etc/qubes-release``; the dev box cannot fake that file per test, so
the fake takes the environment as a constructor parameter — the *shape*
(single package-level binding, zero-arg constructor, ``.domains``
collection, ``app.Qubes`` absent) is what is pinned.
"""
import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.dom0.qubesadmin_adapter import (  # noqa: E402
    DomainView,
    QubesAdminUnavailable,
    QubesAdapter,
    is_stubdom_name,
)


class FakeVM:
    """The QubesVM read-set surface: ``.name`` (attribute access in the
    client), ``.get_power_state()`` / ``.get_mem()`` / ``.get_cputime()``
    (methods), ``.mem_static_max`` (attribute access — the real client
    resolves it through ``__getattr__`` to the property value, so plain
    attribute access is the faithful model)."""

    def __init__(self, name, state="Running", mem=1024,
                 cputime=0, mem_max=2048):
        self._name = name
        self._state = state
        self._mem = mem
        self._cputime = cputime
        self._mem_max = mem_max

    @property
    def name(self):
        return self._name

    def get_power_state(self):
        return self._state

    def get_mem(self):
        return self._mem

    def _get_current_state(self):
        """cpu time and both memory figures arrive here; there is no
        ``get_cputime()`` on qubesadmin 4.3.2, and no ``mem_static_max``
        attribute either."""
        return {
            "mem": str(self._mem),
            "mem_static_max": str(self._mem_max),
            "cputime": str(self._cputime),
            "power_state": self._state,
        }


def make_qubesadmin_module(dom0_like):
    """A fake of the qubesadmin package, ``Qubes`` bound per environment
    at construction (mirrors: bound at package level, per environment)."""
    class QubesBase:  # mirrors the real name
        def __init__(self):
            self.domains = []

    class QubesLocal(QubesBase):
        pass

    class QubesRemote(QubesBase):
        pass

    app_mod = types.ModuleType("qubesadmin.app")
    app_mod.QubesBase = QubesBase
    app_mod.QubesLocal = QubesLocal
    app_mod.QubesRemote = QubesRemote
    # deliberately no app_mod.Qubes — the fidelity pin, module docstring.

    mod = types.ModuleType("qubesadmin")
    mod.app = app_mod
    mod.Qubes = QubesLocal if dom0_like else QubesRemote
    return mod


class TestFakeFidelity(unittest.TestCase):
    def test_app_submodule_has_no_qubes(self):
        mod = make_qubesadmin_module(True)
        self.assertFalse(
            hasattr(mod.app, "Qubes"),
            "the fake must mirror the real package: "
            "qubesadmin.app.Qubes does not exist")
        for name in ("QubesBase", "QubesLocal", "QubesRemote"):
            self.assertTrue(hasattr(mod.app, name), name)
        self.assertTrue(hasattr(mod, "Qubes"))

    def test_qubes_is_zero_arg_and_exposes_domains(self):
        app = make_qubesadmin_module(True).Qubes()
        self.assertEqual(app.domains, [])

    def test_env_binding(self):
        mod = make_qubesadmin_module(True)
        self.assertIs(mod.Qubes, mod.app.QubesLocal)
        mod = make_qubesadmin_module(False)
        self.assertIs(mod.Qubes, mod.app.QubesRemote)


class TestEnvSelectionWithoutInjection(unittest.TestCase):
    """The card's trap item: construction with no app injection must
    select QubesLocal dom0-like and QubesRemote off-box. Because the
    fake's ``app`` has no ``Qubes``, any code path using
    ``qubesadmin.app.Qubes`` would fail here loudly."""

    def test_dom0_like_selects_qubes_local(self):
        mod = make_qubesadmin_module(True)
        sys.modules["qubesadmin"] = mod
        self.assertIsInstance(QubesAdapter().app, mod.app.QubesLocal)

    def test_off_box_selects_qubes_remote(self):
        mod = make_qubesadmin_module(False)
        sys.modules["qubesadmin"] = mod
        self.assertIsInstance(QubesAdapter().app, mod.app.QubesRemote)

    def test_app_is_reused_across_accesses(self):
        mod = make_qubesadmin_module(True)
        sys.modules["qubesadmin"] = mod
        adapter = QubesAdapter()
        self.assertIs(adapter.app, adapter.app)

    def test_off_box_without_qubesadmin_is_explicit(self):
        sys.modules.pop("qubesadmin", None)
        with self.assertRaises(QubesAdminUnavailable):
            QubesAdapter().app


class TestAdapterBehaviour(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("qubesadmin", None)

    def _fake_app(self, vms):
        app = make_qubesadmin_module(True).Qubes()
        app.domains = list(vms)
        return app

    def test_stubdoms_are_skipped(self):
        app = self._fake_app([
            FakeVM("work"),
            FakeVM("work-dm"),
            FakeVM("sys-net-dm"),
            FakeVM("dom0"),
            FakeVM("paused", state="Paused"),
        ])
        names = [v.name for v in QubesAdapter(app=app).domains()]
        self.assertEqual(names, ["work", "dom0", "paused"])

    def test_domain_view_metric_surface(self):
        view = DomainView(FakeVM("work", state="Running", mem=4096,
                                cputime=123, mem_max=8192))
        self.assertEqual(view.name, "work")
        self.assertEqual(view.state, "Running")
        self.assertEqual(view.mem_assigned_kib, 4096)
        self.assertEqual(view.cpu_time_ns, 123)
        self.assertEqual(view.mem_max_kib, 8192)

    def test_custom_stubdom_predicate(self):
        app = self._fake_app([FakeVM("work"), FakeVM("work-dm")])
        # a predicate that flags nothing: the straggler surfaces, proving
        # the predicate (not a hard-coded skip) does the filtering
        names = [v.name for v in QubesAdapter(app=app,
                                            is_stubdom=lambda n: False)
                 .domains()]
        self.assertEqual(names, ["work", "work-dm"])
        self.assertTrue(is_stubdom_name("work-dm"))
        self.assertFalse(is_stubdom_name("work"))

    def test_module_import_without_qubesadmin(self):
        """The top-level import of this test module (above) executed the
        adapter's top level in a fresh interpreter with no qubesadmin
        (the runner runs each module as its own script; this box has no
        qubesadmin). If the adapter's top level imported it, that import
        would have failed. Absence pins the deferred-import design.
        (Pop first: sibling tests install a fake, and class order is not
        a dependency this assertion should rest on.)"""
        sys.modules.pop("qubesadmin", None)
        self.assertNotIn("qubesadmin", sys.modules)


if __name__ == "__main__":
    unittest.main()
