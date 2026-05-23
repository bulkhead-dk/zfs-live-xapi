#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Consumer-side surface for the persisted CBT bitmap state (#108
umbrella; this module ships the read half from #111).

The producer half (PRs #99 / #101 / #103 / #105 / #107) writes
`<sr-mount>/.zfs-live/cbt/<vdi-uuid>.pickle` files whose payload
includes an `nbd_export` descriptor pointing at a qemu NBD server
that exposes the bitmap as a `qemu:dirty-bitmap:<name>`
meta-context. This module reads through that descriptor.

Today's surface (read-only):

    extract_dirty_extents(dbg, payload) -> list[(offset, length)]
    extract_dirty_extents_for(dbg, sr_uri, vdi_key) ->
        dict[bitmap_name, list[(offset, length)]]

The write half -- re-marking the dirty extents on a fresh bitmap
through the merge-bitmap dance -- is the remaining work under
#108. With the read side here, an operator-side diagnostic tool
already has a useful surface ("what changed since the last
snapshot") even before the lifecycle write-back is wired.
"""

from __future__ import absolute_import

try:
    from xapi.storage import log
except ImportError:
    # Fallback for testing outside XCP-ng -- same pattern as
    # qemudisk_raw.py uses.
    import logging

    log = logging.getLogger(__name__)

import qemudisk_raw
import nbd_client


def extract_dirty_extents(dbg, payload):
    """Return the dirty extents recorded in a single-bitmap
    persisted payload as `[(offset, length), ...]`.

    `payload` is one of the dict values from a multi-bitmap
    save (i.e. the inner dict produced by `export_bitmap`):

        {
          "bitmap_name": str,
          "granularity": int,
          "dirty_count":  int,
          "nbd_export": {"socket": str,
                         "export_name": str,
                         "bitmap_context": str},
        }

    Returns an empty list if the payload is missing the
    `nbd_export` descriptor (e.g. a legacy payload from before
    `export_bitmap` always populated it). Raises if the connection
    succeeds but the wire shape is wrong -- caller decides what to
    do (the lifecycle integration in #108 will swallow it the
    same way the rest of the CBT path does).
    """
    nbd = payload.get("nbd_export") if isinstance(payload, dict) else None
    if not nbd:
        return []
    return nbd_client.fetch_dirty_extents(
        dbg,
        socket_path=nbd["socket"],
        export_name=nbd["export_name"],
        bitmap_context=nbd["bitmap_context"],
    )


def extract_dirty_extents_for(dbg, sr_uri, vdi_key):
    """Convenience: load the persisted multi-bitmap payload for a
    VDI and return a dict mapping each bitmap's name to its
    dirty-extent list. Empty dict if no state is persisted.

    Per-bitmap isolation: if one bitmap's NBD fetch raises (dead
    socket, malformed descriptor, server gone away), we log + skip
    that bitmap and the rest still come back. Mirrors the
    producer side's shape -- `_cbt_persist_on_deactivate` from PR
    #101 doesn't lose the whole save when one bitmap's
    `export_bitmap` fails. The diagnostic surface and #109's
    list-changed-blocks plumbing both need partial results to be
    useful when one bitmap is broken.

    A bitmap that fails to fetch is **omitted from the dict**
    rather than mapped to an empty list. Empty list means
    "fetched cleanly, nothing dirty"; omission means "couldn't
    fetch, look elsewhere". Different conditions for the
    consumer; don't conflate.

    This is what an operator-side diagnostic tool ("show me what
    changed for VDI X") wants to call. It's also the shape the
    `xe vdi-list-changed-blocks` plumbing (#109) reads against
    for the per-VDI case.
    """
    multi = qemudisk_raw.load_cbt_metadata(dbg, sr_uri, vdi_key)
    if not isinstance(multi, dict):
        return {}

    result = {}
    for name, single in multi.items():
        try:
            result[name] = extract_dirty_extents(dbg, single)
        except Exception as e:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.warning(
                "{}: cbt_consumer: extract_dirty_extents for "
                "bitmap '{}' (vdi={}) failed: {} -- omitting "
                "from result (other bitmaps still reported)".format(dbg, name, vdi_key, e)
            )
    return result
