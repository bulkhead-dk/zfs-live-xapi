#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint: disable=too-many-lines
"""
qemu-dp management for raw block devices.

This module provides functions to spawn, control, and manage qemu-dp
processes for raw block devices (like ZFS zvols).

Based on xapi/storage/libs/qemudisk.py but adapted for raw format.
"""

import errno
import os
import pickle
import signal
import subprocess
import time

try:
    from xapi.storage import log
except ImportError:
    # Fallback for testing outside XCP-ng
    import logging

    log = logging.getLogger(__name__)
    log.log_call_argv = lambda: None

# qemu-storage-daemon binary for NBD export (standalone storage daemon)
QEMU_STORAGE_DAEMON = "/usr/lib64/qemu-dp/bin/qemu-storage-daemon"

# cgroups for resource management (matching blktap behavior)
QEMU_DP_CGROUP_BLKIO = "blkio:/vm.slice/"
QEMU_DP_CGROUP_CPU = "cpu,cpuacct:/"

# Metadata file names. Two distinct concerns sharing the same module:
#
#   - `METADATA_FILE` -- qemu-dp runtime state (pid, qmp socket path,
#     NBD socket path). Lifetime is the qemu-dp process. Lost on
#     reboot is correct: the process itself is gone, so its socket
#     paths and pids would be stale anyway.
#
#   - `CBT_METADATA_FILE` -- Changed Block Tracking bitmap state. This
#     is durable data the operator's backup workflow depends on for
#     incremental backups, and *must* survive host reboot, SR
#     reattach, and qemu-dp restarts. Lives under the SR mount in
#     a hidden driver-owned directory; see `_cbt_metadata_path()`.
METADATA_FILE = "qemu-dp-raw.pickle"
CBT_METADATA_FILE = "cbt-metadata.pickle"

# Hidden directory under each SR's mount where the driver persists
# its own metadata (currently CBT only). The leading dot keeps it out
# of casual directory listings; SR.ls iterates ZFS datasets rather
# than mount-point files, so no conflict with VDI enumeration.
DRIVER_METADATA_DIRNAME = ".zfs-live"
CBT_METADATA_SUBDIR = "cbt"

# Block device node name in qemu
BLOCK_NODE_NAME = "raw_block_node"


class QMPError(Exception):
    """Error communicating with qemu-dp via QMP."""


class QMPConnection:
    """Simple QMP (QEMU Machine Protocol) client."""

    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.sock = None

    def connect(self, timeout=5.0):
        """Connect to QMP socket."""
        import socket  # pylint: disable=import-outside-toplevel

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)

        # Retry connection (qemu-dp may take time to start)
        for _ in range(50):
            try:
                self.sock.connect(self.socket_path)
                break
            except socket.error:
                time.sleep(0.1)
        else:
            raise QMPError("Failed to connect to QMP socket: {}".format(self.socket_path))

        # Read greeting
        greeting = self._recv()
        if "QMP" not in greeting:
            raise QMPError("Invalid QMP greeting: {}".format(greeting))

        # Send qmp_capabilities to enter command mode
        self._send({"execute": "qmp_capabilities"})
        resp = self._recv()
        if "return" not in resp:
            raise QMPError("qmp_capabilities failed: {}".format(resp))

    def close(self):
        """Close QMP connection."""
        if self.sock:
            try:
                self.sock.close()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            self.sock = None

    def command(self, cmd, **kwargs):
        """Execute a QMP command."""

        msg = {"execute": cmd}
        if kwargs:
            msg["arguments"] = kwargs

        self._send(msg)
        resp = self._recv()

        if "error" in resp:
            raise QMPError("QMP command '{}' failed: {}".format(cmd, resp["error"]))

        return resp.get("return", {})

    def _send(self, msg):
        """Send a JSON message."""
        import json  # pylint: disable=import-outside-toplevel

        data = json.dumps(msg).encode("utf-8")
        self.sock.sendall(data)

    def _recv(self):
        """Receive a JSON message."""
        import json  # pylint: disable=import-outside-toplevel

        data = b""
        while True:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data += chunk
            # Try to parse - QMP sends one JSON object per message
            try:
                return json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                continue
        raise QMPError("Connection closed unexpectedly")


def _detect_block_node(dbg, qmp, device_path=None):
    """Identify the block node name from a running qemu-dp instance.

    When a VM has multiple block devices (VDI, CD-ROM, etc.), blindly
    picking the first ``query-block`` entry is wrong.  This function
    uses a ranked strategy:

      1. Match by ``device_path`` against each inserted drive's
         ``file`` property (exact path match).
      2. If no path supplied or no match, pick the drive that already
         has dirty bitmaps (the CBT-enabled VDI).
      3. Last resort: error -- caller must supply ``block_node_name``.

    Args:
        dbg: Debug context
        qmp: Connected QMPConnection instance
        device_path: Optional block device path to match

    Returns:
        Block node name string

    Raises:
        QMPError: If the node cannot be determined
    """
    blocks = qmp.command("query-block")

    # Strategy 1: match by device path
    if device_path:
        real_path = os.path.realpath(device_path)
        for block in blocks:
            inserted = block.get("inserted", {})
            node = inserted.get("node-name")
            block_file = inserted.get("file")
            if not node:
                continue
            if block_file and (
                block_file == device_path
                or block_file == real_path
                or os.path.realpath(block_file) == real_path
            ):
                log.debug(
                    "{}: matched block node '{}' by device "
                    "path '{}'".format(dbg, node, device_path)
                )
                return node

    # Strategy 2: pick the drive with dirty bitmaps
    for block in blocks:
        inserted = block.get("inserted", {})
        node = inserted.get("node-name")
        if node and inserted.get("dirty-bitmaps"):
            log.debug("{}: matched block node '{}' by dirty " "bitmaps presence".format(dbg, node))
            return node

    raise QMPError(
        "Cannot determine block node name from query-block; "
        "supply device_path or block_node_name explicitly"
    )


class QemuDiskRaw:
    """Manages a qemu-storage-daemon instance for a raw block device."""

    def __init__(
        self,
        pid,
        qmp_sock,
        key,
        device_path=None,
        nbd_unix_sock=None,
        nbd_server_running=False,
        block_node_name=None,
        scm_proxy_pid=None,
        scm_sock_path=None,
    ):
        self.pid = pid
        self.qmp_sock = qmp_sock
        self.key = key
        self.device_path = device_path
        self.nbd_unix_sock = nbd_unix_sock or os.path.join(
            os.path.dirname(qmp_sock), "qemu-nbd.{}".format(key)
        )
        self._nbd_server_running = nbd_server_running
        self.block_node_name = block_node_name or BLOCK_NODE_NAME
        # SCM_RIGHTS proxy process for inbound SXM (import_activate)
        self.scm_proxy_pid = scm_proxy_pid
        self.scm_sock_path = scm_sock_path

    def __repr__(self):
        return "QemuDiskRaw(pid={}, qmp_sock={}, key={}, device={})".format(
            self.pid, self.qmp_sock, self.key, self.device_path
        )

    @classmethod
    def connect_existing(
        cls, dbg, qmp_sock, device_path=None, nbd_unix_sock=None, block_node_name=None
    ):
        """Connect to an existing qemu-dp instance by QMP socket path.

        Used to manage CBT bitmaps on qemu-dp instances spawned by
        xenopsd (for VM I/O) rather than by our datapath plugin.

        xenopsd's qemu-dp does not start an NBD server by default.
        If ``nbd_unix_sock`` is supplied, cbt_export_bitmap() will
        start an NBD server on that path via ``nbd-server-start``
        before adding the export.  If omitted, bitmap export methods
        will raise an error.

        The returned instance must NOT call quit() -- it does not own
        the process.

        Args:
            dbg: Debug context
            qmp_sock: Path to the QMP Unix socket
            device_path: Block device path (e.g. ``/dev/zvol/...``)
                used to identify the correct block node when the
                qemu-dp instance has multiple drives (VDI + ISO).
            nbd_unix_sock: Optional path for the NBD Unix socket.
                When provided, cbt_export_bitmap() will start an
                NBD server here if one is not already running.
            block_node_name: Block device node name used by this
                qemu-dp instance.  If None, auto-detected from
                ``query-block`` by matching ``device_path`` against
                each inserted drive's ``file`` property.

        Returns:
            QemuDiskRaw instance connected to the external process
        """
        if not os.path.exists(qmp_sock):
            raise QMPError("QMP socket not found: {}".format(qmp_sock))

        log.debug("{}: connecting to existing qemu-dp at {}".format(dbg, qmp_sock))

        # Verify connectivity and auto-detect block node name
        qmp = QMPConnection(qmp_sock)
        try:
            qmp.connect(timeout=2.0)
            if block_node_name is None:
                block_node_name = _detect_block_node(dbg, qmp, device_path)
        finally:
            qmp.close()

        return cls(
            pid=None,
            qmp_sock=qmp_sock,
            key=None,
            device_path=device_path,
            nbd_unix_sock=nbd_unix_sock,
            block_node_name=block_node_name,
        )

    def open(self, dbg, device_path):
        """No-op for qemu-storage-daemon (device opened at daemon start).

        Args:
            dbg: Debug context
            device_path: Path to block device (e.g., /dev/zvol/pool/vol)
        """
        # qemu-storage-daemon opens the device at startup via command line
        # This method is kept for API compatibility
        log.debug("{}: device {} already opened by qemu-storage-daemon".format(dbg, device_path))

    def close(self, dbg):
        """No-op for qemu-storage-daemon (cleanup handled by quit).

        Args:
            dbg: Debug context
        """
        # qemu-storage-daemon handles cleanup when terminated
        # This method is kept for API compatibility
        log.debug("{}: close called (no-op for qemu-storage-daemon)".format(dbg))

    def qmp_command(self, dbg, cmd, **kwargs):
        """Execute a QMP command on this qemu-storage-daemon instance.

        Args:
            dbg: Debug context
            cmd: QMP command name
            **kwargs: Command arguments

        Returns:
            Command result dict
        """
        log.debug("{}: QMP command '{}' args={}".format(dbg, cmd, kwargs))
        qmp = QMPConnection(self.qmp_sock)
        try:
            qmp.connect(timeout=5.0)
            result = qmp.command(cmd, **kwargs)
            return result
        finally:
            qmp.close()

    def mirror_start(self, dbg, target_device_path, job_id):
        """Start block mirroring from this device to a local target device.

        Adds the target as a new blockdev and starts a blockdev-mirror job
        that copies all existing data and mirrors ongoing writes.

        Args:
            dbg: Debug context
            target_device_path: Path to destination block device
            job_id: Unique identifier for this mirror job

        Returns:
            Job ID string
        """
        target_node = "mirror_target_{}".format(job_id.replace("-", "_"))

        log.debug(
            "{}: mirror_start: source={} target={} job={}".format(
                dbg, self.device_path, target_device_path, job_id
            )
        )

        # Add target block device
        self.qmp_command(
            dbg,
            "blockdev-add",
            driver="host_device",
            filename=target_device_path,
            **{"aio": "native", "cache": {"direct": True}, "node-name": target_node}
        )

        # Start mirror job (sync=full copies everything, then keeps mirroring)
        self.qmp_command(
            dbg,
            "blockdev-mirror",
            **{
                "job-id": job_id,
                "device": self.block_node_name,
                "target": target_node,
                "sync": "full",
            }
        )

        log.debug("{}: mirror_start: mirror job '{}' started".format(dbg, job_id))
        return job_id

    def mirror_start_nbd(self, dbg, host, port, export_name, job_id):
        """Start block mirroring from this device to a REMOTE NBD target.

        Connects to an NBD server on the destination host and mirrors all
        blocks + ongoing writes to it. This is the cross-host SXM path
        (#286) -- same block-mirror semantics as `mirror_start`, but the
        target is an NBD endpoint instead of a local device.

        Args:
            dbg: Debug context
            host: Destination host IP/hostname
            port: Destination NBD server port (int or str)
            export_name: NBD export name on the destination
            job_id: Unique identifier for this mirror job

        Returns:
            Job ID string
        """
        target_node = "mirror_nbd_{}".format(job_id.replace("-", "_"))

        log.debug(
            "{}: mirror_start_nbd: source={} target=nbd://{}:{}/{} "
            "job={}".format(dbg, self.device_path, host, port, export_name, job_id)
        )

        # Add NBD target block device
        self.qmp_command(
            dbg,
            "blockdev-add",
            driver="nbd",
            server={"type": "inet", "host": str(host), "port": str(port)},
            export=export_name,
            **{"node-name": target_node}
        )

        # Start mirror job (sync=full copies everything, then keeps mirroring)
        self.qmp_command(
            dbg,
            "blockdev-mirror",
            **{
                "job-id": job_id,
                "device": self.block_node_name,
                "target": target_node,
                "sync": "full",
            }
        )

        log.debug(
            "{}: mirror_start_nbd: mirror job '{}' started to "
            "nbd://{}:{}/{}".format(dbg, job_id, host, port, export_name)
        )
        return job_id

    def mirror_stat(self, dbg, job_id):
        """Query the status of a mirror job.

        Args:
            dbg: Debug context
            job_id: Mirror job identifier

        Returns:
            Dict with mirror status:
                - status: 'active', 'ready', 'completed', 'error', 'not_found'
                - offset: bytes copied so far
                - len: total bytes to copy
                - progress: 0.0-1.0 progress ratio
                - ready: True when mirror is synchronized and cutover is safe
        """
        jobs = self.qmp_command(dbg, "query-block-jobs")

        for job in jobs:
            if job.get("id") == job_id:
                offset = job.get("offset", 0)
                length = job.get("len", 0)
                ready = job.get("ready", False)
                status = job.get("status", "unknown")

                progress = (offset / length) if length > 0 else 0.0

                result = {
                    "status": "ready" if ready else status,
                    "offset": offset,
                    "len": length,
                    "progress": progress,
                    "ready": ready,
                    "speed": job.get("speed", 0),
                }
                log.debug("{}: mirror_stat: job={} {}".format(dbg, job_id, result))
                return result

        log.debug("{}: mirror_stat: job '{}' not found".format(dbg, job_id))
        return {"status": "not_found", "ready": False, "progress": 0.0}

    def mirror_complete(self, dbg, job_id):
        """Complete a mirror job and switch to the target device.

        The mirror must be in 'ready' state (fully synchronized) before
        calling this. The source device is replaced by the target.

        Args:
            dbg: Debug context
            job_id: Mirror job identifier
        """
        log.debug("{}: mirror_complete: completing job '{}'".format(dbg, job_id))

        self.qmp_command(dbg, "block-job-complete", device=job_id)

        log.debug(
            "{}: mirror_complete: job '{}' completed, "
            "cutover to target device".format(dbg, job_id)
        )

    def mirror_cancel(self, dbg, job_id):
        """Cancel a mirror job and discard the target.

        Args:
            dbg: Debug context
            job_id: Mirror job identifier
        """
        log.debug("{}: mirror_cancel: cancelling job '{}'".format(dbg, job_id))

        try:
            self.qmp_command(dbg, "block-job-cancel", device=job_id)
        except QMPError as e:
            log.warning("{}: mirror_cancel: failed to cancel job '{}': {}".format(dbg, job_id, e))

    # -- Changed Block Tracking (CBT) via dirty bitmaps --
    #
    # QEMU dirty bitmaps track which blocks have been written to since the
    # bitmap was created or last cleared.  On raw block devices (like ZFS
    # zvols), bitmaps are in-memory only -- they do not survive qemu-dp
    # restarts.  Bitmap data must be exported and persisted externally
    # (via save_cbt_metadata) before the process exits.
    #
    # Typical CBT flow:
    #   1. enable_cbt  -> cbt_bitmap_add("cbt-active", granularity)
    #   2. VM runs     -> bitmap tracks writes automatically
    #   3. snapshot    -> cbt_snapshot_bitmap("cbt-active", "cbt-snap-<uuid>")
    #                    export frozen bitmap, save via save_cbt_metadata
    #   4. VM continues -> new "cbt-active" bitmap tracks further changes
    #   5. list_changed_blocks -> load saved bitmap data, return base64

    def cbt_bitmap_add(self, dbg, bitmap_name, granularity=65536):
        """Create a dirty bitmap for CBT tracking.

        Args:
            dbg: Debug context
            bitmap_name: Unique name for this bitmap
            granularity: Tracking granularity in bytes (default 64K)
        """
        log.debug(
            "{}: creating CBT bitmap '{}' granularity={}".format(dbg, bitmap_name, granularity)
        )
        self.qmp_command(
            dbg,
            "block-dirty-bitmap-add",
            node=self.block_node_name,
            name=bitmap_name,
            granularity=granularity,
        )

    def cbt_bitmap_remove(self, dbg, bitmap_name):
        """Remove a dirty bitmap.

        Args:
            dbg: Debug context
            bitmap_name: Name of bitmap to remove
        """
        log.debug("{}: removing CBT bitmap '{}'".format(dbg, bitmap_name))
        self.qmp_command(
            dbg,
            "block-dirty-bitmap-remove",
            node=self.block_node_name,
            name=bitmap_name,
        )

    def cbt_bitmap_clear(self, dbg, bitmap_name):
        """Clear all bits in a dirty bitmap (reset tracking).

        Marks all blocks as clean. Used after a successful full backup
        to start tracking changes from this point forward.

        Args:
            dbg: Debug context
            bitmap_name: Name of bitmap to clear
        """
        log.debug("{}: clearing CBT bitmap '{}'".format(dbg, bitmap_name))
        self.qmp_command(
            dbg, "block-dirty-bitmap-clear", node=self.block_node_name, name=bitmap_name
        )

    def cbt_bitmap_enable(self, dbg, bitmap_name):
        """Enable a dirty bitmap (resume tracking changes).

        Args:
            dbg: Debug context
            bitmap_name: Name of bitmap to enable
        """
        log.debug("{}: enabling CBT bitmap '{}'".format(dbg, bitmap_name))
        self.qmp_command(
            dbg,
            "block-dirty-bitmap-enable",
            node=self.block_node_name,
            name=bitmap_name,
        )

    def cbt_bitmap_disable(self, dbg, bitmap_name):
        """Disable a dirty bitmap (freeze, stop tracking).

        A disabled bitmap retains its current state but stops recording
        new writes. Used to freeze a bitmap before export/snapshot.

        Args:
            dbg: Debug context
            bitmap_name: Name of bitmap to disable
        """
        log.debug("{}: disabling CBT bitmap '{}'".format(dbg, bitmap_name))
        self.qmp_command(
            dbg,
            "block-dirty-bitmap-disable",
            node=self.block_node_name,
            name=bitmap_name,
        )

    def cbt_bitmap_merge(self, dbg, target_name, source_names):
        """Merge source bitmaps into a target bitmap.

        Computes the union of all source bitmaps and the target: any
        block marked dirty in any source becomes dirty in the target.
        Used to combine change tracking across snapshot intervals.

        Args:
            dbg: Debug context
            target_name: Bitmap to merge into
            source_names: List of bitmap names to merge from
        """
        log.debug("{}: merging bitmaps {} into '{}'".format(dbg, source_names, target_name))
        sources = [{"node": self.block_node_name, "name": s} for s in source_names]
        self.qmp_command(
            dbg,
            "block-dirty-bitmap-merge",
            node=self.block_node_name,
            target=target_name,
            bitmaps=sources,
        )

    def cbt_list_bitmaps(self, dbg):
        """Query all dirty bitmaps on this qemu instance's block node.

        Walks `query-named-block-nodes` filtered to the instance's
        `block_node_name`. The earlier implementation used the
        legacy `query-block` (which enumerates `--drive` devices,
        not `--blockdev` nodes) -- that didn't see any bitmaps on
        a qemu-storage-daemon spawned by `create()` because we use
        `--blockdev`. Surfaced by #126 (yesterday's lab run of
        e2e-zfs-cbt-recovery.sh's qemu-dp-restart subtest:
        `cbt_bitmap_add` returned success and an immediate
        `cbt_list_bitmaps` saw `[]`). Knock-on: the lifecycle
        hooks from PRs #101 / #103 silently no-op'd against
        driver-spawned qemu-dp.

        Returns the same per-bitmap shape as before:
            - name: bitmap name
            - recording: True if actively tracking
            - count: number of dirty bytes
            - granularity: tracking granularity in bytes
            - status: 'active', 'frozen', 'disabled'

        For xenopsd-spawned `qemu-dm` (which uses the legacy
        `--drive` model), `query-named-block-nodes` still works
        -- every drive carries an underlying block node that
        shows up there. So this single-query path covers both
        the driver-spawned and discovered-xenopsd cases.
        """
        nodes = self.qmp_command(dbg, "query-named-block-nodes")
        bitmaps = []
        for node in nodes:
            if node.get("node-name") != self.block_node_name:
                continue
            bitmaps.extend(node.get("dirty-bitmaps", []))
        log.debug(
            "{}: found {} CBT bitmaps on node {}".format(dbg, len(bitmaps), self.block_node_name)
        )
        return bitmaps

    def cbt_snapshot_bitmap(self, dbg, current_name, new_name, granularity=None):
        """Snapshot a CBT bitmap: freeze the current and start a new one.

        Used when a VDI snapshot is taken:
          1. Disable (freeze) the current bitmap -- it now represents all
             changes since the previous snapshot.
          2. Create a new bitmap with the same granularity to track
             future changes.

        The caller should export and persist the frozen bitmap data
        before the qemu-dp process exits (raw device bitmaps are
        in-memory only).

        Args:
            dbg: Debug context
            current_name: Active bitmap to freeze
            new_name: Name for the new active bitmap
            granularity: Granularity for the new bitmap (bytes).
                If None, inherits from the current bitmap via
                query-block so the two bitmaps stay merge-compatible.
        """
        log.debug("{}: snapshotting bitmap '{}' -> new '{}'".format(dbg, current_name, new_name))

        # Auto-detect granularity from the current bitmap so the new
        # bitmap stays merge-compatible (QEMU requires matching
        # granularity for block-dirty-bitmap-merge).
        if granularity is None:
            for bm in self.cbt_list_bitmaps(dbg):
                if bm.get("name") == current_name:
                    granularity = bm.get("granularity", 65536)
                    break
            else:
                granularity = 65536
                log.warning(
                    "{}: bitmap '{}' not found, defaulting "
                    "granularity to {}".format(dbg, current_name, granularity)
                )

        self.cbt_bitmap_disable(dbg, current_name)
        self.cbt_bitmap_add(dbg, new_name, granularity)

        log.debug(
            "{}: bitmap snapshot complete, '{}' frozen, "
            "'{}' active (granularity={})".format(dbg, current_name, new_name, granularity)
        )

    def _ensure_nbd_server(self, dbg):
        """Start an NBD server if one is not already running.

        Our own qemu-storage-daemon instances start with ``--nbd-server``
        so a server is always present.  xenopsd-spawned qemu-dp instances
        do NOT start an NBD server, so we must create one via QMP before
        adding exports.

        Raises:
            QMPError: If no nbd_unix_sock is configured (connect_existing
                      was called without specifying a socket path).
        """
        if not self.nbd_unix_sock:
            raise QMPError(
                "No NBD socket path configured. When using "
                "connect_existing(), supply nbd_unix_sock so the "
                "CBT export has a socket to bind to."
            )

        if self._nbd_server_running:
            return

        log.debug("{}: starting NBD server on {}".format(dbg, self.nbd_unix_sock))
        try:
            self.qmp_command(
                dbg,
                "nbd-server-start",
                addr={"type": "unix", "data": {"path": self.nbd_unix_sock}},
            )
        except QMPError as e:
            if "already running" in str(e).lower():
                log.debug("{}: NBD server already running".format(dbg))
            else:
                raise
        self._nbd_server_running = True

    def cbt_export_bitmap(self, dbg, bitmap_name, export_name=None):
        """Create a temporary NBD export with bitmap metadata context.

        Adds a read-only NBD export that includes the dirty bitmap,
        allowing NBD clients to query changed blocks via
        NBD_CMD_BLOCK_STATUS with the ``qemu:dirty-bitmap:<name>``
        metadata context.

        For xenopsd-owned qemu-dp instances (via connect_existing),
        an NBD server is started automatically on the socket path
        supplied at connection time.

        The caller should remove the export with cbt_remove_export()
        after reading the bitmap data.

        Args:
            dbg: Debug context
            bitmap_name: Dirty bitmap to attach to the export
            export_name: NBD export name (default: ``cbt_<bitmap_name>``)

        Returns:
            Dict with NBD connection info:
                - nbd_unix_sock: path to NBD Unix socket
                - export_name: NBD export name
                - bitmap_context: metadata context string for BLOCK_STATUS
        """
        if export_name is None:
            export_name = "cbt_{}".format(bitmap_name)

        self._ensure_nbd_server(dbg)

        log.debug(
            "{}: creating NBD export '{}' with bitmap '{}'".format(dbg, export_name, bitmap_name)
        )

        self.qmp_command(
            dbg,
            "nbd-server-add",
            device=self.block_node_name,
            name=export_name,
            writable=False,
            bitmap=bitmap_name,
        )

        result = {
            "nbd_unix_sock": self.nbd_unix_sock,
            "export_name": export_name,
            "bitmap_context": "qemu:dirty-bitmap:{}".format(bitmap_name),
        }
        log.debug("{}: CBT export ready: {}".format(dbg, result))
        return result

    # -- Lifecycle export / prepare-from-payload (#96) -------------
    #
    # The CBT-metadata persistence helpers (`save_cbt_metadata` /
    # `load_cbt_metadata` / `remove_cbt_metadata` from #30) need a
    # well-defined dict to pickle. The two primitives below produce
    # and consume that wire format -- but they are intentionally
    # **asymmetric** in what they restore:
    #
    #   payload = {
    #     "bitmap_name":  str,
    #     "granularity":  int,             # bytes
    #     "dirty_count":  int,             # bytes marked dirty at capture
    #     "nbd_export": {                  # absent if not NBD-exposed
    #         "socket":         str,
    #         "export_name":    str,
    #         "bitmap_context": str,
    #     },
    #   }
    #
    # `export_bitmap` *captures* state: it freezes the live bitmap
    # (so writes after capture don't move under the consumer's
    # feet), reads metadata, opens an NBD export so a consumer can
    # fetch the dirty extents via NBD_CMD_BLOCK_STATUS with the
    # `qemu:dirty-bitmap:<name>` meta context, and returns the
    # descriptor for persistence.
    #
    # `prepare_bitmap_from_payload` *only prepares the container*:
    # it reads `granularity` out of the payload and creates a fresh
    # empty bitmap with that shape. **It does NOT restore the saved
    # dirty state.** Restoring extents requires NBD writes against a
    # writable merge-bitmap (QEMU has no QMP command to set
    # arbitrary bits in a bitmap), and that's the incremental-backup
    # consumer's responsibility -- tracked as the next #96 step.
    #
    # The deliberately-narrow name on the import side is what tells
    # the next lifecycle-hook implementer the saved dirty state is
    # NOT yet back. An earlier draft called the second method
    # `import_bitmap`; the symmetric naming over-promised relative
    # to the actual behaviour, so the API was renamed before
    # landing.

    def export_bitmap(self, dbg, bitmap_name):
        """Freeze and export a CBT bitmap for persistence.

        Steps:
          1. Disable the bitmap so its dirty bits stop changing
             under the consumer (`block-dirty-bitmap-disable`).
          2. Read its metadata (granularity, dirty count) via
             `query-block` so we know how to recreate it later.
          3. Open an NBD export with a `qemu:dirty-bitmap:<name>`
             metadata context so a consumer can fetch the dirty
             extents.
          4. Return a dict matching the persisted payload shape
             documented above.

        Returns the descriptor; the caller pickles it via
        `save_cbt_metadata(dbg, sr_uri, key, payload)`.

        Raises:
            QMPError: bitmap not found, or NBD socket not configured.
        """
        log.debug("{}: export_bitmap '{}'".format(dbg, bitmap_name))

        # 1. Freeze the bitmap. cbt_bitmap_disable is idempotent at
        #    the QMP layer (re-disabling an already-disabled bitmap
        #    succeeds), so a re-export is safe.
        self.cbt_bitmap_disable(dbg, bitmap_name)

        # 2. Read metadata. cbt_list_bitmaps returns the per-bitmap
        #    dicts query-block exposes.
        info = None
        for bm in self.cbt_list_bitmaps(dbg):
            if bm.get("name") == bitmap_name:
                info = bm
                break
        if info is None:
            raise QMPError(
                "export_bitmap: bitmap '{}' not found on node {}".format(
                    bitmap_name, self.block_node_name
                )
            )

        # 3. NBD export so the consumer can fetch dirty extents.
        nbd_export = self.cbt_export_bitmap(dbg, bitmap_name)

        payload = {
            "bitmap_name": bitmap_name,
            "granularity": info.get("granularity", 65536),
            "dirty_count": info.get("count", 0),
            "nbd_export": {
                "socket": nbd_export["nbd_unix_sock"],
                "export_name": nbd_export["export_name"],
                "bitmap_context": nbd_export["bitmap_context"],
            },
        }
        log.debug("{}: export_bitmap payload: {}".format(dbg, payload))
        return payload

    def prepare_bitmap_from_payload(self, dbg, bitmap_name, payload):
        """Create an *empty* tracking bitmap shaped to receive a
        previously-exported payload.

        **The container-only restore is deliberate, not a gap.**
        The live qemu bitmap doesn't need to be repopulated from
        the persisted state because consumers
        (`Volume.list_changed_blocks` from PR #114, future
        list-changed-blocks shim path #116) read directly from
        the persisted `<vdi-uuid>.pickle` file via
        `cbt_consumer.extract_dirty_extents_for`. The live qemu
        bitmap exists only to track the **next** cycle's writes
        -- and an empty bitmap right after activate matches reality
        in every lifecycle event we cover (deactivate->activate,
        qemu-dp crash, host reboot, SR detach/reattach, snapshot)
        because no writes happen in the windows where bitmap
        tracking is offline.

        See `docs/backup-integration.md` section "Why no live-bitmap
        write-back" and #117 for the full architectural
        analysis. An earlier draft (#108) proposed a
        merge-bitmap dance to repopulate the live bitmap; that
        proposal was retired because it solves a problem that
        doesn't exist in our consumers-read-from-persisted-file
        architecture.

        The narrow name `prepare_bitmap_from_payload` (vs the
        symmetric `import_bitmap` an earlier draft used) reflects
        what this method actually does: prepare a forward-tracking
        container at the correct granularity. Symmetric naming
        with `export_bitmap` would over-promise.

        If a bitmap with the requested name already exists with
        the right granularity, the call is a no-op so a second
        activate on the same qemu-dp doesn't error.

        Args:
            dbg: Debug context
            bitmap_name: Name to recreate the bitmap under
            payload: Dict previously returned by `export_bitmap`
                (typically rehydrated via `load_cbt_metadata`)
        """
        if not isinstance(payload, dict):
            raise QMPError(
                "prepare_bitmap_from_payload: payload must be a "
                "dict, got {}".format(type(payload).__name__)
            )

        granularity = payload.get("granularity", 65536)
        log.debug(
            "{}: prepare_bitmap_from_payload '{}' "
            "granularity={} (saved dirty_count={}; bitmap is "
            "created EMPTY -- extent replay is the consumer's "
            "job)".format(dbg, bitmap_name, granularity, payload.get("dirty_count", "?"))
        )

        # Idempotency: if the bitmap is already there with the
        # right granularity, leave it alone.
        for bm in self.cbt_list_bitmaps(dbg):
            if bm.get("name") == bitmap_name:
                if bm.get("granularity") == granularity:
                    log.debug(
                        "{}: prepare_bitmap_from_payload '{}' "
                        "already present with granularity={}, "
                        "no-op".format(dbg, bitmap_name, granularity)
                    )
                    return
                # Same name, different granularity is a misconfig --
                # tear it down so the recreate has clean ground.
                log.warning(
                    "{}: prepare_bitmap_from_payload '{}' present "
                    "with granularity={} (expected {}), removing "
                    "and recreating".format(dbg, bitmap_name, bm.get("granularity"), granularity)
                )
                self.cbt_bitmap_remove(dbg, bitmap_name)
                break

        self.cbt_bitmap_add(dbg, bitmap_name, granularity=granularity)

    def cbt_remove_export(self, dbg, export_name):
        """Remove a temporary CBT NBD export.

        Args:
            dbg: Debug context
            export_name: NBD export name to remove
        """
        log.debug("{}: removing NBD export '{}'".format(dbg, export_name))
        self.qmp_command(dbg, "nbd-server-remove", name=export_name)

    def stop_scm_proxy(self, dbg):
        """Terminate the SCM_RIGHTS proxy process if running.

        Args:
            dbg: Debug context
        """
        if self.scm_proxy_pid is None:
            return

        log.debug("{}: terminating SCM proxy pid={}".format(dbg, self.scm_proxy_pid))
        try:
            os.kill(self.scm_proxy_pid, signal.SIGTERM)
            for _ in range(20):
                try:
                    os.kill(self.scm_proxy_pid, 0)
                    time.sleep(0.1)
                except OSError:
                    break
            else:
                os.kill(self.scm_proxy_pid, signal.SIGKILL)
        except OSError:
            pass  # Already dead

        # Clean up SCM socket file
        if self.scm_sock_path:
            try:
                os.unlink(self.scm_sock_path)
            except OSError:
                pass

        self.scm_proxy_pid = None
        self.scm_sock_path = None

    def quit(self, dbg):
        """Terminate the qemu-dp process and any SCM proxy.

        Args:
            dbg: Debug context

        Raises:
            QMPError: If this instance was created via
                ``connect_existing()`` (pid is None -- the
                process is owned by xenopsd, not us).
        """
        if self.pid is None:
            # connect_existing() instances explicitly do not own the
            # qemu-dp process and must not call quit(). The class
            # docstring spells this out, but nothing here used to
            # enforce it: os.kill(None, ...) on the SIGKILL fallback
            # path raised TypeError (not OSError) and the bare
            # `except OSError` did not catch it. (#213 finding 3)
            raise QMPError(
                "quit() called on a connect_existing() instance -- "
                "this process is owned by xenopsd, not us; "
                "tearing it down here is not safe."
            )

        # Kill SCM proxy first (it connects to our NBD socket)
        self.stop_scm_proxy(dbg)

        log.debug("{}: terminating qemu-dp pid={}".format(dbg, self.pid))

        try:
            qmp = QMPConnection(self.qmp_sock)
            qmp.connect(timeout=2.0)
            qmp.command("quit")
            qmp.close()
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.warning("{}: QMP quit failed, killing process: {}".format(dbg, e))
            try:
                os.kill(self.pid, signal.SIGKILL)
            except OSError:
                pass

        # Wait for process to exit
        for _ in range(20):
            try:
                os.kill(self.pid, 0)
                time.sleep(0.1)
            except OSError:
                break

        log.debug("{}: qemu-dp terminated".format(dbg))


def create(dbg, key, device_path):
    """Spawn a new qemu-storage-daemon process for raw block device export.

    Args:
        dbg: Debug context
        key: Unique key for this instance (hash of device path)
        device_path: Path to the raw block device (e.g., /dev/zvol/pool/vol)

    Returns:
        QemuDiskRaw instance
    """
    socket_dir = _socket_dir()
    _mkdir_p(socket_dir)

    qmp_sock = os.path.join(socket_dir, "qmp_sock.{}".format(key))
    nbd_sock = os.path.join(socket_dir, "qemu-nbd.{}".format(key))
    pidfile = os.path.join(socket_dir, "pid.{}".format(key))

    log.debug(
        "{}: spawning qemu-storage-daemon for device {} with nbd socket at {}".format(
            dbg, device_path, nbd_sock
        )
    )

    # qemu-storage-daemon configures everything via command line
    cmd = [
        QEMU_STORAGE_DAEMON,
        "--daemonize",
        "--pidfile",
        pidfile,
        "--chardev",
        "socket,id=qmp,path={},server=on,wait=off".format(qmp_sock),
        "--monitor",
        "chardev=qmp",
        "--blockdev",
        "driver=host_device,filename={},aio=native,cache.direct=on,node-name={}".format(
            device_path, BLOCK_NODE_NAME
        ),
        "--nbd-server",
        "addr.type=unix,addr.path={}".format(nbd_sock),
        "--export",
        "type=nbd,id=exp1,node-name={0},writable=on,name={0}".format(BLOCK_NODE_NAME),
    ]

    log.debug("{}: command: {}".format(dbg, " ".join(cmd)))

    proc = subprocess.Popen(  # pylint: disable=consider-using-with
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True
    )

    # Wait for daemonize to complete
    stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError("qemu-storage-daemon failed to start: {} {}".format(stdout, stderr))

    # Read PID from pidfile
    for _ in range(50):
        try:
            with open(pidfile, "r") as f:  # pylint: disable=unspecified-encoding
                pid = int(f.read().strip())
            break
        except (IOError, ValueError):
            time.sleep(0.1)
    else:
        raise RuntimeError("qemu-storage-daemon pidfile not found")

    # Set cgroups for resource management
    try:
        _set_cgroup(pid, QEMU_DP_CGROUP_BLKIO)
        _set_cgroup(pid, QEMU_DP_CGROUP_CPU)
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.warning("{}: failed to set cgroups: {}".format(dbg, e))

    log.debug("{}: qemu-storage-daemon started with pid {}".format(dbg, pid))

    return QemuDiskRaw(pid, qmp_sock, key, device_path, nbd_sock, nbd_server_running=True)


def _socket_dir():
    """Get the directory for qemu-dp sockets."""
    return "/var/run/nonpersistent/qemu-dp-raw"


def _dp_metadata_root():
    """Root directory under which per-VDI metadata dirs live.

    Exposed so callers (e.g. datapath._gc_orphan_daemons) can iterate
    every key without having to know the path layout.
    """
    return "/var/run/nonpersistent/dp-raw-qdisk"


def _metadata_dir(key):
    """Get the metadata directory for a VDI."""
    return os.path.join(_dp_metadata_root(), key)


def _mkdir_p(path, mode=0o755):
    """Create directory and parents if needed."""
    try:
        os.makedirs(path, mode=mode)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


def _set_cgroup(pid, cgroup):
    """Set cgroup for a process."""
    cgroup_type, _ = cgroup.split(":", 1)
    cgroup_file = "/sys/fs/cgroup/{}/cgroup.procs".format(cgroup_type)
    if os.path.exists(cgroup_file):
        with open(cgroup_file, "w") as f:  # pylint: disable=unspecified-encoding
            f.write(str(pid))


def save_metadata(dbg, key, qemu_disk):
    """Save qemu-dp metadata for later operations.

    Args:
        dbg: Debug context
        key: VDI key
        qemu_disk: QemuDiskRaw instance
    """
    meta_dir = _metadata_dir(key)
    _mkdir_p(meta_dir)
    meta_file = os.path.join(meta_dir, METADATA_FILE)

    log.debug("{}: saving metadata to {}".format(dbg, meta_file))

    with open(meta_file, "wb") as f:
        pickle.dump(  # nosemgrep
            {
                "pid": qemu_disk.pid,
                "qmp_sock": qemu_disk.qmp_sock,
                "key": qemu_disk.key,
                "device_path": qemu_disk.device_path,
                "nbd_unix_sock": qemu_disk.nbd_unix_sock,
                "scm_proxy_pid": qemu_disk.scm_proxy_pid,
                "scm_sock_path": qemu_disk.scm_sock_path,
            },
            f,
        )


def load_metadata(dbg, key):
    """Load qemu-dp metadata.

    Args:
        dbg: Debug context
        key: VDI key

    Returns:
        QemuDiskRaw instance or None if not found
    """
    meta_dir = _metadata_dir(key)
    meta_file = os.path.join(meta_dir, METADATA_FILE)

    if not os.path.exists(meta_file):
        log.debug("{}: no metadata found at {}".format(dbg, meta_file))
        return None

    log.debug("{}: loading metadata from {}".format(dbg, meta_file))

    with open(meta_file, "rb") as f:
        data = pickle.load(f)  # nosec B301  # nosemgrep

    return QemuDiskRaw(
        data["pid"],
        data["qmp_sock"],
        data["key"],
        data.get("device_path"),
        nbd_unix_sock=data.get("nbd_unix_sock"),
        nbd_server_running=True,
        scm_proxy_pid=data.get("scm_proxy_pid"),
        scm_sock_path=data.get("scm_sock_path"),
    )


def remove_metadata(dbg, key):
    """Remove qemu-dp metadata.

    Args:
        dbg: Debug context
        key: VDI key
    """
    meta_dir = _metadata_dir(key)
    meta_file = os.path.join(meta_dir, METADATA_FILE)

    if os.path.exists(meta_file):
        log.debug("{}: removing metadata at {}".format(dbg, meta_file))
        os.unlink(meta_file)

    # Try to remove empty directory
    try:
        os.rmdir(meta_dir)
    except OSError:
        pass


def _sr_mount_from_uri(sr_uri):
    """Decode `file://<path>` SR URIs to their on-disk mount path.

    libcow callers typically pass the URI form (`'file://' + sr_mount`)
    around so the SMAPIv3 contract stays uniform. Strip the scheme
    here so callers don't have to. Accepts a bare path too -- useful
    in unit tests that don't bother with the scheme.
    """
    if sr_uri.startswith("file://"):
        return sr_uri[len("file://") :]
    return sr_uri


def _cbt_metadata_dir(sr_uri):
    """Compute the per-SR CBT metadata directory.

    Layout: `<sr-mount>/.zfs-live/cbt/`. The `.zfs-live` parent is
    driver-owned scratch space -- co-locating CBT state with libcow's
    own `meta.json` + `sqlite3-metadata.db` under the SR mount means
    the data follows the SR through detach / reattach / migrate, and
    survives host reboot for as long as the SR itself is intact.

    `xe sr-detach` unmounts this directory along with the rest of
    the SR mount, then `xe sr-attach` (or its post-reboot equivalent)
    re-creates the path under the new mount -- same filesystem, same
    files. No host-side state to migrate."""
    return os.path.join(_sr_mount_from_uri(sr_uri), DRIVER_METADATA_DIRNAME, CBT_METADATA_SUBDIR)


def _cbt_metadata_path(sr_uri, key):
    """Per-VDI CBT metadata file under the SR's CBT directory.

    One file per VDI key (`<sr-mount>/.zfs-live/cbt/<key>.pickle`)
    rather than a per-key subdirectory, because there's only one
    CBT state object per VDI today and a flat layout is simpler to
    reason about for the destroy-cleanup path."""
    return os.path.join(_cbt_metadata_dir(sr_uri), key + ".pickle")


def save_cbt_metadata(dbg, sr_uri, key, cbt_data):
    """Persist CBT bitmap state durably under the SR mount.

    Raw block devices don't support persistent QEMU dirty bitmaps, so
    bitmap data must be saved externally before the qemu-dp process
    exits. The Volume plugin calls this after exporting bitmap data
    (e.g. during snapshot or deactivate). State persists across
    qemu-dp restart, host reboot, and SR detach/reattach for as long
    as the SR's own dataset is intact (#30).

    Args:
        dbg: Debug context
        sr_uri: SR URI (`'file://' + <sr-mount>`); see `_sr_mount_from_uri`
        key: VDI key
        cbt_data: Dict with bitmap state to persist, typically:
            - bitmap_name: name of the frozen bitmap
            - granularity: tracking granularity in bytes
            - dirty_count: number of dirty bytes at export time
            - nbd_export: connection info from cbt_export_bitmap()
              (or actual bitmap payload if extracted by the caller)
    """
    meta_dir = _cbt_metadata_dir(sr_uri)
    _mkdir_p(meta_dir)
    meta_file = _cbt_metadata_path(sr_uri, key)

    log.debug("{}: saving CBT metadata to {}".format(dbg, meta_file))

    # Write through a `<file>.tmp` and rename to keep readers from
    # seeing a half-written pickle if save_cbt_metadata is interrupted
    # (e.g., qemu-dp crash mid-export). Same atomic-replace pattern
    # used by `state.AttachedSRs._save_locked` in the shim.
    tmp_file = meta_file + ".tmp"
    with open(tmp_file, "wb") as f:
        pickle.dump(cbt_data, f)  # nosemgrep
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp_file, meta_file)


def load_cbt_metadata(dbg, sr_uri, key):
    """Load persisted CBT bitmap state for a VDI.

    Args:
        dbg: Debug context
        sr_uri: SR URI (`'file://' + <sr-mount>`)
        key: VDI key

    Returns:
        Dict with saved CBT state, or None if not found
    """
    meta_file = _cbt_metadata_path(sr_uri, key)

    if not os.path.exists(meta_file):
        log.debug("{}: no CBT metadata found at {}".format(dbg, meta_file))
        return None

    log.debug("{}: loading CBT metadata from {}".format(dbg, meta_file))

    with open(meta_file, "rb") as f:
        return pickle.load(f)  # nosec B301  # nosemgrep


def migrate_legacy_cbt_metadata(dbg, sr_uri, legacy_key, new_key):
    """One-shot in-place migration for #104.

    Pre-#104 the datapath lifecycle hooks persisted CBT state
    under `_device_key(device_path)` (sha256[:16]); the unified
    shape uses the libcow VDI uuid. On upgrade we want existing
    `<legacy_key>.pickle` files to keep working -- so this helper
    is called by every lifecycle path that knows both keys for a
    given VDI:

      - if `<legacy_key>.pickle` exists and `<new_key>.pickle`
        does not: rename, so subsequent ops find the state under
        the unified key.
      - if both exist: the new-shape file wins (it's a fresh
        write from the upgraded code path); drop the legacy.
      - otherwise: no-op.

    Idempotent. Returns True if a migration happened, False
    otherwise -- useful for tests; production callers ignore.
    """
    legacy_file = _cbt_metadata_path(sr_uri, legacy_key)
    new_file = _cbt_metadata_path(sr_uri, new_key)

    if not os.path.exists(legacy_file):
        return False

    if not os.path.exists(new_file):
        log.info(
            "{}: migrating legacy CBT metadata {} -> {} (#104)".format(dbg, legacy_file, new_file)
        )
        os.rename(legacy_file, new_file)
        return True

    log.info(
        "{}: dropping superseded legacy CBT metadata {} (new "
        "shape already at {}) (#104)".format(dbg, legacy_file, new_file)
    )
    os.unlink(legacy_file)
    return True


def remove_cbt_metadata(dbg, sr_uri, key):
    """Remove persisted CBT bitmap state for a VDI.

    Idempotent: if the file isn't present, return without error.
    Called from Volume.destroy / VDI cleanup paths so the SR's
    `.zfs-live/cbt/` directory stays in sync with live VDI keys.
    """
    meta_file = _cbt_metadata_path(sr_uri, key)

    if os.path.exists(meta_file):
        log.debug("{}: removing CBT metadata at {}".format(dbg, meta_file))
        os.unlink(meta_file)

    # Best-effort: clean up the parent dir if it's now empty so the
    # SR mount doesn't grow a stale `.zfs-live/cbt/` over time.
    # Don't propagate errors -- a non-empty dir or a permissions issue
    # is fine here.
    try:
        os.rmdir(_cbt_metadata_dir(sr_uri))
    except OSError:
        pass
