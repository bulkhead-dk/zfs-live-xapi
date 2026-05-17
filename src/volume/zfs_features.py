#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
OpenZFS feature detection -- probe pool capabilities at SR.attach.

Issue #230 / epic #228 (universal version matrix). Mirrors the shape
of `src/shim/xapi_compat.py` (issue #229) on the ZFS side: at SR
attach time, probe the OpenZFS version and the pool's `feature@*`
flag set, derive a small dict of capability bits the rest of the
driver can read, and cache the result per SR for the session
lifetime.

Consumers:

  - Today (#230): a debug-log line summarising what the driver
    detected on this pool.
  - Future (#231): dynamic `Plugin.Query` advertises only what
    THIS pool can actually do -- e.g. add `VDI_ENCRYPT` to features
    when the pool has `feature@encryption` active, default
    compression to zstd when `feature@zstd_compress` is active.
  - Future feature-gated code paths (e.g. fast-clone via block
    cloning when `feature@block_cloning` is active) read the same
    dict instead of re-probing.

The probe is best-effort: any sub-step that fails degrades to
"feature unknown / not advertised" so the driver never claims a
capability it cannot back. This matches `xapi_compat`'s "assume
gap is present" failure model -- uncertainty defaults to the safer
choice.

Cache shape: keyed by SR UUID. SRs on the same pool will probe
the same data twice, but pool-property changes between attaches
should be visible (operators can re-run `zpool upgrade` between
detach/attach cycles); session-lifetime caching is per-SR rather
than per-pool to keep the lookup contract simple.
"""

from __future__ import print_function

import errno
import json
import logging
import os
import re
import subprocess

log = logging.getLogger("zfs_features")


# zfs_features is self-contained on purpose: both volume and
# datapath plugin scripts need to read the sidecar (volume writes
# it from SR.attach; datapath consumers like #231 read it), and
# they live in different installed directories on disk
# (`/usr/libexec/xapi-storage-script/volume/<plugin>/` vs.
# `/usr/libexec/xapi-storage-script/datapath/<plugin>/`). Avoiding
# a `zfs_operations` import means a single small file can be dropped
# into either install location without dragging in xapi-side
# imports `zfs_operations` carries (#230 review round 4).

MOUNT_ROOT = "/var/run/sr-mount"


def _sr_mount_path(sr_uuid):
    return os.path.join(MOUNT_ROOT, sr_uuid)


def _run(dbg, cmd):
    """Tiny `subprocess.check_output` wrapper for the probe's two
    read-only ZFS queries. No retry-on-busy because `zfs version`
    and `zpool get all` are non-mutating and don't hit the busy
    paths zfs_operations.run_zfs_command_with_retry handles for write operations."""
    log.debug("%s: zfs-features: running %s", dbg, " ".join(cmd))
    proc = subprocess.Popen(  # pylint: disable=consider-using-with
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True
    )
    stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            "{} exited {}: {}".format(" ".join(cmd), proc.returncode, stderr)
        )
    return stdout


# Canonical OpenZFS version-gated capabilities. Keyed by capability
# bit name; each entry says how to derive its value from the probe
# inputs (feature flags + version tuple).
#
# Encryption, zstd, block_cloning are detectable directly from
# `feature@*` flags. The version-gated ones (e.g. lz4 always present
# on >=0.6.5) are sanity backstops when the feature flag scan fails.
KNOWN_FEATURE_FLAGS = (
    "feature@lz4_compress",
    "feature@zstd_compress",
    "feature@encryption",
    "feature@block_cloning",
    "feature@large_blocks",
    "feature@large_dnode",
    "feature@async_destroy",
    "feature@embedded_data",
    "feature@extensible_dataset",
    "feature@hole_birth",
    "feature@spacemap_v2",
    "feature@livelist",
    "feature@bookmarks",
    "feature@bookmark_v2",
    "feature@bookmark_written",
)

# A feature is considered "available" for advertisement when its
# `feature@*` value is in this set. `enabled` means the on-disk
# format permits it; `active` means at least one dataset uses it.
# `disabled` is excluded: an admin explicitly turned it off.
_FEATURE_ON_STATES = frozenset(["enabled", "active"])


_RE_VERSION = re.compile(r"zfs[_-]?(\d+)\.(\d+)(?:\.(\d+))?")


# Process-local cache. Keyed by SR UUID. Backed by an on-disk
# JSON sidecar under the SR mountpoint so a fresh interpreter
# spawned by `xapi-storage-script` (one process per RPC) reads
# the same probe result as SR.attach wrote -- the in-memory dict
# is just a fast-path for callers that already touched it in this
# process.
_CACHED = {}

# Filename for the on-disk sidecar. Lives directly under the SR
# mountpoint (`/var/run/sr-mount/<sr-uuid>/zfs_features.json`),
# next to the libcow metabase. Cleared on `SR.detach` because the
# mountpoint goes away with the dataset unmount.
_SIDECAR_FILENAME = "zfs_features.json"


def _features_path(sr_uuid):
    """On-disk sidecar path for `sr_uuid`'s probe result. Sits next
    to the libcow metabase under the SR mountpoint."""
    return os.path.join(_sr_mount_path(sr_uuid), _SIDECAR_FILENAME)


def set_cached(sr_uuid, features):
    """Store the per-SR probe result both in memory and on disk so
    later RPC handlers (`Plugin.Query`, `Volume.*`, datapath) -- each
    spawned in a fresh interpreter by `xapi-storage-script` -- can
    read the same dict via `get_cached(sr_uuid)`. Call once from
    `SR.attach` after `probe()`."""
    _CACHED[sr_uuid] = features
    path = _features_path(sr_uuid)
    try:
        with open(path, "w") as fh:  # pylint: disable=unspecified-encoding
            json.dump(features, fh, default=_jsonable, sort_keys=True)
    except (OSError, IOError) as exc:
        log.warning("zfs-features: could not persist sidecar %s: %s", path, exc)


def get_cached(sr_uuid):
    """Return the cached probe result for `sr_uuid`. Tries the
    in-process cache first, then the on-disk sidecar SR.attach
    wrote, then falls back to a conservative empty-features dict
    (every capability `False`) so the driver never claims a
    capability it cannot back."""
    cached = _CACHED.get(sr_uuid)
    if cached is not None:
        return cached
    on_disk = _read_sidecar(sr_uuid)
    if on_disk is not None:
        _CACHED[sr_uuid] = on_disk
        return on_disk
    return _empty_features()


def _read_sidecar(sr_uuid):
    path = _features_path(sr_uuid)
    try:
        with open(path) as fh:  # pylint: disable=unspecified-encoding
            data = json.load(fh)
    except (OSError, IOError) as exc:
        if getattr(exc, "errno", None) != errno.ENOENT:
            log.warning("zfs-features: could not read sidecar %s: %s", path, exc)
        return None
    except ValueError as exc:  # JSON decode error
        log.warning("zfs-features: sidecar %s corrupt: %s", path, exc)
        return None
    # JSON has no tuple type, so `version_tuple` round-trips as a
    # list. Normalise back so cross-process consumers can do the
    # same `features["version_tuple"] >= (2, 2, 0)` comparison
    # that same-process consumers do -- a list-vs-tuple comparison
    # is a TypeError on Python 3.
    vt = data.get("version_tuple")
    if isinstance(vt, list):
        data["version_tuple"] = tuple(vt)
    return data


def clear_cached(sr_uuid):
    """Drop the in-memory entry for `sr_uuid` and unlink its on-disk
    sidecar. Called from `SR.detach` so a re-attach observes a fresh
    probe rather than an entry that survives `zpool upgrade` /
    OpenZFS package updates between detach and re-attach."""
    _CACHED.pop(sr_uuid, None)
    path = _features_path(sr_uuid)
    try:
        os.unlink(path)
    except OSError as exc:
        if getattr(exc, "errno", None) != errno.ENOENT:
            log.warning("zfs-features: could not unlink sidecar %s: %s", path, exc)


def reset_cached():
    """Clear the in-memory cache. Test-only; production code uses
    `clear_cached(sr_uuid)` so the on-disk sidecar is also removed."""
    _CACHED.clear()


def _jsonable(value):
    """`set` and tuple values appear in our dict; serialise both as
    lists so the sidecar is plain JSON readable from any tool."""
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError("not JSON serialisable: " + repr(type(value)))


def _empty_features():
    return {
        "zfs_version": None,
        "version_tuple": None,
        "feature_flags": {},
        "capabilities": {
            "lz4": False,
            "zstd": False,
            "encryption": False,
            "block_cloning": False,
            "large_blocks": False,
        },
    }


def probe_zfs_version(dbg):
    """Return the OpenZFS version as a (major, minor, patch) tuple,
    or `None` on failure.

    `zfs version` on dom0 emits:

        zfs-2.1.5-1
        zfs-kmod-2.1.5-1

    First line is the userspace version; second is the kernel-
    module version (usually identical on a sane install but worth
    keeping the userspace-side as canonical for advertising).
    """
    try:
        out = _run(dbg, ["zfs", "version"])
    except Exception as exc:  # pylint: disable=broad-except
        log.warning("%s: zfs-features probe: `zfs version` failed: %s", dbg, exc)
        return None
    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    first = out.splitlines()[0] if out else ""
    match = _RE_VERSION.search(first)
    if not match:
        return None
    major, minor, patch = match.group(1), match.group(2), match.group(3)
    return (int(major), int(minor), int(patch) if patch else 0)


def probe_pool_features(dbg, pool_name):
    """Return a dict of `feature@<name>` -> state for `pool_name`.

    `zpool get -H -p -o property,value all <pool>` emits one tab-
    separated row per property; we filter to `feature@*` entries.
    """
    cmd = ["zpool", "get", "-H", "-p", "-o", "property,value", "all", pool_name]
    try:
        out = _run(dbg, cmd)
    except Exception as exc:  # pylint: disable=broad-except
        log.warning(
            "%s: zfs-features probe: `zpool get all %s` " "failed: %s",
            dbg,
            pool_name,
            exc,
        )
        return {}
    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    flags = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        prop, value = parts[0], parts[1]
        if prop.startswith("feature@"):
            flags[prop] = value
    return flags


def _derive_capabilities(flags, version_tuple):
    """Translate raw `feature@*` flags + version into the
    capability-bit dict consumers actually read."""
    lz4_state = flags.get("feature@lz4_compress")
    if lz4_state in _FEATURE_ON_STATES:
        lz4_on = True
    elif lz4_state is None:
        # Pool-feature scan failed entirely (probe couldn't reach
        # `zpool get all`). Fall back to the version backstop:
        # lz4 has shipped enabled-by-default since 0.6.5 (2014),
        # so on a modern kernel claiming it is safe.
        lz4_on = version_tuple is not None and version_tuple >= (0, 6, 5)
    else:
        # Explicit pool state (`disabled`) -- never override. If an
        # admin disabled lz4 on this pool, advertising it would let
        # consumers like #231 promise capabilities a later
        # `compression=lz4` set call would refuse.
        lz4_on = False
    caps = {
        "lz4": lz4_on,
        "zstd": flags.get("feature@zstd_compress") in _FEATURE_ON_STATES,
        "encryption": flags.get("feature@encryption") in _FEATURE_ON_STATES,
        "block_cloning": (flags.get("feature@block_cloning") in _FEATURE_ON_STATES),
        "large_blocks": (flags.get("feature@large_blocks") in _FEATURE_ON_STATES),
    }
    return caps


def probe(dbg, sr_uuid, pool_name):
    """Probe `pool_name` once and return a `zfs_features` dict:

        {
            "zfs_version": "2.1.5",
            "version_tuple": (2, 1, 5),
            "feature_flags": {"feature@encryption": "active", ...},
            "capabilities": {
                "lz4":           True,
                "zstd":          True,
                "encryption":    True,
                "block_cloning": False,
                "large_blocks":  True,
            },
        }

    `sr_uuid` is recorded so callers can use the same value with
    `set_cached(sr_uuid, ...)` after attach. The probe itself
    doesn't write to the cache -- the SR.attach handler controls
    when that happens (after the rest of attach succeeds).
    """
    version_tuple = probe_zfs_version(dbg)
    flags = probe_pool_features(dbg, pool_name)
    caps = _derive_capabilities(flags, version_tuple)
    return {
        "zfs_version": ("{}.{}.{}".format(*version_tuple) if version_tuple else None),
        "version_tuple": version_tuple,
        # Restrict the stored flags to the canonical subset so the
        # cache footprint stays bounded -- the full `zpool get all`
        # output is large and most of it is noise for capability
        # advertising.
        "feature_flags": {k: flags[k] for k in KNOWN_FEATURE_FLAGS if k in flags},
        "capabilities": caps,
    }


def log_summary(dbg, features):
    """Emit a single human-readable debug line describing the probe
    result. Operators grep this when troubleshooting why a feature
    they expected isn't advertised."""
    version = features.get("zfs_version") or "unknown"
    caps = features.get("capabilities") or {}
    on = sorted(name for name, present in caps.items() if present)
    off = sorted(name for name, present in caps.items() if not present)
    log.info(
        "%s: zfs-features: zfs=%s; on=[%s]; off=[%s]",
        dbg,
        version,
        ", ".join(on),
        ", ".join(off),
    )
