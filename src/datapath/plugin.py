#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Plugin.Query implementation for raw-qdisk datapath.

This datapath plugin enables raw block devices (like ZFS zvols) to be
connected to VMs via qemu-dp instead of tapdisk.
"""

import os
import sys

# Add xapi storage libs to path
sys.path.insert(0, "/usr/libexec/xapi-storage-script/")

import xapi.storage.api.v5.plugin
from xapi.storage import log


class Implementation(xapi.storage.api.v5.plugin.Plugin_skeleton):
    """Plugin implementation for raw-qdisk datapath."""

    def query(self, dbg):
        """Return plugin metadata and capabilities."""
        return {
            "plugin": "raw-qdisk",
            "name": "Raw block device datapath using qemu-dp",
            "description": (
                "This plugin manages qemu-dp instances for raw block devices "
                "(such as ZFS zvols), enabling live storage migration and CBT."
            ),
            "vendor": "Moksha",
            "copyright": "(C) 2026 Moksha",
            "version": "1.0",
            "required_api_version": "5.0",
            "features": ["VDI_MIRROR_IN"],
            "configuration": {},
            "required_cluster_stack": [],
        }


if __name__ == "__main__":
    log.log_call_argv()
    cmd = xapi.storage.api.v5.plugin.Plugin_commandline(Implementation())
    base = os.path.basename(sys.argv[0])
    if base == "Plugin.Query":
        cmd.query()
    else:
        raise xapi.storage.api.v5.plugin.Unimplemented(base)
