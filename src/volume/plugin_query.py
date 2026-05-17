#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Plugin.Query response builder.

Issue #231 / epic #228. Lives in its own module -- without any
`xapi.*` import -- so unit tests can call `build_query_response()`
directly. plugin is the thin xapi-side wrapper that wires
this into the SMAPIv3 Plugin_skeleton.
"""

from __future__ import print_function

try:
    from . import zfs_features
except (ImportError, ValueError):
    import zfs_features


# Static SMAPIv2 capability strings. These are the ones xapi's
# `sm_features.ml` parser recognises. `VDI_COPY` (#89) is
# intercepted by the xe CLI wrapper; `VDI_CONFIG_CBT` (#115) is
# dispatched natively via symlinks. All depend on the driver's
# wire implementation, not the underlying ZFS version, so they're
# host-independent.
BASE_FEATURES = [
    "VDI_CREATE",
    "VDI_DESTROY",
    "VDI_RESIZE",
    "VDI_SNAPSHOT",
    "VDI_CLONE",
    "VDI_COPY",
    "VDI_MIRROR",
    "VDI_MIRROR_IN",
    "VDI_CONFIG_CBT",
]


def binary_compression_algorithms(version_tuple):
    """Compression algorithms this OpenZFS *binary* knows about.

    Reviewer-flagged scope: this is a binary-level diagnostic, not a
    pool-level capability claim. A pool with
    `feature@zstd_compress=disabled` would still refuse
    `compression=zstd` even though the binary supports it. The
    actual pool-level state lives in #230's per-SR sidecar
    (`/var/run/sr-mount/<sr-uuid>/zfs_features.json`); Plugin.Query
    has no SR context, so it can only honestly answer the
    binary-level question.

    Operators reading `xe sm-list` see this prefixed `binary_*` so
    the binary-vs-pool distinction is unambiguous."""
    algos = ["off", "on", "lz4", "gzip", "zle", "lzjb"]
    if version_tuple is not None and version_tuple >= (2, 0, 0):
        algos.insert(2, "zstd")  # right after 'on'
    return algos


def binary_default_compression(version_tuple):
    """Default the operator gets if they don't specify one -- at the
    binary level. zstd is the better default on >= 2.0 (better
    ratio at similar speed on modern CPUs); lz4 is the right
    default below -- it's been on by default since 0.6.5.

    Same caveat as `binary_compression_algorithms`: this is the
    binary's preferred default. The actual default chosen at
    `zfs create` time can differ if the pool has the corresponding
    feature flag disabled."""
    if version_tuple is not None and version_tuple >= (2, 0, 0):
        return "zstd"
    return "lz4"


def build_query_response(dbg, probe_version=None):
    """Build the Plugin.Query response.

    `probe_version` is the version-probe callable (defaults to
    `zfs_features.probe_zfs_version`); the test suite swaps it
    for a stub. Real callers leave it `None`.

    The dynamic `configuration` keys are deliberately scoped to
    *binary-level* facts (what this OpenZFS install can do in
    principle), not pool-level capabilities. Plugin.Query has no
    SR context -- there's no single pool to inspect -- so any
    pool-level claim made here would be a guess that risks
    contradicting the actual pool's feature state. The per-SR
    sidecar from #230 is where pool-level capability advertising
    belongs (#231 follow-up); this surface tells the operator
    what the binary supports and explicitly leaves "is it active
    on YOUR pool?" to the SR-context surfaces."""
    if probe_version is None:
        probe_version = zfs_features.probe_zfs_version
    version_tuple = probe_version(dbg)

    config = {}
    if version_tuple is not None:
        config["zfs_version"] = "{}.{}.{}".format(*version_tuple)
        config["binary_compression_algorithms"] = ",".join(
            binary_compression_algorithms(version_tuple)
        )
        config["binary_default_compression"] = binary_default_compression(version_tuple)
        config["binary_supports_encryption"] = (
            "yes" if version_tuple >= (0, 8, 0) else "no"
        )
        config["binary_supports_block_cloning"] = (
            "yes" if version_tuple >= (2, 2, 0) else "no"
        )
        # Operator hint pointing to where pool-level capability
        # answers live. Keeps `xe sm-list` honest -- a curious
        # operator reads this string and finds the actual
        # per-pool truth instead of trusting a binary-level hint.
        config["pool_capabilities_note"] = (
            "binary_* fields above describe what this OpenZFS "
            "install supports; per-pool capability state lives "
            "in /var/run/sr-mount/<sr-uuid>/zfs_features.json"
        )
    else:
        # Probe failed -- degrade to a truthful "unknown" rather
        # than guessing. Operators reading `xe sm-list` see the
        # gap and can chase the warning in syslog.
        config["zfs_version"] = "unknown"

    return {
        "plugin": "zfs-live",
        "name": "ZFS-Live Storage Driver",
        "description": (
            "Native ZFS zvol-backed block storage with per-VDI compression, snapshots, clones, and CBT"
        ),
        "vendor": "Moksha",
        "copyright": "(C) 2026 Moksha",
        "version": "3.0",
        "required_api_version": "5.0",
        "features": list(BASE_FEATURES),
        "configuration": config,
        "required_cluster_stack": [],
    }
