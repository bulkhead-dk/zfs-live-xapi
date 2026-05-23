#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Volume plugin entrypoint -- Plugin.Query / Plugin.diagnostics.

Response-building logic lives in plugin_query (#231) so it
stays testable without xapi-stubbing the whole skeleton.
"""

import os
import sys
import xapi.storage.api.v5.plugin
from xapi.storage import log

try:
    from . import plugin_query
except (ImportError, ValueError):
    import plugin_query


class Implementation(xapi.storage.api.v5.plugin.Plugin_skeleton):

    def diagnostics(self, dbg):
        return "No diagnostic data to report"

    def query(self, dbg):
        return plugin_query.build_query_response(dbg)


if __name__ == "__main__":
    log.log_call_argv()
    cmd = xapi.storage.api.v5.plugin.Plugin_commandline(Implementation())
    base = os.path.basename(sys.argv[0])
    if base == "Plugin.diagnostics":
        cmd.diagnostics()
    elif base == "Plugin.Query":
        cmd.query()
    else:
        raise xapi.storage.api.v5.plugin.Unimplemented(base)
