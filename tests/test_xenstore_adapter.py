"""xenstore adapter (card t_e55e68ef): the **real** libxenstore-ABI
fake (signatures pinned to the Xen source — ``tools/libs/store/xs.c``
+ ``libxenstore.map``, fetched 2026-10-17: ``xs_open(flags)`` →
handle|None; ``xs_close(handle)``; ``xs_read(h, t, path, len*)`` →
malloc'd NUL-terminated value|None with ``*len`` excluding the
terminator; ``xs_directory(h, t, path, num*)`` → single-malloc
``char **``|None — modelled here as a list; write-capability calls
exported by the map and recorded as a spy; **no** ``xs_error`` — the
map does not export it).

This fake exists because the previous revision of this test mirrored an
*invented* ABI (5-argument ``xs_read`` with a caller buffer and
status codes) that matches nothing in the real library — the suite
then agreed with the code and could not catch the wrong binding. The
fidelity pins below assert the fake's signature shape so a caller
written against the old shape fails loudly, the write spy is live,
and the no-write-call pin has a target.

Covered behaviour: clean degradation with the library absent, loud
failure when ``xs_open`` returns NULL (errno carried in the message),
handle reuse (one open, many ticks), the D-6 ASCII-digits-only parse
(+ the 64-byte length cap), the ``Domain-0`` normalisation, errno
failure semantics (ENOENT = absent, anything else = error), and the
no-write-call pin. The real-library paths (string_at + free, the
single free of the directory array) are exercised on the target by the
staged M2 round-trip; off-box they are pinned here by the signature
fidelity and the real candidate loop's clean degradation (this box has
no libxenstore — verified by probe).
"""
import ast
import ctypes
import errno
import inspect
import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.dom0.xenstore_adapter import (  # noqa: E402
    Xenstore,
    XenstoreError,
    XenstoreUnavailable,
    parse_guest_value,
)


class FakeXenStoreLib:
    """In-memory key tree behind the **real** libxenstore signatures.

    Mirrors the library's observable contract (the Python stand-in
    replaces the C pointer/errno machinery with its Python
    equivalents):

    - ``xs_open(flags)``: one argument (``unsigned long``); returns an
      opaque handle or None with errno set (the stand-in sets the
      thread errno via ``ctypes.set_errno`` the way the C library
      leaves it set after a NULL return).
    - ``xs_read(handle, t, path, length)``: **exactly four**
      arguments; on success sets ``length.value`` to the byte length
      *excluding* the NUL terminator and returns the value bytes
      (the C function returns the malloc'd NUL-terminated buffer; the
      adapter's real path reads it with
      ``ctypes.string_at(ptr, length)`` — the same bytes); on a
      missing key returns None with errno ENOENT; on a persistent
      failure None with the failure's errno.
    - ``xs_directory(handle, t, path, num)``: **exactly four**
      arguments; on success sets ``num.value`` and returns a list of
      entry bytes (the C function returns the single-malloc char**;
      the adapter frees it with one ``free()`` — the Python list
      needs no freeing); ENOENT None for a missing directory.
    - ``xs_write`` / ``xs_rm``: exported by the map (VERS_4.0) —
      present in the stand-in and *recorded*, so the no-write-call
      pin has a live target. The adapter never binds or calls them.
    - no ``xs_error``: the map does not export it, and the stand-in
      must not add convenience the library lacks (fidelity pin).
    """

    def __init__(self, keys=None, directories=(), fail_open=False,
                 fail_reads=()):
        self._keys = dict(keys or {})
        self._directories = set(directories)
        self._fail_open = fail_open
        self._fail_reads = set(fail_reads)
        self._handle = None
        self.open_count = 0
        self.open_flags = []
        self.close_count = 0
        self.write_calls = []

    # -- real signatures (pinned by TestFakeFidelity) -------------------

    def xs_open(self, flags):
        self.open_count += 1
        self.open_flags.append(flags)
        if self._fail_open:
            ctypes.set_errno(errno.ECONNREFUSED)
            return None
        self._handle = object()
        return self._handle

    def xs_close(self, handle):
        assert handle is self._handle
        self.close_count += 1
        self._handle = None

    def xs_read(self, handle, t, path, length):
        if handle is not self._handle:
            ctypes.set_errno(errno.EINVAL)
            return None
        path_s = path.decode("ascii")
        if path_s in self._fail_reads:
            ctypes.set_errno(errno.EACCES)
            return None
        if path_s not in self._keys:
            ctypes.set_errno(errno.ENOENT)
            return None
        value = self._keys[path_s]
        # *len excludes the NUL terminator (the C contract).
        length.value = len(value)
        return value

    def xs_directory(self, handle, t, path, num):
        if handle is not self._handle:
            ctypes.set_errno(errno.EINVAL)
            return None
        path_s = path.decode("ascii")
        if path_s not in self._directories:
            ctypes.set_errno(errno.ENOENT)
            return None
        prefix = path_s + "/"
        # Direct children only: take the first path component (a key
        # "/local/domain/7/memory/meminfo" contributes child "7").
        # The real function returns char** (C strings) — the adapter's
        # real path reads them as bytes via c_char_p, so the stand-in
        # returns bytes too.
        entries = sorted(
            {k[len(prefix):].split("/", 1)[0] for k in self._keys
             if k.startswith(prefix)}
        )
        num.value = len(entries)
        return [entry.encode("ascii") for entry in entries]

    # -- write capability: exported by the map (VERS_4.0), recorded so
    # -- the no-write-call pin has a live target.

    def xs_write(self, handle, path, data, length):
        self.write_calls.append(("xs_write", path, data))
        return True

    def xs_rm(self, handle, path):
        self.write_calls.append(("xs_rm", path, None))
        return True


def make_lib(keys=None, fail_reads=()):
    return FakeXenStoreLib(keys=keys or {},
                           directories={"/local/domain"},
                           fail_reads=fail_reads)


def make_keys(domains):
    """domains: {domid: (name, meminfo, swapinfo)} -> full-path key
    dict (all values bytes, as the fake stores them)."""
    keys = {}
    for domid, (name, meminfo, swapinfo) in domains.items():
        if name is not None:
            value = name if isinstance(name, bytes) else name.encode("ascii")
            keys["/local/domain/%d/name" % domid] = value
        if meminfo is not None:
            keys["/local/domain/%d/memory/meminfo" % domid] = meminfo
        if swapinfo is not None:
            keys["/local/domain/%d/memory/swapinfo" % domid] = swapinfo
    return keys


class TestParseGuestValue(unittest.TestCase):
    """D-6, pinned directly: byte-wise ASCII digits only, everything
    else rejected (the check is a byte loop over 0x30..0x39 — no
    locale or unicode-digit path can slip in)."""

    def test_ascii_digits_only(self):
        self.assertEqual(parse_guest_value(b"12345"), 12345)
        self.assertEqual(parse_guest_value(b"0"), 0)
        self.assertEqual(parse_guest_value(b"999999999999999999"),
                         999999999999999999)

    def test_rejects_everything_else(self):
        for bad in (None, b"", b" 1234", b"12a34", b"0x1f", b"1.5",
                    "١٢٣".encode("utf-8"), b"12\r\n", b"-1"):
            self.assertIsNone(parse_guest_value(bad), repr(bad))


class TestFakeFidelity(unittest.TestCase):
    """The fake is as restrictive as the library, not as convenient as
    the caller: signature shape pinned so the old (defective) ABI
    cannot silently keep working."""

    def _nparams(self, func):
        return len([p for p in inspect.signature(func).parameters.values()
                   if p.name != "self"])

    def test_xs_open_takes_exactly_one_argument(self):
        self.assertEqual(self._nparams(FakeXenStoreLib.xs_open), 1)

    def test_xs_close_takes_exactly_one_argument(self):
        self.assertEqual(self._nparams(FakeXenStoreLib.xs_close), 1)

    def test_xs_read_takes_exactly_four_arguments(self):
        """The real prototype is (h, t, path, len*) — four. The old
        invented shape was five (h, path, buf, buflen, multivalue); a
        fake that accepted it would mask a wrong binding again."""
        self.assertEqual(self._nparams(FakeXenStoreLib.xs_read), 4)

    def test_xs_directory_takes_exactly_four_arguments(self):
        """The real prototype is (h, t, path, num*) — four."""
        self.assertEqual(self._nparams(FakeXenStoreLib.xs_directory), 4)

    def test_xs_read_returns_bytes_or_none(self):
        lib = make_lib(make_keys({1: ("work", b"1024", None)}))
        handle = lib.xs_open(0)
        self.assertIsInstance(
            lib.xs_read(handle, 0, b"/local/domain/1/name", ctypes.c_uint(0)),
            bytes)
        self.assertIsNone(
            lib.xs_read(handle, 0, b"/local/domain/9/name", ctypes.c_uint(0)))

    def test_xs_read_sets_len_excluding_nul(self):
        lib = make_lib({"/x": b"1048576"})
        handle = lib.xs_open(0)
        length = ctypes.c_uint(0)
        lib.xs_read(handle, 0, b"/x", length)
        self.assertEqual(length.value, 7,
                         "*len must be the byte length excluding the "
                         "NUL terminator (the C contract)")

    def test_xs_directory_returns_list_or_none(self):
        lib = make_lib(make_keys({1: ("work", b"1024", None)}))
        handle = lib.xs_open(0)
        self.assertIsInstance(
            lib.xs_directory(handle, 0, b"/local/domain", ctypes.c_uint(0)),
            list)
        self.assertIsNone(
            lib.xs_directory(handle, 0, b"/nope", ctypes.c_uint(0)))

    def test_fake_has_no_xs_error(self):
        self.assertFalse(
            hasattr(FakeXenStoreLib, "xs_error"),
            "the real libxenstore does not export xs_error "
            "(libxenstore.map VERS_4.0) — the fake must not add "
            "convenience the library lacks")

    def test_write_spy_is_wired(self):
        """Guard-bites check: the spy records direct calls, so the
        empty assertion in TestNoWriteCalls has a live target."""
        lib = make_lib()
        handle = lib.xs_open(0)
        lib.xs_write(handle, b"/local/evil", b"x", 1)
        lib.xs_rm(handle, b"/local/evil")
        self.assertEqual(len(lib.write_calls), 2)

    def test_old_invented_abi_shape_is_rejected(self):
        """The previous (defective) caller shape — five arguments with a
        caller buffer and a multivalue slot — must fail against the
        real-signature fake. This is the proof that the fidelity pin
        would have caught the original bug."""
        lib = make_lib()
        handle = lib.xs_open(0)
        with self.assertRaises(TypeError):
            lib.xs_read(handle, b"/local/domain/1/name",
                       ctypes.create_string_buffer(16), 16, ctypes.c_int(0))


def _real_libxenstore_present() -> bool:
    """True when this host actually ships libxenstore (dom0 does).

    Whether the candidate loop finds a library is a *host* fact, not a code
    fact — asserting it fails on the target, which is the only machine where
    the real library exists.
    """
    from qubes_top.dom0.xenstore_adapter import candidate_names
    for name in candidate_names():
        try:
            ctypes.CDLL(name)
            return True
        except OSError:
            continue
    return False


class TestNoLibrary(unittest.TestCase):
    """No library available: the degradation path is verified here against
    the real candidate loop, on a host that genuinely lacks the library."""

    @unittest.skipIf(
        _real_libxenstore_present(),
        "a real libxenstore is present (dom0): the unavailable path is "
        "covered by the injected fail-open library in TestOpenFailure")
    def test_degradation_is_clean_and_explicit(self):
        xs = Xenstore()
        self.assertFalse(xs.available)
        self.assertIsNone(xs.library_name)
        self.assertEqual(xs.read_all(), {})
        self.assertEqual(xs.domids(), [])

    def test_open_failure_is_loud_with_errno_message(self):
        xs = Xenstore(library=FakeXenStoreLib(fail_open=True))
        with self.assertRaises(XenstoreUnavailable) as ctx:
            xs.read_all()
        self.assertIn(os.strerror(errno.ECONNREFUSED), str(ctx.exception))
        with self.assertRaises(XenstoreUnavailable):
            xs.domids()


class TestReadAll(unittest.TestCase):
    def test_full_reading(self):
        lib = make_lib(make_keys({
            7: ("work", b"1048576", b"524288"),
            12: ("sys-net", None, None),
        }))
        readings = Xenstore(library=lib).read_all()
        self.assertEqual(sorted(readings), ["sys-net", "work"])
        work = readings["work"]
        self.assertEqual(work.domid, 7)
        self.assertEqual(work.mem_used_kib, 1048576)
        self.assertEqual(work.swap_used_kib, 524288)
        self.assertIsNone(readings["sys-net"].mem_used_kib)
        self.assertIsNone(readings["sys-net"].swap_used_kib)

    def test_memory_ceiling_and_target_are_read(self):
        # D-14: the configured maximum and the live allocation come from
        # xenstore, because on Qubes 4.3 CurrentState's mem/mem_static_max are
        # both libvirt's memory total (the *initial* size for hotplug qubes).
        keys = make_keys({7: ("browser-streaming", b"1153024", None)})
        keys["/local/domain/7/memory/static-max"] = b"1024000"
        keys["/local/domain/7/memory/hotplug-max"] = b"4096000"
        keys["/local/domain/7/memory/target"] = b"1032192"
        reading = Xenstore(library=make_lib(keys)).read_all()["browser-streaming"]
        self.assertEqual(reading.static_max_kib, 1024000)
        self.assertEqual(reading.hotplug_max_kib, 4096000)
        self.assertEqual(reading.target_kib, 1032192)
        # the configured maximum prefers hotplug-max when Qubes wrote one
        self.assertEqual(reading.configured_max_kib(), 4096000)

    def test_missing_memory_ceiling_keys_are_none(self):
        lib = make_lib(make_keys({7: ("work", b"1048576", None)}))
        reading = Xenstore(library=lib).read_all()["work"]
        self.assertIsNone(reading.static_max_kib)
        self.assertIsNone(reading.hotplug_max_kib)
        self.assertIsNone(reading.target_kib)
        self.assertIsNone(reading.configured_max_kib())

    def test_zero_hotplug_max_falls_back_to_static_max(self):
        # A zero key is an absent value, not a ceiling of zero.
        keys = make_keys({7: ("work", b"1048576", None)})
        keys["/local/domain/7/memory/static-max"] = b"8192000"
        keys["/local/domain/7/memory/hotplug-max"] = b"0"
        reading = Xenstore(library=make_lib(keys)).read_all()["work"]
        self.assertEqual(reading.configured_max_kib(), 8192000)

    def test_domain_zero_is_normalised_to_dom0(self):
        lib = make_lib(make_keys({0: (b"Domain-0", None, None)}))
        readings = Xenstore(library=lib).read_all()
        self.assertIn("dom0", readings)
        self.assertNotIn("Domain-0", readings)
        self.assertEqual(readings["dom0"].domid, 0)

    def test_domain_without_name_is_skipped(self):
        lib = make_lib(make_keys({3: (None, b"1024", None)}))
        self.assertEqual(Xenstore(library=lib).read_all(), {})

    def test_non_numeric_domid_entries_are_skipped(self):
        keys = make_keys({5: ("work", b"1024", None)})
        keys["/local/domain/bogus"] = b"x"  # untrusted data, skipped
        lib = make_lib(keys)
        self.assertEqual(Xenstore(library=lib).domids(), [5])

    def test_non_ascii_meminfo_is_none(self):
        lib = make_lib(make_keys({1: ("work", b"12a34", b"512")}))
        reading = Xenstore(library=lib).read_all()["work"]
        self.assertIsNone(reading.mem_used_kib)
        self.assertEqual(reading.swap_used_kib, 512)

    def test_large_value_within_int_limit_parses(self):
        """4200 digits: far beyond any real memory value but within
        CPython's int-from-ASCII default limit (4300) — D-6 accepts it
        (all ASCII digits) and the parse must handle it without
        raising."""
        value = b"9" * 4200
        lib = make_lib(make_keys({1: ("work", value, None)}))
        reading = Xenstore(library=lib).read_all()["work"]
        self.assertEqual(reading.mem_used_kib, int(value))

    def test_value_beyond_int_limit_degrades_to_none(self):
        """The documented deviation from upstream: a value beyond the
        interpreter's int-from-ASCII digit limit (default 4300,
        CPython 3.11+) degrades to None instead of raising — a
        guest-written oversized digit string must not kill the monitor
        (upstream's unbounded int() would raise ValueError)."""
        limit = getattr(sys, "get_int_max_str_digits", None)
        if limit is None or limit() == 0:
            self.skipTest(
                "this interpreter has no int digit limit; the "
                "containment guard is the absence of the limit")
        value = b"9" * (limit() + 1)
        lib = make_lib(make_keys({1: ("work", value, None)}))
        reading = Xenstore(library=lib).read_all()["work"]
        self.assertIsNone(reading.mem_used_kib)

    def test_persistent_failure_raises_with_errno_message(self):
        lib = make_lib(make_keys({1: ("work", b"1024", b"512")}),
                       fail_reads={"/local/domain/1/memory/swapinfo"})
        with self.assertRaises(XenstoreError) as ctx:
            Xenstore(library=lib).read_all()
        self.assertIn(os.strerror(errno.EACCES), str(ctx.exception),
                      "the message must carry the errno (the real "
                      "library sets it; there is no status-code or "
                      "error-message ABI to fall back on)")

    def test_missing_key_is_enoent_not_an_error(self):
        """A missing key (ENOENT) degrades to None; any other errno
        (covered by test_persistent_failure) raises. This pins the
        errno-based failure protocol of the real API."""
        lib = make_lib(make_keys({1: ("work", None, None)}))
        reading = Xenstore(library=lib).read_all()["work"]
        self.assertIsNone(reading.mem_used_kib)
        self.assertIsNone(reading.swap_used_kib)

    def test_one_handle_for_many_ticks(self):
        lib = make_lib(make_keys({1: ("work", b"1024", None)}))
        xs = Xenstore(library=lib)
        for _ in range(3):
            self.assertTrue(xs.read_all())
            self.assertEqual(xs.domids(), [1])
        self.assertEqual(lib.open_count, 1,
                         "one reused handle — never a fork per qube per "
                         "tick (D-11)")
        self.assertEqual(lib.open_flags, [0],
                         "the card's call value: xs_open(0)")

    def test_close_reopens_on_next_use(self):
        lib = make_lib(make_keys({1: ("work", b"1024", None)}))
        xs = Xenstore(library=lib)
        xs.read_all()
        self.assertIsNotNone(lib._handle)
        xs.close()
        self.assertEqual(lib.close_count, 1)
        self.assertFalse(xs.available)
        self.assertIsNone(lib._handle, "xs_close must release the handle")
        xs.read_all()
        self.assertEqual(lib.open_count, 2)
        self.assertEqual(lib.close_count, 1)


class TestNoWriteCalls(unittest.TestCase):
    """D-11: the dom0 user's write capability must stay unused. The
    fake exports the map's write-capability calls and records them;
    the adapter must never execute them."""

    def test_no_write_calls_are_ever_executed(self):
        lib = make_lib(make_keys({1: ("work", b"1024", b"512")}))
        xs = Xenstore(library=lib)
        xs.read_all()
        xs.domids()
        xs.read_all()
        xs.close()
        self.assertEqual(lib.write_calls, [])

    def test_close_path_makes_no_write_calls(self):
        lib = make_lib(make_keys({1: ("work", b"1024", None)}))
        xs = Xenstore(library=lib)
        xs.read_all()
        xs.close()
        self.assertEqual(lib.write_calls, [])


class TestModuleImport(unittest.TestCase):
    def test_module_import_without_qubesadmin(self):
        """The top-level import of this test module (above) executed the
        adapter's top level in a fresh interpreter with no qubesadmin
        (the runner runs each module as its own script; this box has
        no qubesadmin). If the adapter's top level imported it, that
        import would have failed. Absence pins the import-time purity
        (the xenstore adapter is ctypes-only; qubesadmin belongs to the
        sibling adapter's deferred import)."""
        sys.modules.pop("qubesadmin", None)
        self.assertNotIn("qubesadmin", sys.modules)


class TestErrnoFlagIsArmedAtLoad(unittest.TestCase):
    """ctypes bakes errno saving in when the CDLL is created; assigning
    ``func.use_errno = True`` afterwards is silently ignored.

    Why it matters: the adapter distinguishes "key absent" (NULL + ENOENT)
    from "real failure" (NULL + any other errno) via ``ctypes.get_errno()``.
    With the flag missing, get_errno() stays 0, so **every missing key raises**
    — and ``memory/swapinfo`` is absent on every qube without swap, which is
    most of a typical fleet. The Python fake cannot catch this, because
    ``ctypes.set_errno()`` sets the thread-local directly and works either way.
    """

    def setUp(self):
        self.source = (pathlib.Path(__file__).resolve().parents[1]
                       / "qubes_top" / "dom0" / "xenstore_adapter.py")

    def _cdll_calls_in(self, tree, function_name):
        """CDLL(...) calls inside one function — the libc handle used for
        ``free()`` elsewhere deliberately carries no errno flag."""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == function_name:
                return [call for call in ast.walk(node)
                        if isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "CDLL"]
        return None

    def test_every_cdll_load_arms_errno(self):
        tree = ast.parse(self.source.read_text(), str(self.source))
        calls = self._cdll_calls_in(tree, "_ensure_open")
        if calls is None:
            self.fail("_ensure_open not found — guard has nothing to check")
        self.assertTrue(calls, "no ctypes.CDLL call inside _ensure_open")
        for call in calls:
            kwargs = {kw.arg: kw.value for kw in call.keywords}
            self.assertIn("use_errno", kwargs,
                          "CDLL(...) without use_errno=True: errno stays 0 and a "
                          "missing key raises instead of returning None")
            value = kwargs["use_errno"]
            self.assertIsInstance(value, ast.Constant, "use_errno must be a literal True")
            self.assertTrue(getattr(value, "value", False), "use_errno must be True")

    def test_premise_attribute_assignment_does_not_arm_errno(self):
        # Pins the CPython behaviour the adapter's comment relies on. If a
        # future CPython honours the per-symbol attribute, this fails and the
        # note in _ensure_open should be revisited (the load-time flag remains
        # correct either way).
        libc = ctypes.CDLL("libc.so.6")
        fn = libc.open
        fn.argtypes = [ctypes.c_char_p, ctypes.c_int]
        fn.restype = ctypes.c_int
        try:
            fn.use_errno = True  # type: ignore[attr-defined]  # deliberately probing
        except Exception:  # assignment not permitted: certainly not armed
            pass
        ctypes.set_errno(0)
        self.assertEqual(fn(b"/nonexistent-qubes-top-probe", 0), -1)
        self.assertEqual(
            ctypes.get_errno(), 0,
            "per-symbol use_errno now arms errno in this CPython — revisit the "
            "load-time-flag note in xenstore_adapter._ensure_open")


class TestLibcFreeIsBound(unittest.TestCase):
    """The 64-bit truncation bug that crashed the target with SIGSEGV.

    ``free`` declared without argtypes converts the int from a ``c_void_p``
    restype into a C ``int``: the address is truncated to 32 bits and free()
    walks into garbage. It cannot reproduce off-box (the Python fake never
    reaches the free line), so it is pinned here instead.
    """

    def test_free_takes_a_pointer(self):
        from qubes_top.dom0 import xenstore_adapter as xa
        self.assertEqual(xa._libc.free.argtypes, [ctypes.c_void_p])
        self.assertIsNone(xa._libc.free.restype)


if __name__ == "__main__":
    unittest.main()
