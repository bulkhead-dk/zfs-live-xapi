#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ctypes bindings for libzfs_core (`liblzc`) -- the structured-signal
layer beneath OpenZFS's CLI tools.

Issue #246 (epic #228 follow-up). The CLI tools (`zfs`, `zpool`)
return only `0`/`1`/`2` exit codes -- they cannot disambiguate
EBUSY from ENOSPC from ENOENT. Their stderr is the only
differentiator, and substring-matching against stderr is fragile
(see #243 / PR #245 for the prior interim hardening). libzfs_core
returns the actual errno from each ioctl, which IS the structured
signal #246 asks for.

This module is deliberately minimal: only the functions that
zfs_operations currently retries on busy. New ports follow as the
migration progresses (see lzc-migration for the plan).

Locale-independence: errnos are integers from the kernel ioctl
layer, completely independent of the caller's `LANG` / `LC_ALL`.
The locale-regression test in #246's acceptance criteria is
trivially satisfied by anything that consumes errno; substring-
match against translated strings is impossible to make
locale-independent without forcing the subprocess locale.

Failure model: if libzfs_core.so cannot be loaded (older OpenZFS
without the shared library, or a packaging accident), the
top-level `available()` returns False and callers fall back to
the CLI path. The shim never crashes the driver because of a
missing optional native dep.
"""

from __future__ import print_function

import ctypes
import ctypes.util
import errno as errno_mod
import logging
import os

log = logging.getLogger("zfs_live.lzc")


# Candidate `libzfs_core.so` SONAMEs. Version 3 is what OpenZFS
# 2.x ships; version 1 was the very early API. ctypes.util.find_library
# also tries (covers distros that don't soname-pin).
_LIB_CANDIDATES = (
    "libzfs_core.so.3",
    "libzfs_core.so.1",
    "libzfs_core.so",
)


# Public errno constants the higher-level zfs_operations consumers care
# about. These are the standard kernel errnos the lzc_* functions
# return verbatim (libzfs_core preserves errno from the ioctl
# layer).
EBUSY = errno_mod.EBUSY  # 16 -- retryable
ENOENT = errno_mod.ENOENT  # 2  -- non-retryable; dataset gone
EEXIST = errno_mod.EEXIST  # 17 -- non-retryable; already exists
EACCES = errno_mod.EACCES  # 13 -- non-retryable; permission

# Errnos the busy-retry path should treat as transient and retry.
RETRYABLE_ERRNOS = frozenset([EBUSY])


_LIB = None
_NVPAIR_LIB = None
_INIT_DONE = False


# Symbols this module wires signatures for. A candidate library
# that doesn't expose ALL of them is rejected as ABI-incompatible
# and the loader continues to the next SONAME -- without this
# check, a host with a loadable-but-old `libzfs_core.so.1` would
# raise `AttributeError` during `_wire_signatures` instead of
# falling back to the CLI path the failure model promises.
_REQUIRED_SYMBOLS = (
    "libzfs_core_init",
    "libzfs_core_fini",
    "lzc_destroy",
    "lzc_exists",
)

# Per-operation symbol requirements. Each migrated lzc primitive
# checks only the symbols it actually needs. Reviewer-flagged
# twice now (#246 phase 4 + phase 5 reviews): collapsing
# multiple operations behind a shared gate downgrades unrelated
# already-migrated paths when one symbol is missing.
#
# Each tuple is `(lzc_symbol, requires_nvpair)`. `requires_nvpair`
# tracks whether the operation builds an nvlist via the
# `fnvlist_*` family (snapshot does -- needs libnvpair) or passes
# NULL (clone in this PR -- doesn't need libnvpair). When more
# operations migrate (create, set_props), each gets its own
# entry here.
#
# See lzc-migration for the design decision behind
# per-operation gates.
_OP_SNAPSHOT = ("lzc_snapshot", True)
_OP_CLONE = ("lzc_clone", False)
_OP_CREATE_ZVOL = ("lzc_create", True)  # builds props nvlist (volsize/volblocksize)


# Companion library -- `libnvpair.so.3` ships alongside
# libzfs_core in OpenZFS userland. We bind the `fnvlist_*`
# "fatal" variants because they abort on alloc failure (cleaner
# ABI: void return for adders, direct pointer return for
# allocator) instead of the int-return / ptr-to-ptr pattern of
# the regular `nvlist_*` family. We catch real failures
# downstream via `lzc_*` return values, so the abort-on-OOM
# behaviour of fnvlist is acceptable.
_NVPAIR_CANDIDATES = (
    "libnvpair.so.3",
    "libnvpair.so.1",
    "libnvpair.so",
)
_NVPAIR_REQUIRED_SYMBOLS = (
    "fnvlist_alloc",
    "fnvlist_free",
    "fnvlist_add_boolean",
)


# Optional nvpair adders -- present on all OpenZFS 2.x SONAME 3
# builds, but the per-operation gates verify presence
# individually so a partial-build host degrades the matching
# operation rather than the whole nvlist surface.
_NVPAIR_OPTIONAL_SYMBOLS = (
    "fnvlist_add_uint64",
    "fnvlist_add_string",
)


def _candidate_has_required_symbols(lib):
    """ctypes resolves symbols lazily via dlsym on first attribute
    access. Touch each required symbol so dlsym fires now; missing
    symbols raise AttributeError which we map to "reject this
    candidate, try the next one"."""
    for name in _REQUIRED_SYMBOLS:
        try:
            getattr(lib, name)
        except AttributeError:
            log.warning(
                "candidate libzfs_core missing required symbol "
                "%s; rejecting (likely older SONAME or ABI-"
                "incompatible build)",
                name,
            )
            return False
    return True


def _try_load_lib():
    """Best-effort load of libzfs_core. Returns the loaded
    ctypes.CDLL (with required-symbol validation passing) or
    None. Cached at module scope; subsequent calls return the
    same handle."""
    global _LIB  # pylint: disable=global-statement
    if _LIB is not None:
        return _LIB
    candidates = list(_LIB_CANDIDATES)
    found = ctypes.util.find_library("zfs_core")
    if found:
        candidates.append(found)
    for soname in candidates:
        try:
            candidate = ctypes.CDLL(soname, use_errno=True)
        except OSError:
            continue
        if not _candidate_has_required_symbols(candidate):
            # Loadable but ABI-incompatible. Try the next SONAME;
            # don't stash this one as `_LIB` because we'd then
            # crash on signature wiring.
            continue
        _LIB = candidate
        log.debug("loaded %s with all required symbols present", soname)
        return _LIB
    log.warning(
        "libzfs_core.so not loadable (or no candidate exposes "
        "all required symbols); busy-retry will fall back to "
        "the CLI substring-match path. Install OpenZFS userland "
        "to enable structured-signal retries."
    )
    return None


def _wire_signatures(lib):
    """Set ctypes argtypes/restype for the functions we use.
    Catches ABI surprises early (e.g. wrong return-type assumptions)
    and lets ctypes.errno_at(...) work correctly."""
    # int libzfs_core_init(void);
    lib.libzfs_core_init.argtypes = []
    lib.libzfs_core_init.restype = ctypes.c_int
    # void libzfs_core_fini(void);
    lib.libzfs_core_fini.argtypes = []
    lib.libzfs_core_fini.restype = None
    # int lzc_destroy(const char *name);
    # OpenZFS 2.x signature -- single-arg destroy. Earlier API
    # drafts had `lzc_destroy_one(name, opts)`; the current
    # SONAME 3 export is the single-arg form.
    lib.lzc_destroy.argtypes = [ctypes.c_char_p]
    lib.lzc_destroy.restype = ctypes.c_int
    # boolean_t lzc_exists(const char *name);
    # boolean_t is uint32 in OpenZFS userland headers.
    lib.lzc_exists.argtypes = [ctypes.c_char_p]
    lib.lzc_exists.restype = ctypes.c_uint
    # `lzc_snapshot` signatures wired lazily inside
    # `_wire_extended_signatures()` -- only after the extended-tier
    # symbol is confirmed present. Wiring it here would crash
    # `_wire_signatures` on older libs that have destroy but
    # not snapshot.


def _wire_extended_signatures(lib):
    """Wire the extended-tier signatures (nvlist-using lzc
    primitives) per-symbol. Each missing symbol is skipped
    rather than blocking the rest -- keeps each migrated
    operation's lzc path independent of the others' symbol
    availability (reviewer-flagged #246 phase 5 review)."""
    try:
        # int lzc_snapshot(nvlist_t *snaps, nvlist_t *props,
        #                  nvlist_t **errlist);
        lib.lzc_snapshot.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.lzc_snapshot.restype = ctypes.c_int
    except AttributeError:
        pass
    try:
        # int lzc_clone(const char *fsname, const char *origin,
        #               nvlist_t *props);
        lib.lzc_clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p]
        lib.lzc_clone.restype = ctypes.c_int
    except AttributeError:
        pass
    try:
        # int lzc_create(const char *fsname,
        #                enum lzc_dataset_type type,
        #                nvlist_t *props,
        #                uint8_t *wkeydata, uint_t wkeylen);
        #
        # OpenZFS 2.x signature has FIVE args -- the trailing
        # `wkeydata` / `wkeylen` are for encryption-key
        # passthrough (we pass NULL/0 since we don't use that
        # path here). Earlier draft of these bindings only
        # declared 3 argtypes; the missing argtypes meant
        # ctypes left the 4th/5th register slots garbage,
        # which lzc_create then read as a uint8 array and
        # tripped a libnvpair assertion mid-call. ABI matters.
        #
        # `type` enum lzc_dataset_type:
        #   LZC_DATSET_TYPE_ZFS = 2 (filesystem)
        #   LZC_DATSET_TYPE_ZVOL = 3 (zvol)
        # Same numeric values as dmu_objset_type_t which the
        # CLI tools use; we just expose ZVOL=3 as DMU_OST_ZVOL
        # for the canonical name.
        lib.lzc_create.argtypes = [
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        lib.lzc_create.restype = ctypes.c_int
    except AttributeError:
        pass


def _wire_nvpair_signatures(lib):
    """ABI declarations for the libnvpair symbols we use.
    fnvlist_* variants are fatal-on-alloc-failure (void return
    for adders, direct ptr return for the allocator) -- simpler
    than the int-return regular nvlist_* family."""
    # nvlist_t *fnvlist_alloc(void);
    lib.fnvlist_alloc.argtypes = []
    lib.fnvlist_alloc.restype = ctypes.c_void_p
    # void fnvlist_free(nvlist_t *);
    lib.fnvlist_free.argtypes = [ctypes.c_void_p]
    lib.fnvlist_free.restype = None
    # void fnvlist_add_boolean(nvlist_t *, const char *);
    # The "boolean" variant adds a name with no value -- the
    # canonical idiom for an nvlist used as a string set
    # (e.g. lzc_snapshot's snap-names list).
    lib.fnvlist_add_boolean.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.fnvlist_add_boolean.restype = None
    # Optional adders -- phase 6+ paths use these for typed
    # property values (volsize, volblocksize as uint64;
    # compression, sync etc. as string). Wired in their own
    # try blocks so a libnvpair without one of them doesn't
    # block the rest.
    try:
        # void fnvlist_add_uint64(nvlist_t *, const char *,
        #                         uint64_t);
        lib.fnvlist_add_uint64.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint64,
        ]
        lib.fnvlist_add_uint64.restype = None
    except AttributeError:
        pass
    try:
        # void fnvlist_add_string(nvlist_t *, const char *,
        #                         const char *);
        lib.fnvlist_add_string.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        lib.fnvlist_add_string.restype = None
    except AttributeError:
        pass


def _try_load_nvpair():
    """Best-effort load of libnvpair, mirroring the ladder used
    for libzfs_core. Returns the loaded lib or None. Validates
    the required-symbols set so a mismatched SONAME falls
    through to the CLI path rather than crashing in
    `_wire_nvpair_signatures`."""
    global _NVPAIR_LIB  # pylint: disable=global-statement
    if _NVPAIR_LIB is not None:
        return _NVPAIR_LIB
    candidates = list(_NVPAIR_CANDIDATES)
    found = ctypes.util.find_library("nvpair")
    if found:
        candidates.append(found)
    for soname in candidates:
        try:
            cand = ctypes.CDLL(soname, use_errno=True)
        except OSError:
            continue
        ok = True
        for name in _NVPAIR_REQUIRED_SYMBOLS:
            try:
                getattr(cand, name)
            except AttributeError:
                log.warning("candidate libnvpair missing required symbol " "%s; rejecting", name)
                ok = False
                break
        if not ok:
            continue
        _NVPAIR_LIB = cand
        log.debug("loaded %s for nvlist support", soname)
        return _NVPAIR_LIB
    log.warning(
        "libnvpair.so not loadable; lzc_snapshot path will fall "
        "back to CLI for snapshot operations."
    )
    return None


def _ensure_init():
    """Call libzfs_core_init once per process. Idempotent -- the
    init path inside libzfs_core itself refcounts, so re-calling
    is safe but wasteful."""
    global _INIT_DONE  # pylint: disable=global-statement
    if _INIT_DONE:
        return
    lib = _try_load_lib()
    if lib is None:
        return
    _wire_signatures(lib)
    rc = lib.libzfs_core_init()
    if rc != 0:
        log.warning("libzfs_core_init returned %d; lzc disabled", rc)
        return
    # Wire extended-tier signatures per-symbol. Each missing
    # symbol is skipped inside `_wire_extended_signatures()`
    # rather than blocking the rest -- keeps each operation's
    # lzc path independent of the others' symbol availability
    # (reviewer-flagged in #246 phase 4 + phase 5 reviews).
    _wire_extended_signatures(lib)
    # libnvpair is loaded lazily -- only the snapshot/clone/
    # create paths need it. If absent, those paths fall back
    # to CLI; destroy paths still go through lzc.
    nvp = _try_load_nvpair()
    if nvp is not None:
        _wire_nvpair_signatures(nvp)
    _INIT_DONE = True


def available():
    """Return True if libzfs_core is loadable and initialised on
    this host. Callers consult this before calling lzc_* -- when
    False, fall back to the CLI path."""
    _ensure_init()
    return _LIB is not None and _INIT_DONE


def destroy_one(name):
    """Destroy a single dataset / snapshot / clone via the
    structured ioctl path. Returns 0 on success or the kernel
    errno on failure.

    `name` is the full ZFS path (`pool/dataset` or
    `pool/dataset@snap`). Passed to the ioctl as UTF-8 bytes.

    EBUSY (16) here is the structured signal the busy-retry
    path consumes. Compare against `RETRYABLE_ERRNOS` to
    decide retry; never against stderr text -- there is no
    stderr from this call path."""
    if not available():
        raise RuntimeError(
            "lzc not available; caller must check available() " "before calling destroy_one()"
        )
    if isinstance(name, str):
        name = name.encode("utf-8")
    rc = _LIB.lzc_destroy(name)
    return rc


def exists(name):
    """Return True if the named dataset / snapshot exists.
    Cheap structural check -- uses the lzc ioctl, no shell-out."""
    if not available():
        raise RuntimeError("lzc not available; caller must check available()")
    if isinstance(name, str):
        name = name.encode("utf-8")
    return bool(_LIB.lzc_exists(name))


def destroy_with_retry(name, max_attempts=10, retry_delay_sec=0.1):
    """Call `destroy_one(name)` with the same backoff curve
    `zfs_operations.run_zfs_command_with_retry()` uses (10 attempts, 0.1s sleep).
    Returns 0 on success or the kernel errno of the last
    attempt. Centralises the retry loop so each migrated
    zfs_operations call site can be a one-liner instead of
    re-implementing the loop in every place.

    Locale-independent by construction -- all arithmetic happens
    on errno integers, never on stderr strings."""
    rc = destroy_one(name)
    if rc == 0:
        return 0
    if rc not in RETRYABLE_ERRNOS:
        return rc
    import time as _time  # pylint: disable=import-outside-toplevel

    for _ in range(max_attempts - 1):
        if retry_delay_sec > 0:
            _time.sleep(retry_delay_sec)
        rc = destroy_one(name)
        if rc == 0:
            return 0
        if rc not in RETRYABLE_ERRNOS:
            return rc
    return rc


def _op_available(op, extra_nvpair_symbols=()):
    """Per-operation availability check. `op` is one of the
    `_OP_*` tuples -- `(lzc_symbol_name, requires_nvpair)`.
    Returns True if the base lzc layer is up, the specific
    `lzc_*` symbol is present, libnvpair is loaded (if the op
    builds an nvlist), AND every nvpair adder in
    `extra_nvpair_symbols` is present (e.g. zvol create needs
    `fnvlist_add_uint64`). Each migrated operation's gate is
    independent of the others' symbol availability."""
    _ensure_init()
    if not available():
        return False
    sym, requires_nvpair = op
    try:
        getattr(_LIB, sym)
    except AttributeError:
        return False
    if requires_nvpair:
        if _NVPAIR_LIB is None:
            return False
        for nvp_sym in extra_nvpair_symbols:
            try:
                getattr(_NVPAIR_LIB, nvp_sym)
            except AttributeError:
                return False
    return True


def snapshot_available():
    """True iff `lzc.snapshot()` can be called: base lzc up,
    `lzc_snapshot` symbol present, libnvpair loaded (we build
    a snaps-nvlist via `fnvlist_*`)."""
    return _op_available(_OP_SNAPSHOT)


def create_zvol_available():
    """True iff `lzc.create_zvol()` can be called: base lzc up,
    `lzc_create` symbol present, libnvpair loaded with
    `fnvlist_add_uint64` (zvol create populates volsize and
    volblocksize as uint64 nvlist entries)."""
    return _op_available(_OP_CREATE_ZVOL, extra_nvpair_symbols=("fnvlist_add_uint64",))


def clone_available():
    """True iff `lzc.clone()` can be called: base lzc up,
    `lzc_clone` symbol present. Does NOT require libnvpair --
    the current implementation passes NULL for the props
    nvlist (reviewer-flagged #246 phase 5 review). When a
    future PR grows property-at-clone-time support, that
    capability will get its own gate."""
    return _op_available(_OP_CLONE)


def nvlist_available():
    """Back-compat alias for `snapshot_available()`. Older
    callers consult this before calling snapshot. Kept so
    `take_snapshot`'s dispatch surface doesn't change shape;
    new callers should use the per-operation predicates."""
    return snapshot_available()


def snapshot(snap_name):
    """Take a single ZFS snapshot via the structured ioctl path.

    `snap_name` is the full `pool/dataset@snap` form. Returns 0
    on success or the kernel errno on failure -- same retry-
    classification surface as `destroy_one()`.

    Builds the snaps-nvlist (single-element, boolean shape per
    lzc_snapshot's ABI), calls lzc_snapshot with NULL props and
    a NULL errlist (we don't need per-snap errors for a single
    snap -- the int return is enough), frees the nvlist before
    returning regardless of outcome.

    Caller must check `snapshot_available()` (or the
    back-compat `nvlist_available()`) before calling. We
    don't fall back inside this function because the caller
    typically wants to know which path was taken (for logging /
    metrics), and the CLI fallback at the call site mirrors the
    pattern used by destroy paths."""
    if not snapshot_available():
        raise RuntimeError(
            "lzc snapshot support not available; caller must "
            "check snapshot_available() before calling snapshot()"
        )
    if isinstance(snap_name, str):
        snap_name_b = snap_name.encode("utf-8")
    else:
        snap_name_b = snap_name
    snaps = _NVPAIR_LIB.fnvlist_alloc()
    try:
        _NVPAIR_LIB.fnvlist_add_boolean(snaps, snap_name_b)
        errlist = ctypes.c_void_p(None)
        rc = _LIB.lzc_snapshot(snaps, None, ctypes.byref(errlist))
        # If errlist got populated on failure, free it. On
        # success it stays NULL and the free is a no-op.
        if errlist.value:  # pylint: disable=using-constant-test
            _NVPAIR_LIB.fnvlist_free(errlist)
        return rc
    finally:
        _NVPAIR_LIB.fnvlist_free(snaps)


def clone(fsname, origin):
    """Clone a snapshot via the structured ioctl path.

    `origin` is the source snapshot (`pool/dataset@snap`),
    `fsname` is the new clone (`pool/clone-name`). Both are
    UTF-8 encoded for the C string arguments. Returns 0 on
    success or the kernel errno on failure.

    This wrapper passes NULL for the property-overrides nvlist
    -- matches the existing `zfs clone snap clone` CLI form
    which carries no property overrides. A future migration
    that wants to pass `compression=zstd` etc. at clone time
    will need to bind `fnvlist_add_string` first (#246 phase
    6+ scope; binding patterns in nvlist-and-fnvlist).

    Caller must check `clone_available()` before calling. The
    clone path doesn't construct an nvlist (props is NULL), so
    libnvpair is NOT required -- the gate is narrower than
    snapshot's."""
    if not clone_available():
        raise RuntimeError(
            "lzc clone support not available; caller must "
            "check clone_available() before calling clone()"
        )
    if isinstance(fsname, str):
        fsname = fsname.encode("utf-8")
    if isinstance(origin, str):
        origin = origin.encode("utf-8")
    return _LIB.lzc_clone(fsname, origin, None)


def clone_with_retry(fsname, origin, max_attempts=10, retry_delay_sec=0.1):
    """Call `clone(fsname, origin)` with the same backoff curve
    `destroy_with_retry` / `snapshot_with_retry` use. Returns 0
    on success or the kernel errno of the last attempt."""
    rc = clone(fsname, origin)
    if rc == 0:
        return 0
    if rc not in RETRYABLE_ERRNOS:
        return rc
    import time as _time  # pylint: disable=import-outside-toplevel

    for _ in range(max_attempts - 1):
        if retry_delay_sec > 0:
            _time.sleep(retry_delay_sec)
        rc = clone(fsname, origin)
        if rc == 0:
            return 0
        if rc not in RETRYABLE_ERRNOS:
            return rc
    return rc


DMU_OST_ZVOL = 3  # dmu_objset_type_t enum -- zvol


def create_zvol(name, size_bytes, volblocksize=8192):
    """Create a zvol via the structured ioctl path.

    Builds a properties nvlist with `volsize` (uint64) and
    `volblocksize` (uint64), then calls `lzc_create(name, ZVOL,
    props)`. Returns 0 on success or the kernel errno on
    failure.

    Phase-6 scope is the simple no-extra-property case (just
    size and block size). Callers that want compression /
    copies / sync overrides must fall back to CLI: those are
    PROP_TYPE_INDEX in OpenZFS, and the kernel's
    `zfs_set_prop_nvlist()` rejects DATA_TYPE_STRING for
    PROP_TYPE_INDEX -- the CLI path runs `zfs_valid_proplist()`
    first to convert them to their internal uint64 enum
    values. Doing the same in this layer would require a
    project-side property-name -> enum table that mirrors
    OpenZFS's; until that lands (post-v1.0 follow-up to #254),
    overrides stay on the CLI path. See lzc-migration
    "Why phase 7 needs a property-validation layer first" for
    the full diagnosis."""
    if not create_zvol_available():
        raise RuntimeError(
            "lzc create_zvol support not available; caller "
            "must check create_zvol_available() before calling"
        )
    if isinstance(name, str):
        name = name.encode("utf-8")
    # Round volsize up to a multiple of volblocksize. The CLI
    # (`zfs create -V`) normalises through libzfs before issuing
    # the ioctl; lzc_create does not, so a non-aligned size that
    # used to succeed on the CLI path would fail here with
    # EINVAL. Match the CLI contract so dispatch is observably
    # identical regardless of which path create_zvol picks.
    size_bytes = int(size_bytes)
    volblocksize = int(volblocksize)
    if volblocksize > 0 and size_bytes % volblocksize != 0:
        size_bytes = ((size_bytes + volblocksize - 1) // volblocksize) * volblocksize
    props = _NVPAIR_LIB.fnvlist_alloc()
    try:
        _NVPAIR_LIB.fnvlist_add_uint64(props, b"volsize", size_bytes)
        _NVPAIR_LIB.fnvlist_add_uint64(props, b"volblocksize", volblocksize)
        # NULL/0 for wkeydata / wkeylen -- the unencrypted-zvol
        # path. Passing these explicitly is non-negotiable: the
        # OpenZFS 2.x ABI has them in the signature whether or
        # not the caller uses encryption.
        rc = _LIB.lzc_create(name, DMU_OST_ZVOL, props, None, 0)
        return rc
    finally:
        _NVPAIR_LIB.fnvlist_free(props)


def create_zvol_with_retry(
    name, size_bytes, volblocksize=8192, max_attempts=10, retry_delay_sec=0.1
):
    """Call `create_zvol()` with the standard retry curve."""
    rc = create_zvol(name, size_bytes, volblocksize)
    if rc == 0:
        return 0
    if rc not in RETRYABLE_ERRNOS:
        return rc
    import time as _time  # pylint: disable=import-outside-toplevel

    for _ in range(max_attempts - 1):
        if retry_delay_sec > 0:
            _time.sleep(retry_delay_sec)
        rc = create_zvol(name, size_bytes, volblocksize)
        if rc == 0:
            return 0
        if rc not in RETRYABLE_ERRNOS:
            return rc
    return rc


def snapshot_with_retry(snap_name, max_attempts=10, retry_delay_sec=0.1):
    """Call `snapshot(snap_name)` with the same backoff curve
    `destroy_with_retry()` uses. Returns 0 on success or the
    kernel errno of the last attempt. Centralised so each
    migrated snapshot call site is a one-liner."""
    rc = snapshot(snap_name)
    if rc == 0:
        return 0
    if rc not in RETRYABLE_ERRNOS:
        return rc
    import time as _time  # pylint: disable=import-outside-toplevel

    for _ in range(max_attempts - 1):
        if retry_delay_sec > 0:
            _time.sleep(retry_delay_sec)
        rc = snapshot(snap_name)
        if rc == 0:
            return 0
        if rc not in RETRYABLE_ERRNOS:
            return rc
    return rc


def errno_to_str(rc):
    """Render an errno integer as the canonical strerror text.
    For logging only -- the retry decision uses the integer
    directly (locale-independent), this is operator-readable
    cosmetic."""
    if rc == 0:
        return "ok"
    try:
        return os.strerror(rc)
    except (ValueError, OverflowError):
        return "errno {}".format(rc)
