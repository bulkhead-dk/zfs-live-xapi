#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint: disable=too-many-lines
"""
Datapath implementation for raw-qdisk.

This module provides the full SMAPIv3 datapath lifecycle for raw block
devices (ZFS zvols):

  - attach/activate/deactivate/detach -- standard VM disk lifecycle
  - open/close -- disk setup/teardown around VM start/stop
  - import_activate -- inbound NBD endpoint for live storage migration

The datapath returns BlockDevice implementations, letting xenopsd handle
the actual device connection via qemu-dp (for qdisk backend) or directly
(for vbd backend). This ensures proper integration with Xen's device model.
"""

import hashlib
import os
import signal
import subprocess
import sys

# Add xapi storage libs to path
sys.path.insert(0, "/usr/libexec/xapi-storage-script/")

import xapi.storage.api.v5.datapath
from xapi.storage import log

import nbd_proxy
import qemudisk_raw

# URI format: raw+qdisk://<sr-type>/<device-path>
# Example: raw+qdisk://zfs-live//dev/zvol/sr-pool/vdi-123

# /dev/zvol/ prefix stripped to get the ZFS zvol path
ZVOL_DEV_PREFIX = "/dev/zvol/"


def _zfs_get_property(dbg, zvol_path, prop):
    """Query a single ZFS property value from a zvol.

    Returns the property value as a string, or None on failure.
    """
    cmd = [  # pylint: disable=redefined-outer-name
        "zfs",
        "get",
        "-Hp",
        "-o",
        "value",
        prop,
        zvol_path,
    ]
    try:
        result = subprocess.check_output(cmd, stderr=subprocess.PIPE)
        return result.decode("utf-8", errors="replace").strip()
    except subprocess.CalledProcessError as e:
        log.debug("%s: zfs get %s %s failed: %s", dbg, prop, zvol_path, e)
        return None


def _get_block_device_sectors(dbg, device_path):
    """Read sector sizes from the block device's sysfs queue.

    ZFS zvol devices at /dev/zvol/... are symlinks to /dev/zdN.
    The kernel exposes sector sizes via /sys/block/zdN/queue/.

    Returns:
        tuple: (logical_block_size, physical_block_size) or (None, None)
    """
    try:
        real_path = os.path.realpath(device_path)
        dev_name = os.path.basename(real_path)
        sysfs_queue = "/sys/block/{}/queue".format(dev_name)

        logical = None
        physical = None

        logical_path = os.path.join(sysfs_queue, "logical_block_size")
        if os.path.exists(logical_path):
            with open(logical_path) as f:  # pylint: disable=unspecified-encoding
                logical = int(f.read().strip())

        physical_path = os.path.join(sysfs_queue, "physical_block_size")
        if os.path.exists(physical_path):
            with open(physical_path) as f:  # pylint: disable=unspecified-encoding
                physical = int(f.read().strip())

        return (logical, physical)
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.debug(
            "%s: failed to read sysfs sector sizes for %s: %s", dbg, device_path, e
        )
        return (None, None)


def _get_zvol_features(dbg, device_path):
    """Auto-detect VBD features from ZFS properties and block device info.

    Queries refreservation to determine discard eligibility:
    - discard: enabled for thin (refreservation=0), disabled for thick

    Reads actual sector sizes from sysfs (not volblocksize, which can
    exceed the 512/4096 values Xen VBDs accept).

    Also reads volblocksize, copies, and compression for logging.

    Args:
        device_path: Block device path (e.g., /dev/zvol/pool/ds/uuid)

    Returns:
        Dict with detected features, empty dict on failure.
    """
    if not device_path.startswith(ZVOL_DEV_PREFIX):
        log.debug(
            "%s: not a zvol device, skipping feature detection: %s", dbg, device_path
        )
        return {}

    zvol_path = device_path[len(ZVOL_DEV_PREFIX) :]
    features = {}

    try:
        # Detect provisioning type from refreservation
        refreservation_str = _zfs_get_property(dbg, zvol_path, "refreservation")
        if refreservation_str is not None:
            if refreservation_str in ("0", "none", "-", ""):
                refreservation = 0
            else:
                refreservation = int(refreservation_str)

            if refreservation > 0:
                features["discard"] = False
                features["provisioning"] = "thick"
            else:
                features["discard"] = True
                features["provisioning"] = "thin"

        # volblocksize (for discard granularity and logging, NOT sector size)
        volblocksize_str = _zfs_get_property(dbg, zvol_path, "volblocksize")
        if volblocksize_str is not None:
            features["volblocksize"] = int(volblocksize_str)

        # Actual sector sizes from sysfs -- these are what Xen VBDs accept
        # (typically 512 or 4096, unlike volblocksize which can be up to 128K)
        logical_bs, physical_bs = _get_block_device_sectors(dbg, device_path)
        if logical_bs is not None:
            features["logical_sector_size"] = logical_bs
        if physical_bs is not None:
            features["physical_sector_size"] = physical_bs

        # Info properties (logged for visibility, not passed to Xen)
        copies = _zfs_get_property(dbg, zvol_path, "copies")
        if copies is not None:
            features["copies"] = copies

        compression = _zfs_get_property(dbg, zvol_path, "compression")
        if compression is not None:
            features["compression"] = compression

    except Exception as e:  # pylint: disable=broad-exception-caught
        log.error("%s: failed to auto-detect features for %s: %s", dbg, zvol_path, e)

    return features


def parse_uri(uri):
    """Parse a raw+qdisk:// URI and return the device path.

    Args:
        uri: URI like raw+qdisk://zfs-live//dev/zvol/pool/vol

    Returns:
        Tuple of (sr_type, device_path)
    """
    # Remove scheme
    if uri.startswith("raw+qdisk://"):
        path = uri[len("raw+qdisk://") :]
    else:
        raise ValueError("Invalid URI scheme: {}".format(uri))

    # Split sr-type from device path
    # Format: <sr-type>/<device-path>
    # The device path starts with /dev/ so we split on the first /dev/
    if "/dev/" in path:
        idx = path.index("/dev/")
        sr_type = path[:idx].rstrip("/")
        device_path = path[idx:]
        return (sr_type, device_path)
    raise ValueError("Invalid URI format, expected /dev/ in path: {}".format(uri))


class Implementation(xapi.storage.api.v5.datapath.Datapath_skeleton):
    """Datapath implementation for raw block devices.

    Returns BlockDevice implementations to let xenopsd manage device connections.

    Implemented operations:
        attach/activate/deactivate/detach -- standard VM disk lifecycle
        open/close -- disk setup/teardown around VM start/stop
        import_activate -- NBD endpoint for inbound live storage migration
    """

    def attach(self, dbg, uri, domain):
        """Prepare a connection between storage and VM domain.

        Returns implementations for multiple backends:
        - XenDisk: For Xen PV backends (vbd or qdisk)
        - BlockDevice: For QEMU emulation (position 0-3)
        - Nbd: For inbound storage migration (SXM) and VDI copy

        The Nbd implementation is required by XAPI for:
        - xe vdi-copy (sparse_dd -> import_nbd_proxy -> get_nbd_server)
        - SMAPIv3 live migration (receive_start3 -> attach3 -> nbd_export)
        - Copy phase of any SXM operation

        A qemu-storage-daemon is spawned (or reused if already running)
        to export the zvol via NBD.  For normal VM I/O, xenopsd uses the
        XenDisk implementation and the daemon sits idle.

        Args:
            dbg: Debug context string
            uri: Storage URI (raw+qdisk://...)
            domain: Opaque domain identifier

        Returns:
            Dict with implementations list for XAPI
        """
        log.debug("{}: raw-qdisk attach uri={} domain={}".format(dbg, uri, domain))

        try:
            sr_type, device_path = parse_uri(uri)
            log.debug(
                "{}: parsed sr_type={} device_path={}".format(dbg, sr_type, device_path)
            )
        except ValueError as e:
            log.error("{}: failed to parse URI: {}".format(dbg, e))
            raise

        # Verify the device exists
        if not os.path.exists(device_path):
            log.error("{}: device path does not exist: {}".format(dbg, device_path))
            raise Exception(  # pylint: disable=broad-exception-raised
                "Device path does not exist: {}".format(device_path)
            )  # pylint: disable=broad-exception-raised

        # Auto-detect features from ZFS properties
        features = _get_zvol_features(dbg, device_path)

        log.debug(
            "%s: auto-detected features for %s: "
            "provisioning=%s, discard=%s, volblocksize=%s, "
            "logical_sector=%s, physical_sector=%s, "
            "copies=%s, compression=%s",
            dbg,
            device_path,
            features.get("provisioning", "unknown"),
            features.get("discard", "unknown"),
            features.get("volblocksize", "unknown"),
            features.get("logical_sector_size", "unknown"),
            features.get("physical_sector_size", "unknown"),
            features.get("copies", "unknown"),
            features.get("compression", "unknown"),
        )

        # Build XenDisk extra parameters from auto-detected features.
        extra = {}
        if "discard" in features:
            extra["discard-enable"] = str(features["discard"])
            if features["discard"] and "volblocksize" in features:
                extra["discard-granularity"] = str(features["volblocksize"])
        if "physical_sector_size" in features:
            extra["physical-sector-size"] = str(features["physical_sector_size"])
        if "logical_sector_size" in features:
            extra["logical-sector-size"] = str(features["logical_sector_size"])

        implementations = [
            ["XenDisk", {"backend_type": "vbd", "params": device_path, "extra": extra}],
            ["BlockDevice", {"path": device_path}],
        ]

        # Ensure a qemu-storage-daemon is running so we can provide an
        # Nbd implementation.  XAPI's get_nbd_server (used by
        # import_nbd_proxy, sparse_dd copy, and SMAPIv3 receive_start3)
        # calls attach() and extracts the Nbd URI.
        key = _device_key(device_path)
        qemu_disk = _ensure_qemu_daemon(dbg, key, device_path)
        if qemu_disk:
            nbd_uri = "nbd+unix:///{}?socket={}".format(
                qemudisk_raw.BLOCK_NODE_NAME, qemu_disk.nbd_unix_sock
            )
            implementations.append(["Nbd", {"uri": nbd_uri}])
            log.debug("{}: attach: Nbd implementation at {}".format(dbg, nbd_uri))

        log.debug(
            "{}: returning {} implementations for {}".format(
                dbg, len(implementations), device_path
            )
        )

        return {"implementations": implementations}

    def activate(self, dbg, uri, domain):
        """Called just before a VM needs to read/write its disk.

        With direct block device access, xenopsd/qemu-dp handles activation.
        Logs auto-detected ZFS features for debugging.

        Also restores any persisted CBT bitmap containers for this VDI
        (#100). Restoration is **bounded to driver-tracked qemu-dp
        instances** -- for VDIs that pass only through xenopsd's qemu-dp
        (typical VM I/O), the helper no-ops cleanly. xenopsd-spawned
        qemu-dp discovery is a separate, larger problem tracked under
        the same epic (#96).

        Args:
            dbg: Debug context string
            uri: Storage URI
            domain: Opaque domain identifier
        """
        log.debug("{}: raw-qdisk activate uri={} domain={}".format(dbg, uri, domain))

        try:
            _, device_path = parse_uri(uri)
        except ValueError as e:
            log.error("{}: failed to parse URI: {}".format(dbg, e))
            raise

        # Verify device still exists
        if not os.path.exists(device_path):
            log.error("{}: device path does not exist: {}".format(dbg, device_path))
            raise Exception(  # pylint: disable=broad-exception-raised
                "Device path does not exist: {}".format(device_path)
            )  # pylint: disable=broad-exception-raised

        # Log ZFS features at activation for debugging
        features = _get_zvol_features(dbg, device_path)
        if features:
            log.debug(
                "%s: activate features for %s: "
                "provisioning=%s, discard=%s, volblocksize=%s, "
                "logical_sector=%s, physical_sector=%s, "
                "copies=%s, compression=%s",
                dbg,
                device_path,
                features.get("provisioning", "unknown"),
                features.get("discard", "unknown"),
                features.get("volblocksize", "unknown"),
                features.get("logical_sector_size", "unknown"),
                features.get("physical_sector_size", "unknown"),
                features.get("copies", "unknown"),
                features.get("compression", "unknown"),
            )

        # CBT lifecycle: re-prepare bitmap containers from persisted state.
        # Wrapped in a broad try/except -- activate must not block VM
        # start because of internal CBT plumbing.
        try:
            _cbt_restore_on_activate(dbg, device_path)
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.warning("{}: CBT restore failed (continuing): {}".format(dbg, e))

        log.debug("{}: raw-qdisk activate complete".format(dbg))

    def deactivate(self, dbg, uri, domain):
        """Called when a VM has finished reading/writing its disk.

        With direct block device access, xenopsd/qemu-dp handles deactivation.

        On the CBT side (#100), this is the last chance to read the
        live dirty bitmaps off a driver-tracked qemu-dp before its
        process exits. We persist them via `save_cbt_metadata` so a
        subsequent activate can re-prepare the bitmap containers.

        Per the SMAPIv3 contract, deactivate must never raise -- the
        CBT save side is wrapped so an internal failure can't break
        the operator-facing path.

        Args:
            dbg: Debug context string
            uri: Storage URI
            domain: Opaque domain identifier
        """
        log.debug("{}: raw-qdisk deactivate uri={} domain={}".format(dbg, uri, domain))

        try:
            _, device_path = parse_uri(uri)
        except ValueError as e:
            # Even URI-parse failures get swallowed -- deactivate must
            # not raise per the SMAPIv3 contract.
            log.warning(
                "{}: deactivate URI parse failed (continuing): " "{}".format(dbg, e)
            )
            return

        try:
            _cbt_persist_on_deactivate(dbg, device_path)
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.warning("{}: CBT persist failed (continuing): {}".format(dbg, e))

        log.debug("{}: raw-qdisk deactivate complete".format(dbg))

    def detach(self, dbg, uri, domain):
        """Called sometime after a VM has finished to clean up resources.

        Cleans up:
        - qemu-storage-daemon spawned for NBD export (attach / import_activate)
        - SCM_RIGHTS proxy process spawned by import_activate

        Without this, the daemon holds the zvol open and subsequent
        zfs destroy fails with "dataset is busy".

        This method should never fail per the API spec.

        Args:
            dbg: Debug context string
            uri: Storage URI
            domain: Opaque domain identifier
        """
        log.debug("{}: raw-qdisk detach uri={} domain={}".format(dbg, uri, domain))

        try:
            _, device_path = parse_uri(uri)
        except ValueError as e:
            log.debug("{}: failed to parse URI: {}".format(dbg, e))
            return  # Never fail per API spec

        key = _device_key(device_path)
        qemu_disk = qemudisk_raw.load_metadata(dbg, key)
        if qemu_disk:
            log.debug(
                "{}: cleaning up qemu-storage-daemon pid={} "
                "(scm_proxy={})".format(dbg, qemu_disk.pid, qemu_disk.scm_proxy_pid)
            )
            try:
                qemu_disk.quit(dbg)
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.warning("{}: failed to stop qemu-storage-daemon: {}".format(dbg, e))
            qemudisk_raw.remove_metadata(dbg, key)

        log.debug("{}: raw-qdisk detach complete".format(dbg))

    def open(self, dbg, uri, persistent):
        """Called before a disk is attached to a VM.

        For raw zvols, this is a no-op. The device is always persistent
        (writes go directly to the zvol).

        Args:
            dbg: Debug context string
            uri: Storage URI
            persistent: If True, persist all writes. If False, may discard.
        """
        log.debug(
            "{}: raw-qdisk open uri={} persistent={}".format(dbg, uri, persistent)
        )

        if not persistent:
            log.warning(
                "{}: non-persistent mode not supported for raw zvols, "
                "writes will be persisted regardless".format(dbg)
            )

        log.debug("{}: raw-qdisk open complete".format(dbg))

    def close(self, dbg, uri):
        """Called after a disk is detached and VM shutdown.

        Final cleanup for qemu-storage-daemon and SCM proxy processes.

        Args:
            dbg: Debug context string
            uri: Storage URI
        """
        log.debug("{}: raw-qdisk close uri={}".format(dbg, uri))

        try:
            _, device_path = parse_uri(uri)
        except ValueError as e:
            log.error("{}: failed to parse URI: {}".format(dbg, e))
            return

        key = _device_key(device_path)
        qemu_disk = qemudisk_raw.load_metadata(dbg, key)
        if qemu_disk:
            log.debug(
                "{}: close: cleaning up qemu-storage-daemon pid={} "
                "(scm_proxy={})".format(dbg, qemu_disk.pid, qemu_disk.scm_proxy_pid)
            )
            try:
                qemu_disk.quit(dbg)
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.warning("{}: failed to stop qemu-storage-daemon: {}".format(dbg, e))
            qemudisk_raw.remove_metadata(dbg, key)

        log.debug("{}: raw-qdisk close complete".format(dbg))

    def import_activate(self, dbg, uri, domain):
        """Prepare an SCM_RIGHTS listening socket for inbound storage migration.

        XAPI's nbd_handler (used for SMAPIv1->SMAPIv3 migration) connects
        to the returned socket path and sends an HTTP file descriptor via
        SCM_RIGHTS.  A background proxy process receives this fd and
        proxies data between it and the qemu-storage-daemon's NBD socket.

        Flow:
          1. Ensure qemu-storage-daemon is running (spawn or reuse)
          2. Create a Unix listening socket for SCM_RIGHTS
          3. Fork a proxy daemon that accepts, receives fd, and proxies
          4. Return the listening socket path to XAPI

        Args:
            dbg: Debug context string
            uri: Storage URI of the destination VDI
            domain: Domain identifier (typically 0 for Dom0)

        Returns:
            Path to a UNIX domain socket for SCM_RIGHTS fd passing
        """
        log.debug(
            "{}: raw-qdisk import_activate uri={} domain={}".format(dbg, uri, domain)
        )

        try:
            _, device_path = parse_uri(uri)
        except ValueError as e:
            log.error("{}: failed to parse URI: {}".format(dbg, e))
            raise

        if not os.path.exists(device_path):
            raise Exception(  # pylint: disable=broad-exception-raised
                "Destination device does not exist: {}".format(device_path)
            )  # pylint: disable=broad-exception-raised

        key = _device_key(device_path)

        # Reuse existing daemon and SCM proxy if still running (XAPI may
        # call import_activate twice via Storage_mux).
        existing = qemudisk_raw.load_metadata(dbg, key)
        if existing:
            try:
                os.kill(existing.pid, 0)
                # qemu-storage-daemon alive -- check SCM proxy too
                if existing.scm_proxy_pid is not None and existing.scm_sock_path:
                    try:
                        os.kill(existing.scm_proxy_pid, 0)
                        log.debug(
                            "{}: reusing existing daemon pid={} and "
                            "SCM proxy pid={}".format(
                                dbg, existing.pid, existing.scm_proxy_pid
                            )
                        )
                        return existing.scm_sock_path
                    except OSError:
                        # SCM proxy dead, but daemon alive -- create new proxy
                        log.debug("{}: SCM proxy dead, creating new one".format(dbg))
                # Daemon alive but no SCM proxy -- fall through to create one
            except OSError:
                # Daemon dead -- clean up stale metadata
                log.debug("{}: cleaning up dead qemu-storage-daemon".format(dbg))
                try:
                    existing.quit(dbg)
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
                qemudisk_raw.remove_metadata(dbg, key)
                existing = None

        # Ensure qemu-storage-daemon is running
        qemu_disk = _ensure_qemu_daemon(dbg, key, device_path)

        # For SXM: also start a TCP NBD server so the source host can
        # mirror blocks directly to this VDI via `mirror_start_nbd`.
        # The port is written to a sidecar file so the source proxy
        # can discover it.
        nbd_port = _start_tcp_nbd_export(dbg, qemu_disk, key)
        if nbd_port:
            log.debug(
                "{}: import_activate: TCP NBD export on port {} "
                "for cross-host mirror".format(dbg, nbd_port)
            )

        # Create SCM_RIGHTS listening socket and fork proxy daemon
        # (used by xapi's native nbd_handler path)
        scm_path = os.path.join(qemudisk_raw._socket_dir(), "scm.{}".format(key))
        proxy_pid = nbd_proxy.start_scm_daemon(scm_path, qemu_disk.nbd_unix_sock)

        # Track proxy process for cleanup
        qemu_disk.scm_proxy_pid = proxy_pid
        qemu_disk.scm_sock_path = scm_path
        qemu_disk.nbd_tcp_port = nbd_port
        qemudisk_raw.save_metadata(dbg, key, qemu_disk)

        log.debug(
            "{}: import_activate: SCM socket at {} (proxy pid={})".format(
                dbg, scm_path, proxy_pid
            )
        )

        return scm_path


def _gc_orphan_daemons(dbg, current_key):
    """Reap qemu-storage-daemons whose backing zvol no longer exists.

    The driver spawns a qemu-storage-daemon on every Datapath.attach
    and is responsible for killing it on Datapath.detach. When XAPI's
    enclosing operation aborts (vdi-copy hang, operator Ctrl-C, ...),
    Datapath.detach is never called and the daemon leaks. The leak
    compounds: each abort adds another stale daemon, and the host
    eventually accumulates enough contention to make fresh operations
    hang in turn.

    This GC runs lazily on every Datapath.attach / import_activate
    call (no background daemons, no timers). For each metadata entry
    on disk:

    * If the recorded device_path no longer exists on disk, the
      backing zvol has been destroyed -- the daemon is provably no
      longer in use, kill it and drop the metadata.
    * If the metadata is unreadable / corrupt, drop it.
    * If the recorded pid is no longer alive (daemon died but its
      metadata survived), drop the metadata.
    * Otherwise the daemon may legitimately be serving a concurrent
      attach -- leave it alone.

    The current_key (the daemon we're about to attach or have just
    attached) is always skipped -- never GC the daemon we're about to
    use.
    """
    meta_root = qemudisk_raw._dp_metadata_root()
    try:
        entries = os.listdir(meta_root)
    except OSError:
        return  # No metadata dir yet, nothing to GC

    for entry_key in entries:
        if entry_key == current_key:
            continue
        try:
            entry_meta = qemudisk_raw.load_metadata(dbg, entry_key)
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Corrupt / unreadable -- drop it.
            log.debug(
                "{}: GC: dropping unreadable metadata {}: {}".format(dbg, entry_key, e)
            )
            qemudisk_raw.remove_metadata(dbg, entry_key)
            continue
        if entry_meta is None:
            # Empty entry directory -- best-effort cleanup.
            try:
                os.rmdir(os.path.join(meta_root, entry_key))
            except OSError:
                pass
            continue

        # If the daemon's recorded pid is dead, drop the metadata.
        try:
            os.kill(entry_meta.pid, 0)
            pid_alive = True
        except OSError:
            pid_alive = False
        if not pid_alive:
            log.debug(
                "{}: GC: dropping metadata for dead pid={} "
                "(key={})".format(dbg, entry_meta.pid, entry_key)
            )
            qemudisk_raw.remove_metadata(dbg, entry_key)
            continue

        # The safe-to-reap predicate: backing zvol vanished.
        device_path = entry_meta.device_path
        if device_path and os.path.exists(device_path):
            # Daemon may legitimately be serving a concurrent attach.
            continue

        log.warning(
            "{}: GC: reaping orphan qemu-storage-daemon "
            "pid={} key={} device_path={} (backing zvol gone)".format(
                dbg, entry_meta.pid, entry_key, device_path
            )
        )
        try:
            entry_meta.quit(dbg)
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.warning(
                "{}: GC: quit failed for pid={}, falling back to "
                "SIGKILL: {}".format(dbg, entry_meta.pid, e)
            )
            try:
                os.kill(entry_meta.pid, signal.SIGKILL)
            except OSError:
                pass
        qemudisk_raw.remove_metadata(dbg, entry_key)


def _ensure_qemu_daemon(dbg, key, device_path):
    """Ensure a qemu-storage-daemon is running for the given device.

    Reuses an existing daemon if alive, otherwise spawns a new one.
    Metadata is persisted so other datapath methods can find the daemon.

    Always sweeps orphan daemons for unrelated keys before deciding
    whether to spawn -- this is the recovery path for any aborted SXM
    operation that left a daemon behind without its corresponding
    Datapath.detach.

    Args:
        dbg: Debug context string
        key: Device key (hash of device_path)
        device_path: Block device path

    Returns:
        QemuDiskRaw instance, or None on failure
    """
    # Reap leaked daemons from previously-aborted operations.
    _gc_orphan_daemons(dbg, key)

    existing = qemudisk_raw.load_metadata(dbg, key)
    if existing:
        try:
            os.kill(existing.pid, 0)
            log.debug(
                "{}: reusing existing qemu-storage-daemon "
                "pid={}".format(dbg, existing.pid)
            )
            return existing
        except OSError:
            log.debug("{}: cleaning up dead qemu-storage-daemon".format(dbg))
            try:
                existing.quit(dbg)
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            qemudisk_raw.remove_metadata(dbg, key)

    try:
        qemu_disk = qemudisk_raw.create(dbg, key, device_path)
        qemudisk_raw.save_metadata(dbg, key, qemu_disk)
        log.debug(
            "{}: spawned qemu-storage-daemon pid={} for {}".format(
                dbg, qemu_disk.pid, device_path
            )
        )
        return qemu_disk
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.error(
            "{}: failed to spawn qemu-storage-daemon for {}: {}".format(
                dbg, device_path, e
            )
        )
        return None


def _device_key(device_path):
    """Generate a stable key from a device path.

    Args:
        device_path: Block device path

    Returns:
        Hex digest suitable for use as a key
    """
    return hashlib.sha256(device_path.encode("utf-8")).hexdigest()[:16]


def _start_tcp_nbd_export(dbg, qemu_disk, key):
    """Start a TCP NBD server on the qemu-dp instance for cross-host SXM.

    The source host's `mirror_start_nbd()` connects to this port to
    mirror blocks into the destination VDI. Uses qemu-dp's built-in
    NBD server via QMP `nbd-server-start` + `nbd-server-add`.

    Returns the TCP port number, or None if setup fails (non-fatal;
    the SCM_RIGHTS path remains available as fallback).
    """
    import socket as _socket  # pylint: disable=import-outside-toplevel

    # Find a free port
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    try:
        # Start NBD server on the qemu-dp instance
        qemu_disk.qmp_command(
            dbg,
            "nbd-server-start",
            addr={"type": "inet", "data": {"host": "0.0.0.0", "port": str(port)}},
        )
        # Export the block node
        qemu_disk.qmp_command(
            dbg,
            "nbd-server-add",
            device=qemu_disk.block_node_name,
            name="sxm-target",
            writable=True,
        )
        log.debug(
            "{}: TCP NBD export started on port {} "
            "(export='sxm-target')".format(dbg, port)
        )

        # Write the port to a sidecar so the source proxy can discover it
        port_file = os.path.join(qemudisk_raw._socket_dir(), "nbd-port.{}".format(key))
        with open(port_file, "w") as f:  # pylint: disable=unspecified-encoding
            f.write(str(port))

        return port
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.warning(
            "{}: failed to start TCP NBD export: {} "
            "(SCM_RIGHTS fallback remains)".format(dbg, e)
        )
        return None


# -- CBT lifecycle helpers (#100) --------------------------------------
#
# These wire `Datapath.activate` / `Datapath.deactivate` to the QMP
# primitives from PR #99 + the persistence helpers from PR #95. The
# wire-format is per-VDI multi-bitmap:
#
#   {bitmap_name: <export_bitmap payload>, ...}
#
# Both helpers are bounded to **driver-tracked qemu-dp instances**
# (those registered under `qemudisk_raw.save_metadata`). VDIs that
# only ever pass through xenopsd's qemu-dp (typical VM I/O) get a
# clean no-op -- xenopsd-qemu-dp QMP discovery is a separate problem.
#
# **Bitmap-name filter.** qemu-dp can carry multiple bitmaps at any
# moment: the long-lived driver-owned tracking bitmap (`cbt-active`
# per the convention documented in qemudisk_raw's CBT section
# header), plus transient frozen snapshot-era bitmaps named
# `cbt-snap-<uuid>` produced by `cbt_snapshot_bitmap`. Only the
# tracking bitmap is meant to survive a deactivate/activate cycle --
# the snapshot bitmaps are persisted under their own VDI keys at
# snapshot time (acceptance criterion 4 of #96, separate PR).
# Persisting the snapshot bitmaps from the active VDI's deactivate
# would resurrect them on the next activate even after the snapshot
# they belong to has been deleted.
#
# `_is_persistent_tracking_bitmap` is the single decision point;
# narrow it (or override its prefix) when a future driver-owned
# bitmap shape needs to ride the lifecycle too.

CBT_PERSISTENT_TRACKING_PREFIX = "cbt-active"


def _is_persistent_tracking_bitmap(bitmap_name):
    """Return True iff this bitmap participates in the
    deactivate/activate persistence contract.

    Today: any bitmap whose name starts with `cbt-active`. Frozen
    snapshot-era bitmaps (`cbt-snap-<uuid>`) and any other
    consumer-owned scratch bitmaps are filtered out -- they are
    NOT meant to ride the active VDI's lifecycle."""
    if not bitmap_name:
        return False
    return bitmap_name.startswith(CBT_PERSISTENT_TRACKING_PREFIX)


# --- xenopsd-qemu discovery (#106) ------------------------------------
#
# The CBT lifecycle hooks below were originally bounded to qemu-dp
# instances we spawned ourselves and registered via
# `qemudisk_raw.save_metadata`. For typical VM I/O the qemu instance
# living on the device is `qemu-dm-<domid>` spawned by xenopsd --
# outside our registry. Without discovery, the hooks no-op for the
# main case operators care about (incremental backups of running
# VMs).
#
# Discovery convention: xenopsd creates a QMP socket at
# `/var/run/xen/qmp-libxl-<domid>` for each `qemu-dm`. The socket
# isn't keyed by VDI; we identify the right one by asking each
# candidate's `query-block` whether it has our device path open.
# `QemuDiskRaw.connect_existing` already does that match
# (auto-detect block node from device path), so the discovery
# helper is just "glob + try connect, first match wins".

# QMP sockets for xenopsd-spawned qemu-dm instances live here. One
# per running domain.
_XEN_QMP_DIR = "/var/run/xen"
_XEN_QMP_SOCKET_GLOB = "qmp-libxl-*"

# NBD scratch dir for sockets we spawn-on-demand against an
# xenopsd-discovered qemu-dm. Driver-owned, segregated from
# `/var/run/xen/` so it doesn't collide with any xen-side server.
_DRIVER_NBD_SCRATCH_DIR = "/var/run/zfs-live"

# Per-socket QMP-connect timeout for the discovery scan. A stuck
# qemu-dm shouldn't hang the lifecycle hook indefinitely; we'd
# rather skip the suspect socket and let the hook no-op.
_DISCOVERY_QMP_CONNECT_TIMEOUT = 1.0


def _domid_from_qmp_socket(qmp_sock_path):
    """`/var/run/xen/qmp-libxl-7` -> `7`. Used to derive the NBD
    scratch socket path. Returns None on unrecognised shape."""
    base = os.path.basename(qmp_sock_path)  # pylint: disable=redefined-outer-name
    prefix = "qmp-libxl-"
    if not base.startswith(prefix):
        return None
    return base[len(prefix) :]


def _find_xenopsd_qemu_for_device(dbg, device_path):
    """Find the xenopsd-spawned qemu-dm that has `device_path`
    open as a block backend.

    Scans `/var/run/xen/qmp-libxl-*`, attempts to connect and
    auto-detect the block node on each. Returns the first match
    as a `QemuDiskRaw` (via `connect_existing`), or None if no
    socket carries this device -- VDI not currently attached to
    any running domain.

    Defensive shape: per-socket connect failures are swallowed
    (a stuck qemu-dm is a single-domain problem, not a host-
    wide one) and the absence of the QMP directory is a no-op
    (non-Xen environments / fresh installs).
    """
    import glob  # pylint: disable=import-outside-toplevel

    if not os.path.isdir(_XEN_QMP_DIR):
        log.debug(
            "{}: xenopsd-qemu discovery: {} missing -- "
            "no-op".format(dbg, _XEN_QMP_DIR)
        )
        return None

    sockets = sorted(glob.glob(os.path.join(_XEN_QMP_DIR, _XEN_QMP_SOCKET_GLOB)))
    if not sockets:
        log.debug(
            "{}: xenopsd-qemu discovery: no QMP sockets in "
            "{} -- no-op".format(dbg, _XEN_QMP_DIR)
        )
        return None

    # Canonicalise our target so we can compare against
    # `query-block` inserted.file regardless of /dev/zvol -> /dev/zd
    # symlink resolution. `connect_existing`'s detector already
    # handles this internally; we resolve here for the log line.
    try:
        target_real = os.path.realpath(device_path)
    except OSError:
        target_real = device_path

    for sock in sockets:
        try:
            domid = _domid_from_qmp_socket(sock)
            nbd_scratch = (
                os.path.join(_DRIVER_NBD_SCRATCH_DIR, "qmp-libxl-{}.nbd".format(domid))
                if domid
                else None
            )

            # Best-effort: ensure the scratch dir exists for the
            # NBD server `cbt_export_bitmap` will spawn on demand.
            try:
                os.makedirs(_DRIVER_NBD_SCRATCH_DIR)
            except OSError:
                if not os.path.isdir(_DRIVER_NBD_SCRATCH_DIR):
                    raise

            qd = qemudisk_raw.QemuDiskRaw.connect_existing(
                dbg, qmp_sock=sock, device_path=device_path, nbd_unix_sock=nbd_scratch
            )
            log.info(
                "{}: xenopsd-qemu discovery: matched {} via {} "
                "(domid={}, target={})".format(
                    dbg, device_path, sock, domid, target_real
                )
            )
            return qd
        except qemudisk_raw.QMPError as e:
            # Either device-not-found on this domain (expected; try
            # the next) or a connect failure (skip + continue).
            log.debug(
                "{}: xenopsd-qemu discovery: {} skipped: " "{}".format(dbg, sock, e)
            )
            continue
        except Exception as e:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.warning(
                "{}: xenopsd-qemu discovery: unexpected "
                "error on {}: {}".format(dbg, sock, e)
            )
            continue

    log.debug(
        "{}: xenopsd-qemu discovery: no socket carries "
        "{} -- no-op".format(dbg, device_path)
    )
    return None


def _vdi_uuid_from_device_path(device_path):
    """`/dev/zvol/<pool>/<dataset>/<vdi-uuid>` -> `<vdi-uuid>`.

    The CBT-metadata files (`<sr-mount>/.zfs-live/cbt/<key>.pickle`)
    are keyed by the libcow VDI uuid on the volume side
    (`Volume.destroy`, `Volume.snapshot`); the datapath side reads
    and writes the same files, so it must derive the same key
    shape (#104). The qemu-dp instance registry (`load_metadata` /
    `save_metadata`) keeps using `_device_key`'s sha256 hash --
    that's a separate concern with different requirements.
    """
    return os.path.basename(device_path)


def _resolve_sr_uri(dbg, device_path):
    """Derive the libcow-canonical SR URI from a zvol device path.

    `device_path` is `/dev/zvol/<pool>/<dataset>/<vdi-uuid>`. The
    parent dataset (`<pool>/<dataset>`) carries the SR marker
    property `xcp:sr=<sr-uuid>` set by `SR.create`; we read it via
    `zfs get` and assemble `'file:///var/run/sr-mount/<sr-uuid>'`.

    Returns the URI string, or None on any failure (caller's
    contract is to no-op on failure rather than raise -- both
    activate's CBT path and deactivate's must not break).
    """
    if not device_path.startswith(ZVOL_DEV_PREFIX):
        return None
    rel = device_path[len(ZVOL_DEV_PREFIX) :]
    parts = rel.rsplit("/", 1)
    if len(parts) != 2:
        return None
    parent_dataset = parts[0]

    sr_uuid = _zfs_get_property(dbg, parent_dataset, "xcp:sr")
    if not sr_uuid or sr_uuid == "-":
        return None
    return "file:///var/run/sr-mount/{}".format(sr_uuid)


def _cbt_persist_on_deactivate(dbg, device_path):
    """Save active bitmaps off a driver-tracked qemu-dp before it exits.

    No-op when (a) no driver-tracked qemu-dp is registered for this
    VDI, (b) no bitmaps are active on it, or (c) the SR mount can't
    be resolved. The all-no-op shape lets this run unconditionally
    on every deactivate without breaking VDIs not opted into CBT.
    """
    # Two distinct keys (#104):
    #   - `instance_key`: sha256 of the device path; identifies the
    #     qemu-dp process metadata (pid + qmp socket) in the
    #     `qemudisk_raw.{save,load}_metadata` registry.
    #   - `vdi_key`: the libcow VDI uuid; identifies the persisted
    #     CBT bitmap state on disk so it lines up with the volume-
    #     side hooks (`Volume.destroy -> remove_cbt_metadata`,
    #     `Volume.snapshot -> save_cbt_metadata`).
    instance_key = _device_key(device_path)
    vdi_key = _vdi_uuid_from_device_path(device_path)
    qemu_disk = qemudisk_raw.load_metadata(dbg, instance_key)
    if qemu_disk is None:
        # Fall through to xenopsd-spawned qemu-dm (#106). For
        # typical VM I/O the qemu instance with the device open is
        # outside our registry; the discovery scan finds it via
        # `/var/run/xen/qmp-libxl-*`. Returns None cleanly when no
        # running domain has this device -- keeps the no-op shape.
        qemu_disk = _find_xenopsd_qemu_for_device(dbg, device_path)
    if qemu_disk is None:
        log.debug(
            "{}: CBT persist: no qemu instance carries "
            "vdi={} -- no-op".format(dbg, vdi_key)
        )
        return

    all_bitmaps = qemu_disk.cbt_list_bitmaps(dbg)
    # Narrow to the driver-owned persistent tracking bitmap(s).
    # Snapshot-era frozen bitmaps and consumer-owned scratch
    # bitmaps don't ride this hook.
    bitmaps = [
        bm for bm in all_bitmaps if _is_persistent_tracking_bitmap(bm.get("name"))
    ]
    if not bitmaps:
        if all_bitmaps:
            log.debug(
                "{}: CBT persist: {} bitmap(s) present on "
                "vdi={} but none match the persistent "
                "tracking prefix '{}' -- no-op".format(
                    dbg, len(all_bitmaps), vdi_key, CBT_PERSISTENT_TRACKING_PREFIX
                )
            )
        else:
            log.debug(
                "{}: CBT persist: no active bitmaps on vdi={} "
                "-- no-op".format(dbg, vdi_key)
            )
        return

    sr_uri = _resolve_sr_uri(dbg, device_path)
    if sr_uri is None:
        log.warning(
            "{}: CBT persist: unable to resolve SR URI from "
            "{}, skipping save".format(dbg, device_path)
        )
        return

    # One-shot upgrade migration (#104): pre-#104 deactivates wrote
    # `<hash>.pickle`. We're inside the bitmaps-found branch -- the
    # only branch that could have produced a legacy file in the
    # first place -- so this is the right point to migrate. The
    # activate-side hook also runs migration unconditionally, so a
    # legacy file gets picked up on either the next deactivate-with-
    # bitmaps OR the next activate, whichever comes first.
    try:
        qemudisk_raw.migrate_legacy_cbt_metadata(dbg, sr_uri, instance_key, vdi_key)
    except Exception as e:  # noqa: BLE001  pylint: disable=broad-exception-caught
        log.warning(
            "{}: legacy CBT migration failed " "(continuing): {}".format(dbg, e)
        )

    payload = {}
    for bm in bitmaps:
        name = bm.get("name")
        if not name:
            continue
        try:
            payload[name] = qemu_disk.export_bitmap(dbg, name)
        except Exception as e:  # pylint: disable=broad-exception-caught
            # One bitmap failing must not lose the others.
            log.warning(
                "{}: CBT persist: export_bitmap('{}') failed: "
                "{}".format(dbg, name, e)
            )

    if not payload:
        log.debug(
            "{}: CBT persist: no exportable bitmaps for vdi={}".format(dbg, vdi_key)
        )
        return

    qemudisk_raw.save_cbt_metadata(dbg, sr_uri, vdi_key, payload)
    log.debug(
        "{}: CBT persist: saved {} bitmap(s) for vdi={}".format(
            dbg, len(payload), vdi_key
        )
    )


def _cbt_restore_on_activate(dbg, device_path):
    """Re-prepare bitmap containers on a driver-tracked qemu-dp.

    `prepare_bitmap_from_payload` only restores the empty container
    at the saved granularity -- re-marking the saved dirty extents
    is the consumer-side follow-up tracked under #96. So a VDI that
    flowed deactivate -> activate without an intervening consumer
    fetch comes back with empty bitmaps, but at the correct shape
    so subsequent writes are tracked compatibly.

    No-op when (a) no driver-tracked qemu-dp for this VDI, (b) no
    persisted state, or (c) SR mount unresolvable.
    """
    # Same dual-key shape as the persist side (see #104).
    instance_key = _device_key(device_path)
    vdi_key = _vdi_uuid_from_device_path(device_path)
    qemu_disk = qemudisk_raw.load_metadata(dbg, instance_key)
    if qemu_disk is None:
        # Fall through to xenopsd's qemu-dm (#106).
        qemu_disk = _find_xenopsd_qemu_for_device(dbg, device_path)
    if qemu_disk is None:
        log.debug(
            "{}: CBT restore: no qemu instance carries "
            "vdi={} -- no-op".format(dbg, vdi_key)
        )
        return

    sr_uri = _resolve_sr_uri(dbg, device_path)
    if sr_uri is None:
        log.debug(
            "{}: CBT restore: SR URI unresolvable for {}, "
            "skipping load".format(dbg, device_path)
        )
        return

    # One-shot upgrade migration: rename `<hash>.pickle` ->
    # `<vdi-uuid>.pickle` if a pre-#104 deactivate left the legacy
    # shape behind. Best-effort; a failure here just means the load
    # below sees no state and we no-op.
    try:
        qemudisk_raw.migrate_legacy_cbt_metadata(dbg, sr_uri, instance_key, vdi_key)
    except Exception as e:  # noqa: BLE001  pylint: disable=broad-exception-caught
        log.warning(
            "{}: legacy CBT migration failed " "(continuing): {}".format(dbg, e)
        )

    payload = qemudisk_raw.load_cbt_metadata(dbg, sr_uri, vdi_key)
    if not payload:
        log.debug("{}: CBT restore: no persisted state for vdi={}".format(dbg, vdi_key))
        return

    if not isinstance(payload, dict):
        log.warning(
            "{}: CBT restore: persisted state for vdi={} has "
            "unexpected shape ({}), skipping".format(
                dbg, vdi_key, type(payload).__name__
            )
        )
        return

    restored = 0
    skipped = 0
    for bitmap_name, bitmap_payload in payload.items():
        # Defence-in-depth: even if a previous (broader) writer
        # persisted a non-tracking bitmap, the restore side will
        # not resurrect it. Keeps the contract narrow on either
        # side of an upgrade.
        if not _is_persistent_tracking_bitmap(bitmap_name):
            log.debug(
                "{}: CBT restore: skipping '{}' (not a "
                "persistent tracking bitmap)".format(dbg, bitmap_name)
            )
            skipped += 1
            continue
        try:
            qemu_disk.prepare_bitmap_from_payload(dbg, bitmap_name, bitmap_payload)
            restored += 1
        except Exception as e:  # pylint: disable=broad-exception-caught
            # One bitmap failing must not block the others.
            log.warning(
                "{}: CBT restore: prepare_bitmap_from_payload"
                "('{}') failed: {}".format(dbg, bitmap_name, e)
            )

    log.debug(
        "{}: CBT restore: prepared {}/{} bitmap container(s) "
        "(skipped {} non-tracking) for vdi={}".format(
            dbg, restored, len(payload), skipped, vdi_key
        )
    )


if __name__ == "__main__":
    log.log_call_argv()
    cmd = xapi.storage.api.v5.datapath.Datapath_commandline(Implementation())
    base = os.path.basename(sys.argv[0])  # pylint: disable=redefined-outer-name

    if base == "Datapath.attach":
        cmd.attach()
    elif base == "Datapath.activate":
        cmd.activate()
    elif base == "Datapath.deactivate":
        cmd.deactivate()
    elif base == "Datapath.detach":
        cmd.detach()
    elif base == "Datapath.open":
        cmd.open()
    elif base == "Datapath.close":
        cmd.close()
    elif base == "Datapath.import_activate":
        cmd.import_activate()
    else:
        raise xapi.storage.api.v5.datapath.Unimplemented(base)
