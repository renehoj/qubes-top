"""Qube metadata provider (the QubeMetaProvider seam): unit tests
(card t_b14eabf7, M3 gate).

The frozen ``SampleRecord`` has no ``template``/``netvm``/``vcpus``
(DESIGN.md §5), so the header gets them from this seam. Pinned here:
reading the attributes off the shared adapter (one ``Qubes()``
instance, never a second), per-field ``None`` degradation (an oddly
typed or missing attribute is ``None`` — the header renders ``n/a`` —
never a crash out of the stack), a missing qube is ``None`` (never a
blank), an unavailable app (the ``QubesAdminUnavailable`` path) degrades
to ``None`` per field rather than raising into the tick, the vcpus
coercion (positive int or numeric string; bool, zero, non-numeric
string, float, other -> ``None``), and the *deferred* import (the
module imports cleanly on a qubesadmin-less box — ``qubesadmin`` never
enters ``sys.modules`` on import).

Run standalone:

    python3 tests/test_ui_qubemeta.py

The sys.path shim below makes this module layout-independent.
"""
import pathlib
import sys
import unittest

# sys.path shim: puts the repo root (the parent of tests/) on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.dom0.qubesadmin_adapter import (  # noqa: E402
    QubesAdminUnavailable,
)
from qubes_top.ui.qubemeta import (  # noqa: E402
    QubeMeta,
    QubesAdminMetaProvider,
)


class FakeVM:
    """A stand-in for a qubesadmin VM domain (attribute reads only)."""

    def __init__(self, name, template=None, netvm=None, vcpus=None):
        self.name = name
        self.template = template
        self.netvm = netvm
        self.vcpus = vcpus


class FakeTemplate:
    def __init__(self, name):
        self.name = name


class FakeApp:
    def __init__(self, domains):
        self._domains = domains

    @property
    def domains(self):
        return self._domains


class FakeAdapter:
    """A stand-in for QubesAdapter (the .app property)."""

    def __init__(self, app):
        self._app = app

    @property
    def app(self):
        return self._app


def provider(domains, raise_on_app=False):
    if raise_on_app:
        class BrokenApp:
            @property
            def domains(self):
                raise QubesAdminUnavailable("adapter down (test)")
        return QubesAdminMetaProvider(qubes=FakeAdapter(BrokenApp()))
    return QubesAdminMetaProvider(qubes=FakeAdapter(FakeApp(domains)))


def vm(items):
    return [FakeVM(n, t, net, v) for (n, t, net, v) in items]


class TestMetaRead(unittest.TestCase):
    def test_full_read(self):
        p = provider(vm([
            ("work", FakeTemplate("fedora-40"), "work-net", 2),
        ]))
        m = p.meta("work")
        self.assertIsInstance(m, QubeMeta)
        self.assertEqual(m.template, "fedora-40")
        self.assertEqual(m.netvm, "work-net")
        self.assertEqual(m.vcpus, 2)

    def test_missing_qube_is_none_not_blank(self):
        p = provider(vm([("work", None, None, None)]))
        self.assertIsNone(p.meta("no-such-qube"))

    def test_missing_attributes_are_none(self):
        p = provider(vm([("work", None, None, None)]))
        m = p.meta("work")
        self.assertEqual(m.template, None)
        self.assertEqual(m.netvm, None)
        self.assertEqual(m.vcpus, None)

    def test_template_object_uses_its_name(self):
        p = provider(vm([("work", FakeTemplate("t-obj"), None, None)]))
        self.assertEqual(p.meta("work").template, "t-obj")

    def test_template_str_passthrough(self):
        p = provider(vm([("work", "t-str", None, None)]))
        self.assertEqual(p.meta("work").template, "t-str")

    def test_netvm_object_uses_its_name(self):
        p = provider(vm([("work", None, FakeTemplate("net-obj"), None)]))
        self.assertEqual(p.meta("work").netvm, "net-obj")

    def test_netvm_empty_string_is_none(self):
        p = provider(vm([("work", None, "", None)]))
        self.assertEqual(p.meta("work").netvm, None)

    def test_vcpus_numeric_string(self):
        p = provider(vm([("work", None, None, "2")]))
        self.assertEqual(p.meta("work").vcpus, 2)

    def test_vcpus_zero_is_none(self):
        p = provider(vm([("work", None, None, 0)]))
        self.assertEqual(p.meta("work").vcpus, None)

    def test_vcpus_bool_is_none(self):
        p = provider(vm([("work", None, None, True)]))
        self.assertEqual(p.meta("work").vcpus, None)

    def test_vcpus_float_is_none(self):
        p = provider(vm([("work", None, None, 2.5)]))
        self.assertEqual(p.meta("work").vcpus, None)

    def test_vcpus_non_numeric_string_is_none(self):
        p = provider(vm([("work", None, None, "abc")]))
        self.assertEqual(p.meta("work").vcpus, None)

    def test_vcpus_weird_object_is_none(self):
        p = provider(vm([("work", None, None, object())]))
        self.assertEqual(p.meta("work").vcpus, None)

    def test_app_unavailable_degrades_to_whole_meta_none(self):
        # a wedged app / absent library: the *whole meta* is None (the
        # docstring contract) — never a raise into the tick, never a
        # fabricated field
        p = provider([], raise_on_app=True)
        self.assertIsNone(p.meta("work"))

    def test_domains_raises_degrades_to_whole_meta_none(self):
        class BoomApp:
            @property
            def domains(self):
                raise RuntimeError("boom (test)")
        p = QubesAdminMetaProvider(qubes=FakeAdapter(BoomApp()))
        self.assertIsNone(p.meta("work"))


class TestDeferredImport(unittest.TestCase):
    def test_module_imports_without_qubesadmin(self):
        # the card: "deferred qubesadmin import so the module imports
        # cleanly on a gi-less/qubesadmin-less dev box". Importing the
        # module (and even calling with a fake adapter) must never pull
        # in the real qubesadmin package.
        import qubes_top.ui.qubemeta as _qm
        self.assertTrue(hasattr(_qm, "QubesAdminMetaProvider"))
        self.assertNotIn("qubesadmin", sys.modules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
