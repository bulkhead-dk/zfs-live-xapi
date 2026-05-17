#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
XAPI capability probe -- detect upstream dispatch gaps.

Called inline by the `xe` CLI wrapper when no sidecar cache exists.
Results are written to a JSON sidecar so subsequent wrapper
invocations skip the probe. Consumers:

  - (#232): graceful retirement -- when xapi grows native support
    for a previously-broken method (e.g. VDI_COPY), the xe wrapper
    steps aside instead of intercepting.

The probe is intentionally cheap: one `xapi --version` call and one
`grep -aoE` over the xapi binary to enumerate the recognised
feature-name strings. Results are cached in-process and on disk.
"""

from __future__ import print_function

import errno
import json
import logging
import os
import re
import subprocess

XAPI_BINARY = "/opt/xensource/bin/xapi"

# On-disk sidecar location. The `xe` wrapper writes here after an
# inline probe so subsequent invocations skip the probe.
SIDECAR_PATH = "/var/run/xapi-storage-script-shim/xapi_compat.json"


# Module-level cache. The xe wrapper calls `set_cached()` after an
# inline probe when no sidecar exists. `None` until `set_cached()`
# runs -- `get_cached()` returns a conservative fallback (all gaps
# active) in that state.
_CACHED = None


def set_cached(compat, sidecar_path=SIDECAR_PATH):
    """Store the probe result in memory and on disk. The xe wrapper
    calls this after an inline probe; subsequent invocations read
    the sidecar via `get_cached()` without re-probing."""
    global _CACHED  # pylint: disable=global-statement
    _CACHED = compat
    if sidecar_path is None:
        return
    try:
        os.makedirs(os.path.dirname(sidecar_path), exist_ok=True)
        with open(sidecar_path, "w", encoding="utf-8") as fh:
            json.dump(compat, fh, default=_jsonable, sort_keys=True)
    except (OSError, IOError) as exc:
        # Sidecar write failure is non-fatal -- the in-memory cache
        # still serves same-process callers; next invocation retries.
        logging.getLogger("shim.xapi_compat").warning(
            "could not persist sidecar %s: %s", sidecar_path, exc
        )


def get_cached(sidecar_path=SIDECAR_PATH):
    """Return the cached probe result. Lookup order:

      1. In-process `_CACHED` (fast path -- same process that
         called `set_cached`).
      2. On-disk sidecar (cross-process -- what xe-wrapper sees on
         every operator `xe` invocation).
      3. Conservative fallback that assumes every known gap is
         still present, so the shim keeps providing the workaround
         when both caches are empty.

    The fallback shape matches `probe()` output so consumers don't
    need to special-case the empty-cache state."""
    if _CACHED is not None:
        return _CACHED
    on_disk = _read_sidecar(sidecar_path)
    if on_disk is not None:
        return on_disk
    return {
        "xapi_version": None,
        "recognised_features": set(),
        "gaps": {method: True for method, _ in KNOWN_GAPS},
        "active_shims": [method for method, _ in KNOWN_GAPS],
    }


def _read_sidecar(sidecar_path):
    if not sidecar_path:
        return None
    try:
        with open(sidecar_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, IOError) as exc:
        if getattr(exc, "errno", None) != errno.ENOENT:
            logging.getLogger("shim.xapi_compat").warning(
                "could not read sidecar %s: %s", sidecar_path, exc
            )
        return None
    except ValueError as exc:  # JSON decode error
        logging.getLogger("shim.xapi_compat").warning(
            "sidecar %s corrupt: %s", sidecar_path, exc
        )
        return None
    # JSON has no `set` type; the recognised_features field round-
    # trips as a list. Normalise back so consumers can do membership
    # tests with the same shape probe() returned in-process.
    rf = data.get("recognised_features")
    if isinstance(rf, list):
        data["recognised_features"] = set(rf)
    return data


def is_gap_filled(method, sidecar_path=SIDECAR_PATH):
    """Return True if the dispatch gap for `method` is still active
    on this xapi version, False if xapi has grown native dispatch
    and the wrapper can step aside.

    Consumers (#232 graceful retirement): xe-wrapper checks this
    before intercepting `xe vdi-copy` -- when xapi natively recognises
    VDI_COPY, the wrapper passes through to xe.real instead of
    running the fast-path interception that's no longer needed.

    Defaults to True (gap present) when the cache is empty --
    matching the rest of the failure model. The shim always errs
    on the side of providing the workaround under uncertainty."""
    cache = get_cached(sidecar_path)
    return bool(cache.get("gaps", {}).get(method, True))


def reset_cached(sidecar_path=SIDECAR_PATH):
    """Clear the in-memory cache and unlink the on-disk sidecar.
    Test-only; not called from production code."""
    global _CACHED  # pylint: disable=global-statement
    _CACHED = None
    if sidecar_path:
        try:
            os.unlink(sidecar_path)
        except OSError as exc:
            if getattr(exc, "errno", None) != errno.ENOENT:
                logging.getLogger("shim.xapi_compat").warning(
                    "could not unlink sidecar %s: %s", sidecar_path, exc
                )


def _jsonable(value):
    """`set` round-trips through JSON as a sorted list. Preserves
    deterministic order so test assertions don't see flapping."""
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError("not JSON serialisable: " + repr(type(value)))


# Known dispatch gaps the xe wrapper / driver work around. Each
# entry is (method_name, feature_flag) where `feature_flag` is the
# string xapi's `sm_features.ml` parser would need to recognise for
# the call to route to an SM driver natively.
#
# `None` for the feature flag means the gap is in the xapi-storage-
# script OCaml dispatcher rather than xapi's feature-name parser
# (see #80 for `VDI.similar_content`); we can't probe those by
# inspecting the xapi binary, so the wrapper always assumes the gap
# is active for those entries.
KNOWN_GAPS = (
    ("VDI.copy", "VDI_COPY"),  # #89
    ("VDI.list_changed_blocks", "VDI_CONFIG_CBT"),  # #115
    ("VDI.similar_content", None),  # #80
)


_RE_FEATURE_NAME = re.compile(r"^VDI_[A-Z_]+(/[0-9])?$")

# Runs of printable ASCII used as a Python-side `strings(1)`
# fallback when /usr/bin/strings is unavailable. 4+ printable
# bytes matches GNU strings' default minimum length.
_RE_PRINTABLE_RUN = re.compile(rb"[\x20-\x7e]{4,}")


def _run(cmd, log):
    """Tiny `subprocess.check_output` wrapper that logs failures and
    returns `None` instead of raising -- the probe is best-effort,
    failures degrade to "assume the gap is still there"."""
    try:
        return subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError) as exc:
        log.warning("xapi-compat probe: %s failed: %s", " ".join(cmd), exc)
        return None


def probe_xapi_version(log, xapi_binary=XAPI_BINARY):
    """Return the xapi version string, or `None` if probing fails.

    `xapi --version` is canonical on XCP-ng 8.3. Output shape:

        xapi version 25.6.0 (...)
    """
    out = _run([xapi_binary, "--version"], log)
    if out is None:
        return None
    text = out.decode("utf-8", errors="replace").strip()
    # First line, last whitespace-separated token containing a digit.
    # Resilient to small upstream formatting changes.
    first = text.splitlines()[0] if text else ""
    match = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", first)
    return match.group(1) if match else first or None


def probe_recognised_features(log, xapi_binary=XAPI_BINARY):
    """Return the set of `VDI_*` feature names xapi's binary contains
    as standalone string literals, or an empty set if probing fails.

    Equivalent to the diagnostic in #89 / #115:
        strings /opt/xensource/bin/xapi | grep -E '^VDI_[A-Z_]+(/[0-9])?$'

    `strings(1)` separates extracted runs by newlines, after which
    `grep -E '^...$'` keeps only entries that are *exactly* a
    feature name. Matters because xapi embeds error strings like
    `VDI_COPY_FAILED` and event names alongside the recognised
    feature flags -- a substring match would conflate them.
    """
    runs = _extract_printable_runs(xapi_binary, log)
    if runs is None:
        return set()
    return {r for r in runs if _RE_FEATURE_NAME.match(r)}


def _extract_printable_runs(xapi_binary, log):
    """Return a list of strings extracted from `xapi_binary` (one per
    run of >=4 printable ASCII bytes), or `None` if the binary can
    not be read at all.

    Prefers `/usr/bin/strings`; falls back to a Python regex scan if
    `strings` is missing (unusual on dom0 but cheap to support).
    """
    out = _run(["strings", xapi_binary], log)
    if out is not None:
        return [line.decode("ascii", errors="replace") for line in out.splitlines()]
    try:
        with open(xapi_binary, "rb") as fh:
            blob = fh.read()
    except (OSError, IOError) as exc:
        log.warning("xapi-compat probe: cannot read %s: %s", xapi_binary, exc)
        return None
    return [m.group(0).decode("ascii") for m in _RE_PRINTABLE_RUN.finditer(blob)]


def probe(log=None, xapi_binary=XAPI_BINARY):
    """Probe xapi once and return a `xapi_compat` dict:

        {
            "xapi_version": "25.6.0" | None,
            "recognised_features": {"VDI_ATTACH", ...},
            "gaps": {                # method-name -> bool (True = shim fills it)
                "VDI.copy": True,
                "VDI.list_changed_blocks": True,
                "VDI.similar_content": True,
            },
            "active_shims": ["VDI.copy", ...],   # methods the shim must provide
        }

    Consumers should call this once and cache the result. The probe
    is best-effort: any sub-step that fails degrades to "gap is
    present" so the shim keeps providing the workaround.
    """
    if log is None:
        log = logging.getLogger("shim.xapi_compat")

    version = probe_xapi_version(log, xapi_binary=xapi_binary)
    features = probe_recognised_features(log, xapi_binary=xapi_binary)

    gaps = {}
    active = []
    for method, feature_flag in KNOWN_GAPS:
        if feature_flag is None:
            # Dispatcher-side gap -- not visible in xapi's feature
            # strings. Assume it's still present (the shim always
            # provides it on the relevant code path).
            present = True
        else:
            present = feature_flag not in features
        gaps[method] = present
        if present:
            active.append(method)

    return {
        "xapi_version": version,
        "recognised_features": features,
        "gaps": gaps,
        "active_shims": active,
    }


def log_summary(compat, log):
    """Emit a single human-readable syslog line describing the probe
    result. Operators grep this to confirm the shim's view of xapi
    matches their expectations."""
    version = compat.get("xapi_version") or "unknown"
    active = compat.get("active_shims") or []
    if active:
        log.info(
            "xapi-compat: xapi=%s; active dispatch gaps: %s",
            version,
            ", ".join(sorted(active)),
        )
    else:
        log.info("xapi-compat: xapi=%s; no dispatch gaps detected", version)
