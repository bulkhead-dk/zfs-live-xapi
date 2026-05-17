#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Volume.read_sr_metadata -- installable helper invoked over SSH.

xe_wrapper's cross-host fast path needs to read the destination
SR's metadata from the remote host. We can't import `sr_metadata` over
a one-shot SSH invocation (Python module path setup), so we ship this
tiny script alongside the rest of the volume plugin and invoke it as:

    ssh root@host /usr/libexec/xapi-storage-script/volume/<plugin>/Volume.read_sr_metadata <sr-mount>

It prints the metadata JSON to stdout. Exit 0 on success, 1 on
missing file / unparseable JSON / wrong arg count. Stderr carries the
diagnostic.

Keeping this as a separate script (rather than inlining `cat
<sr-mount>/meta.json` over SSH) means the basename only appears once
in the repo -- `src/shim/sr_metadata.py:META_BASENAME` -- and a future
libcow rename touches one file instead of N.
"""

import os
import sys

# Resolve sibling import without requiring a package install.
# Use realpath so we follow symlinks back to the real on-disk file --
# the installer registers this script as `Volume.read_sr_metadata`
# (a symlink to read_sr_metadata.py) in the volume-plugin dir;
# `os.path.abspath` would resolve to the symlink's directory if
# the script is invoked through a different symlink, missing the
# sibling sr_metadata.py. (#217 finding 7)
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import sr_metadata  # noqa: E402


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: {} <sr-mount-path>\n".format(argv[0]))
        return 1
    path = sr_metadata.sr_metadata_path(argv[1])
    try:
        with open(path, encoding="utf-8") as fh:
            sys.stdout.write(fh.read())
    except (IOError, OSError) as e:
        sys.stderr.write("Volume.read_sr_metadata: {}: {}\n".format(path, e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
