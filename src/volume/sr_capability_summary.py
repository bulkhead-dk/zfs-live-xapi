#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Per-pool capability summary string -- what `SR.stat` puts into
`health[1]` for `xe sr-param-list` to surface.

Issue #231 follow-on. Plugin.Query speaks at the binary level
(`binary_*` configuration fields) because it has no SR context;
SR.stat *does* have SR context, so it gets to publish the
authoritative per-pool capability state via this helper.

Reads #230's per-SR sidecar via `zfs_features.get_cached(sr_uuid)`.
Silent on missing/unreadable sidecar -- truthful absence beats
fabricating from binary-level guesses.

Self-contained on purpose (no xapi.* imports), same shape as
zfs_features -- keeps the helper testable without xapi
stubbing.
"""

from __future__ import print_function

try:
    from . import zfs_features
except (ImportError, ValueError):
    import zfs_features


def format_capability_summary(sr_uuid):
    """Render a compact "zfs=X.Y.Z; on=[..]; off=[..]" line for the
    SR.stat health-detail field. Returns the empty string when the
    sidecar isn't present -- operators see no false claim."""
    if not sr_uuid:
        return ""
    try:
        features = zfs_features.get_cached(sr_uuid)
    except Exception:  # pylint: disable=broad-except
        return ""
    version = features.get("zfs_version")
    if version is None:
        return ""
    caps = features.get("capabilities") or {}
    on = sorted(name for name, present in caps.items() if present)
    off = sorted(name for name, present in caps.items() if not present)
    return "zfs={}; on=[{}]; off=[{}]".format(version, ", ".join(on), ", ".join(off))
