#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Single source of truth for the SR-metadata file's on-disk location.

`xapi.storage.libs.util.update_sr_metadata` (libcow, upstream
xapi-storage) writes the SR-level metadata file as `meta.json` under
the SR mount, but libcow exposes no public accessor for the path.
Out-of-tree consumers (the xe-wrapper, the cross-host SSH path, future
tooling) used to reach past that absent API by hardcoding the basename.
That bit us once already in PR #93's lab bring-up -- the wrapper had
guessed the wrong filename and every cross-host call silently fell
through to sparse_dd until someone noticed.

This module is the project-local mitigation: we centralise the basename
in **one** place so a future libcow API rename only touches this file,
and out-of-tree consumers go through `sr_metadata_path()` /
`read_sr_metadata()` rather than reaching past us.

The proper long-term fix is upstream -- `xapi.storage.libs.util`
publishing a `sr_metadata_path(sr_uri)` accessor -- at which point this
module collapses to a thin shim or disappears. See #94 for that
trajectory.
"""

import json
import os

# The single literal in our tree. Every other site reads it through
# this module -- `git grep '"meta.json"'` should hit only this file
# and the contract test that anchors it.
META_BASENAME = "meta.json"


def sr_metadata_path(sr_mount):
    """Return the absolute path of the SR-metadata file for `sr_mount`.

    `sr_mount` is the on-disk mount point (e.g.
    `/var/run/sr-mount/<sr-uuid>`). The libcow-canonical form
    `'file://' + sr_mount` is also accepted for symmetry with
    `util.get_sr_metadata(dbg, sr_uri)` -- we strip the scheme.
    """
    if sr_mount.startswith("file://"):
        sr_mount = sr_mount[len("file://") :]
    return os.path.join(sr_mount, META_BASENAME)


def read_sr_metadata(sr_mount):
    """Read and parse the SR-metadata file. Returns the dict on
    success or `None` on missing-file / unparseable JSON. Does not
    raise -- the wrapper's policy on metadata-read failure is to
    passthrough to the upstream code path, never to crash."""
    path = sr_metadata_path(sr_mount)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (IOError, OSError, ValueError):
        return None
