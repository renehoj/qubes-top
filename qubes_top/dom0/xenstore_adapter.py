"""Read-only dom0 adapter for the Xen xenstore (ctypes, no fork).

Reads, per domain (domid):

    /local/domain/<domid>/name
    /local/domain/<domid>/memory/meminfo
    /local/domain/<domid>/memory/swapinfo
    /local/domain/<domid>/memory/static-max
    /local/domain/<domid>/memory/hotplug-max
    /local/domain/<domid>/memory/target

and enumerates ``/local/domain`` once per tick. This is the *raw*
hypervisor view: it includes stubdomains (names ending ``-dm``) and dom0
(raw name ``Domain-0``). Filtering and joining by name is the sampler's
job.

The three extra memory keys are the honest source for the figures the
qubesadmin API cannot give on Qubes 4.3 (D-14): ``static-max`` is the
domain's maximum (libxl writes it from the XML ``<memory>``, which is the
*initial* memory when memory hotplug is in use), ``hotplug-max`` is the
configured maximum that Qubes writes for hotplug-enabled qubes
(``qubes/vm/qubesvm.py``: ``str(self.maxmem * 1024)``), and ``target`` is
qmemman's live balloon target — the only per-tick "what it has right now"
value that exists in 4.3. ``meminfo``/``swapinfo`` are guest-authored and
untrusted (D-6).

The same read pattern is used upstream by the mgmt-API stats path
(QubesOS/qubes-core-admin@main, ``qubes/app.py``, ``get_vm_stats``
line 392: reads ``/local/domain/<domid>/name`` via
``get_name_from_domid`` (line 2178), ``.../memory/meminfo`` and
``.../memory/swapinfo`` via the ``memory/{key}`` reader (line 594),
rejects values that are not all-ASCII-digits (``isascii() and isdigit()``,
line 601), maps the raw name ``Domain-0`` to ``dom0`` (line 633), and
marks ``name.endswith("-dm")`` domains as stubdomains (line 638)).
``parse_guest_value`` mirrors that check byte-wise. One deliberate
deviation from upstream: the ``int()`` conversion is wrapped so that a
value beyond the interpreter's int-from-ASCII digit limit (default
4300 digits, CPython 3.11+) degrades to ``None`` instead of raising —
a guest-written 1 MiB digit string must not kill the monitor, and
the cap is the interpreter's documented one, not a second policy.

The bound API is the real libxenstore, verified against source (Xen
master ``tools/libs/store/xs.c`` + ``libxenstore.map``, fetched
2026-10-17):

    struct xs_handle *xs_open(unsigned long flags)      (xs.c:388)
    void xs_close(struct xs_handle *xsh)               (xs.c:459)
    char **xs_directory(h, xs_transaction_t t,
                        const char *path,
                        unsigned int *num)             (xs.c:840)
    void *xs_read(h, xs_transaction_t t,
                  const char *path, unsigned int *len) (xs.c:861)

``xs_transaction_t`` is ``uint32_t`` (``tools/include/xenstore.h:52``,
xen-RELEASE-4.16.1) — the ``t`` slot is a 4-byte unsigned int; this
adapter always passes 0 (no transaction: read-only single requests).

Return/ownership contract (xs.c + the 4.16.1 header comments):

- ``xs_read`` returns a **malloc'd NUL-terminated buffer**; ``*len`` is
  the byte length **excluding** the terminator. The caller must
  ``free()`` it. Read with ``ctypes.string_at(ptr, len)``.
- ``xs_directory`` returns ``char **``: ``xs_directory_common``
  (xs.c:760) is a **single** malloc covering the pointer array *and* the
  NUL-terminated string data following it — one ``free()`` of the array
  frees everything. Freeing the entries individually would be a
  mid-allocation free (heap corruption); the adapter never does that.
- Failure protocol: NULL return with **errno set** (``xs_talkv``:
  "NULL and set errno on error"; server errors are mapped to errno via
  ``get_error`` — a missing key is errno ``ENOENT``). There is no
  status-code ABI and no error-message function in the exported
  surface (the VERS_4.0 map), so none is bound or called.
- The write-capability calls the map exports are never bound or
  executed here (the forbidden-call list is in DESIGN.md; the dom0
  user sits in group ``qubes`` and the socket is
  ``srw-rw---- root:qubes`` — the capability exists and stays
  unused). ``xenstore-ls`` text is never parsed: its ``-p`` prints
  *permissions*, not paths, and the output is dotted-leader with
  variable-width columns.

The soname is resolved by trying the candidates in order — unverified
on the target (Fedora's xen-libs packaging may differ); "no library
found" is a clean, explicit degradation (``available`` stays False,
``read_all()`` returns ``{}``, ``domids()`` returns ``[]``), never an
ImportError at import time. ``xs_open`` returning NULL *with the
library present* is a loud ``XenstoreUnavailable`` instead (a target
anomaly deserves a signal, not a silent half-blind monitor).

Read-only, frozen (DESIGN.md D-11): one handle is opened once and
reused for the adapter's lifetime — never a fork per qube per tick;
the monitor must not perturb what it measures. Every malloc'd return
is freed before the next request, so no per-tick leak.
"""
import ctypes
import ctypes.util
import errno as _errno
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

__all__ = [
    "SONAME_CANDIDATES",
    "DomainReading",
    "Xenstore", "XenstoreUnavailable", "XenstoreError", "parse_guest_value",
]

#: Tried in order; the first one that loads wins. Unverified on the
#: target (Fedora's xen-libs packaging may differ); the staged dom0
#: check (DESIGN.md §7, M2) records which candidate actually loads
#: there.
SONAME_CANDIDATES = (
    "libxenstore3.0.so.0",
    "libxenstore.so.3.0",
    "libxenstore.so",
)

try:
    _libc = ctypes.CDLL(None)
except OSError:
    _libc = ctypes.CDLL("libc.so.6")
# ``free`` MUST be declared with a pointer argument. Without argtypes, ctypes
# converts the Python int returned by a ``c_void_p`` restype into a C ``int``,
# truncating a 64-bit address to 32 bits — then free() walks into garbage and
# the process dies with SIGSEGV (observed on Qubes 4.3.2 as rc=139, only once
# a real library was present: the Python fake never calls this line).
_libc.free.argtypes = [ctypes.c_void_p]
_libc.free.restype = None


class XenstoreUnavailable(RuntimeError):
    """The library is present but ``xs_open`` returned NULL (a target
    anomaly: loud, not silent)."""


class XenstoreError(RuntimeError):
    """A persistent (non-ENOENT) xenstore read failure."""


def parse_guest_value(raw: Optional[bytes]) -> Optional[int]:
    """D-6: a guest-authored value is accepted only when it is all ASCII
    digits; anything else (empty, whitespace, letters, non-ASCII digits,
    non-digits) is rejected as ``None``.

    Mirrors the upstream check byte-wise (``isascii() and isdigit()`` —
    ``qubes/app.py`` line 601): a byte loop over 0x30..0x39, so no
    locale or unicode-digit path can slip in. One deviation from
    upstream (documented in the module docstring): the ``int()``
    conversion degrades to ``None`` on the interpreter's
    int-from-ASCII digit limit instead of raising, so a guest-written
    oversized digit string cannot kill the monitor.
    """
    if raw is None or not raw:
        return None
    for b in raw:
        if b < 0x30 or b > 0x39:
            return None
    try:
        return int(raw)
    except ValueError:
        return None  # int() digit limit (documented interpreter guard)


@dataclass(frozen=True)
class DomainReading:
    """One domain's xenstore reading. ``mem_used_kib`` / ``swap_used_kib``
    are None when the key was absent or its value failed the D-6 check
    (guest-authored data is untrusted). The three ``*_kib`` memory keys are
    absent on some domains (a qube with no qmemman, a stubdomain), so each
    carries its own None."""

    domid: int
    name: str
    mem_used_kib: Optional[int]
    swap_used_kib: Optional[int]
    static_max_kib: Optional[int] = None
    hotplug_max_kib: Optional[int] = None
    target_kib: Optional[int] = None

    def configured_max_kib(self) -> Optional[int]:
        """The domain's configured maximum: ``hotplug-max`` when Qubes wrote
        one (memory-hotplug qubes — the XML ``<memory>`` is only the initial
        size there), else ``static-max``. None when neither exists."""
        for value in (self.hotplug_max_kib, self.static_max_kib):
            if value is not None and value > 0:
                return value
        return None


def candidate_names():
    """Soname resolution: ask the loader first, then the known constants.

    Hardcoding a candidate list assumed Xen's soname. On Qubes 4.3.2 none of
    the three constants loaded even though ``xenstore-read`` works, so the
    dynamic loader is the oracle — ``find_library`` consults ldconfig and
    returns whatever this host actually ships.
    """
    names = []
    try:
        found = ctypes.util.find_library("xenstore")
    except Exception:
        found = None
    if found:
        names.append(found)
    names.extend(n for n in SONAME_CANDIDATES if n not in names)
    return names


class Xenstore:
    """One handle, one dlopen, for the adapter's lifetime.

    Construct with no argument on the target (the soname candidates are
    tried; absence is a clean degradation). Construct with ``library=...``
    to inject a stand-in mirroring the real prototypes (tests).
    """

    def __init__(self, library=None) -> None:
        self._library = library
        self._handle = None
        self._library_name: Optional[str] = None

    # -- status (explicit degradation) ---------------------------------

    @property
    def available(self) -> bool:
        """True once the library is loaded and the handle is open."""
        return self._handle is not None

    @property
    def library_name(self) -> Optional[str]:
        """The soname actually loaded (only meaningful when the library
        was not injected; None while degraded)."""
        return self._library_name

    def close(self) -> None:
        """Close the handle (idempotent). The next read reopens it."""
        if self._handle is not None:
            self._library.xs_close(self._handle)
            self._handle = None

    # -- internals -------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._handle is not None:
            return
        lib = self._library
        name = None
        if lib is None:
            for name in candidate_names():
                try:
                    # ``use_errno=True`` at LOAD time is load-bearing. Setting
                    # ``func.use_errno = True`` afterwards is silently ignored
                    # (proven empirically, pinned by
                    # TestErrnoFlagIsArmedAtLoad in tests/test_xenstore_adapter.py):
                    # without this flag ``ctypes.get_errno()`` stays 0, so a
                    # missing key (ENOENT) would read as errno 0 and raise
                    # XenstoreError instead of returning None — breaking every
                    # qube that has no swapinfo, which is most of them.
                    lib = ctypes.CDLL(name, use_errno=True)
                except OSError:
                    continue
                break
            if lib is None:
                return  # clean degradation: available stays False
        self._library_name = name
        self._bind(lib)
        # Real prototype: xs_open(unsigned long flags) (xs.c:388).
        # 0 = the plain open (the card's call value; no flag bits set).
        handle = lib.xs_open(0)
        if not handle:
            raise XenstoreUnavailable(
                "xs_open failed: %s" % os.strerror(ctypes.get_errno()))
        self._handle = handle

    def _bind(self, lib) -> None:
        # Signatures are bound only for ctypes function pointers (the
        # real library, per the prototypes above). A Python stand-in
        # (tests) has bound methods, which are called as-is — the fake
        # mirrors the real signatures in Python.
        def bind(name, argtypes, restype):
            sym = getattr(lib, name)
            if hasattr(sym, "argtypes"):
                sym.argtypes = argtypes
                sym.restype = restype
                # NB: ``sym.use_errno = True`` here would be a no-op — ctypes
                # bakes errno saving in when the CDLL is created, not per
                # symbol. The flag is set at load time in ``_ensure_open``.

        # xs_transaction_t = uint32_t (xenstore.h:52 @ 4.16.1) -> c_uint.
        bind("xs_open", [ctypes.c_ulong], ctypes.c_void_p)
        bind("xs_close", [ctypes.c_void_p], None)
        bind("xs_read",
             [ctypes.c_void_p, ctypes.c_uint, ctypes.c_char_p,
              ctypes.POINTER(ctypes.c_uint)],
             ctypes.c_void_p)
        bind("xs_directory",
             [ctypes.c_void_p, ctypes.c_uint, ctypes.c_char_p,
              ctypes.POINTER(ctypes.c_uint)],
             ctypes.c_void_p)
        self._library = lib

    def _read_key(self, path: str) -> Optional[bytes]:
        """One key via ``xs_read``: NULL + errno on failure; a
        malloc'd NUL-terminated value on success (*len excludes the
        terminator), freed immediately."""
        self._ensure_open()
        if self._handle is None:
            return None  # degraded: key absent
        length = ctypes.c_uint(0)
        ptr = self._library.xs_read(
            self._handle, 0, path.encode("ascii", "strict"), length)
        if ptr is None:
            code = ctypes.get_errno()
            if code == _errno.ENOENT:
                return None
            raise XenstoreError(
                "xs_read(%r) failed: %s" % (path, os.strerror(code)))
        if isinstance(ptr, int):
            # Real library: read exactly *len bytes (NUL excluded),
            # then free the malloc'd buffer before the next request.
            data = ctypes.string_at(ptr, length.value)
            _libc.free(ptr)
        else:
            data = ptr  # Python stand-in: already the value bytes
        return data

    def _list_directory(self, path: str) -> List[bytes]:
        """Children of ``path`` via ``xs_directory``: NULL + errno on
        failure; ``char **`` on success — a single malloc covering the
        pointer array and the string data (xs.c:760), freed with one
        ``free()`` of the array (freeing the entries individually would
        be a mid-allocation free)."""
        self._ensure_open()
        if self._handle is None:
            return []  # degraded: nothing
        num = ctypes.c_uint(0)
        arr = self._library.xs_directory(
            self._handle, 0, path.encode("ascii", "strict"), num)
        if arr is None:
            code = ctypes.get_errno()
            if code == _errno.ENOENT:
                return []
            raise XenstoreError(
                "xs_directory(%r) failed: %s" % (path, os.strerror(code)))
        if isinstance(arr, int):
            # Real library: one free() of the array frees the strings
            # too (they live in the same allocation).
            char_ptrs = ctypes.cast(arr, ctypes.POINTER(ctypes.c_char_p))
            entries = [char_ptrs[i] for i in range(num.value)]
            _libc.free(arr)
        else:
            entries = arr  # Python stand-in: already a list of bytes
        return [entry for entry in entries if entry is not None]

    # -- per-tick API ----------------------------------------------------

    def domids(self) -> List[int]:
        """``/local/domain`` entries as ints. Non-numeric entries
        (untrusted data) are skipped, never trusted (D-6 discipline)."""
        result = []
        for entry in self._list_directory("/local/domain"):
            value = parse_guest_value(entry)
            if value is not None:
                result.append(value)
        return result

    def read_all(self) -> Dict[str, DomainReading]:
        """One pass per tick: the directory, then name/meminfo/swapinfo per
        domid, on the one reused handle.

        Keyed by domain *name*: the raw name ``Domain-0`` is normalised to
        ``dom0`` (the upstream mapping, ``qubes/app.py`` line 633). A
        domain with no ``name`` key is unkeyable and skipped (the upstream
        reader does the same). Returns ``{}`` while degraded.
        """
        readings: Dict[str, DomainReading] = {}
        for domid in self.domids():
            base = "/local/domain/%d" % domid
            name = self._read_key(base + "/name")
            if name is None:
                continue
            name = name.decode("ascii", "replace")
            if name == "Domain-0":
                name = "dom0"
            readings[name] = DomainReading(
                domid=domid,
                name=name,
                mem_used_kib=parse_guest_value(
                    self._read_key(base + "/memory/meminfo")),
                swap_used_kib=parse_guest_value(
                    self._read_key(base + "/memory/swapinfo")),
                static_max_kib=parse_guest_value(
                    self._read_key(base + "/memory/static-max")),
                hotplug_max_kib=parse_guest_value(
                    self._read_key(base + "/memory/hotplug-max")),
                target_kib=parse_guest_value(
                    self._read_key(base + "/memory/target")),
            )
        return readings
