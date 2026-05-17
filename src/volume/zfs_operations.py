# -*- coding: utf-8 -*-
# pylint: disable=too-many-lines
import hashlib
import os
import pickle
import subprocess
import time

import xapi
from xapi.storage import log

###

# Patterns the kernel / libzfs surface for an EBUSY-class error.
# Earlier code only matched `b"is busy"`; #243 generalised that
# to also catch the canonical errno strerror text for EBUSY (16)
# -- `Device or resource busy` -- which is the most durable
# signal because it comes from libc's strerror table, not
# OpenZFS-specific phrasing. A future OpenZFS could rephrase
# its dataset-level wording from "dataset is busy" to anything
# else, but the kernel-strerror line is universal across
# err-handling paths that format `errno` into the message.
#
# The "true" fix the original FIXME was reaching for is libzfs
# Python bindings (`pyzfs` / `python-libzfs`), so the retry
# decision could read a structured `lzc_*` return value
# directly. That's a packaging change (libzfs headers must be
# matched to the running kernel module) we're deliberately not
# taking on for v1.0. Until then, this helper is the structured
# layer between subprocess stderr and the retry loop.
#
# `expRc=2` is preserved: zfs/zpool CLIs return 2 for invocation
# errors (bad args, unknown subcommand) which are never
# retry-recoverable regardless of stderr content.
_BUSY_PATTERNS = (
    b"Device or resource busy",  # canonical EBUSY strerror
    b"is busy",  # ZFS-specific dataset/pool wording
    b"resource busy",  # variant phrasing seen in older builds
)


def is_busy_error(stderr_bytes, returncode):
    """Return True if `stderr_bytes` (subprocess stderr, raw bytes
    from `Popen.communicate()`) carries any signal that the failure
    was an EBUSY-class transient -- the kind of error a retry can
    legitimately recover from. Returns False for `returncode == 2`
    (CLI invocation error) regardless of stderr.

    Centralised so the busy-detection logic has one definition and
    one test surface; the previous inline substring check at the
    `run_zfs_command()` retry site was hard to extend safely (#243)."""
    if returncode == 2:
        return False
    return any(pat in stderr_bytes for pat in _BUSY_PATTERNS)


def run_zfs_command(
    dbg,
    cmd_args,
    error=True,
    simple=True,
    expRc=0,  # pylint: disable=invalid-name
    ntries=1,
    retry_delay_sec=0.1,
):
    "Subprocess wrapper with EBUSY retry for ZFS CLI commands"
    while ntries:
        log.debug("%s: Executing ZFS command: %s", dbg, cmd_args)
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True
        )
        stdout, stderr = proc.communicate()
        if error and proc.returncode != expRc:
            log.error(
                "%s: %s exited with code %s: %s",
                dbg,
                " ".join(cmd_args),
                proc.returncode,
                stderr,
            )
            if ntries > 1 and is_busy_error(stderr, proc.returncode):
                ntries -= 1
                log.debug("%s: EBUSY: retrying %s times", dbg, ntries)
                # Sleep before the retry -- the retry_delay_sec
                # parameter was accepted but never honoured (#213
                # finding 2), so retries hammered ZFS with zero
                # backoff and frequently lost the same race.
                if retry_delay_sec > 0:
                    time.sleep(retry_delay_sec)
                continue
            raise xapi.InternalError(
                "{} exited with non-zero code {}: {}".format(
                    " ".join(cmd_args), proc.returncode, stderr
                )
            )
        if simple:
            return stdout
        return stdout, stderr, proc.returncode
    return None


def run_zfs_command_with_retry(
    dbg,
    cmd_args,
    error=True,
    simple=True,
    expRc=0,  # pylint: disable=invalid-name
):
    return run_zfs_command(
        dbg, cmd_args, error=error, simple=simple, expRc=expRc, ntries=10
    )


###
# Valid values for ZFS properties exposed via xapi device-config /
# vdi sm-config. Defined once here so sr.py (device-config validator
# at SR-create time) and volume.py (per-VDI Volume.set validator)
# never drift apart. (#217 finding 3 -- pre-fix, sr.py had VALID_*
# constants and volume.py had a parallel VALID_MUTABLE_VALUES dict
# with the same values; adding a new property required two edits.)

VALID_COMPRESSION = {
    "off",
    "on",
    "lz4",
    "zstd",
    "zstd-fast",
    "gzip",
    "gzip-1",
    "gzip-2",
    "gzip-3",
    "gzip-4",
    "gzip-5",
    "gzip-6",
    "gzip-7",
    "gzip-8",
    "gzip-9",
    "zle",
    "lzjb",
}
VALID_COPIES = {"1", "2", "3"}
VALID_SYNC = {"standard", "always", "disabled"}
VALID_ATIME = {"on", "off"}
VALID_CACHE = {"all", "metadata", "none"}
VALID_LOGBIAS = {"latency", "throughput"}
VALID_PROVISIONING = {"thin", "thick"}
VALID_VOLBLOCKSIZE = {
    "4K": 4096,
    "8K": 8192,
    "16K": 16384,
    "32K": 32768,
    "64K": 65536,
    "128K": 131072,
}

# Per-VDI mutable ZFS properties -- both the set and per-property
# valid values, keyed for O(1) lookup. Subset of VALID_* above.
VALID_MUTABLE_VALUES = {
    "compression": VALID_COMPRESSION,
    "copies": VALID_COPIES,
    "sync": VALID_SYNC,
    "primarycache": VALID_CACHE,
    "secondarycache": VALID_CACHE,
    "logbias": VALID_LOGBIAS,
}
MUTABLE_ZFS_PROPERTIES = frozenset(VALID_MUTABLE_VALUES)
IMMUTABLE_ZFS_PROPERTIES = frozenset(["volblocksize", "provisioning"])

###

ZFS_LIVE_MOUNT_PREFIX = "/var/run/sr-mount"


def log_pool_tree(dbg, label, pool_name):
    """Log every dataset and snapshot under `pool_name` for debug
    purposes. Scope the listing to that pool with `-r` (#217 finding
    6); without it, this listed every dataset across every pool on
    the host, slow on multi-pool hosts."""
    cmd = "zfs list -t all -Hp -r -o name,origin".split() + [pool_name]
    log.debug("%s: %s: %s", dbg, label, run_zfs_command(dbg, cmd))


def format_zvol_name(pool_name, vol_id):
    return "{}/{}".format(pool_name, vol_id)


def vol_exists(zvol_path):
    """Check if a ZFS volume exists."""
    try:
        with open(os.devnull, "w") as devnull:  # pylint: disable=unspecified-encoding
            subprocess.check_call(
                ["zfs", "list", "-H", zvol_path], stdout=devnull, stderr=devnull
            )
        return True
    except subprocess.CalledProcessError:
        return False


def format_snap_name(pool_name, vol_id, snap_id):
    return "{}/{}@{}".format(pool_name, vol_id, snap_id)


# snapshot id is unique but full name will vary with "promote"
# operations, so we have to walk the full list to know its current
# name
def find_snapshot_by_uuid(dbg, pool_name, snap_id):
    cmd = "zfs list -t snapshot -Hp -o name -r".split() + [pool_name]
    snap_id = str(snap_id)
    for this_snap_name in run_zfs_command(dbg, cmd).strip().splitlines():
        _, this_snap_id = this_snap_name.split("@")
        if this_snap_id == snap_id:
            return this_snap_name
    return None


def list_zvol_snapshots(dbg, vol_name):
    cmd = "zfs list -Hp -t snapshot -o name".split() + [vol_name]
    return run_zfs_command(dbg, cmd).strip().splitlines()


def find_snapshot_clones(dbg, snap_name):
    """Yield zvols whose `origin` is `snap_name` -- i.e. clones of
    that snapshot. Scope the listing to the pool the snapshot lives
    in (#217 finding 5). Without `-r <pool>` this listed every
    dataset across every pool on the host, which is slow on hosts
    with many pools."""
    pool_name = snap_name.split("/", 1)[0].split("@", 1)[0]
    cmd = "zfs list -Hp -r -o name,origin".split() + [pool_name]
    for entry in run_zfs_command(dbg, cmd).strip().splitlines():
        zvol, origin = entry.split("\t")
        if origin == snap_name:
            yield zvol


###


def get_dataset_mountpoint(dbg, pool_name):
    cmd = "zfs get mountpoint -H -o value".split() + [pool_name]
    return run_zfs_command(dbg, cmd).strip()


def pool_is_imported(dbg, pool_name):
    """Check if a ZFS pool is currently imported. Returns True/False."""
    cmd = "zpool list -H -o name".split() + [pool_name]
    try:
        run_zfs_command(dbg, cmd)
        return True
    except xapi.InternalError:
        return False


def pool_exists(dbg, pool_name):
    """Check if a ZFS pool exists (imported or importable). Returns True/False."""
    # Check imported pools first
    if pool_is_imported(dbg, pool_name):
        return True
    # Pool may be exported but present on disk -- check importable pools
    cmd = "zpool import".split()
    try:
        output = run_zfs_command(dbg, cmd, error=False)
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        for line in output.strip().splitlines():
            line = line.strip()
            if line.startswith("pool:") and line.split(":", 1)[1].strip() == pool_name:
                return True
    except xapi.InternalError:
        pass
    return False


def create_pool(dbg, pool_name, vdev_defn):
    """Create a new ZFS pool.

    Note: We don't use -R altroot here because SR.create will set
    the mountpoint property explicitly, which persists in ZFS metadata.
    """
    cmd = ["zpool", "create", pool_name] + vdev_defn
    run_zfs_command(dbg, cmd)


def import_pool(dbg, pool_name):
    """Import a ZFS pool.

    Note: We don't use -R altroot because the mountpoint property
    is stored in ZFS metadata and will be used automatically.
    """
    cmd = ["zpool", "import", pool_name]
    run_zfs_command(dbg, cmd)


def export_pool(dbg, pool_name):
    cmd = "zpool export".split() + [pool_name]
    run_zfs_command(dbg, cmd)


###
# Dataset mountpoint management
# ZFS stores mountpoint in metadata, auto-mounts via zfs-mount service on boot


def dataset_exists(dbg, dataset_name):
    """Check if a ZFS dataset exists. Returns True/False."""
    cmd = ["zfs", "list", "-H", "-o", "name", dataset_name]
    try:
        run_zfs_command(dbg, cmd)
        return True
    except xapi.InternalError:
        return False


def dataset_create(dbg, dataset_name, create_parents=True):
    """Create a ZFS dataset (filesystem, not volume).

    Args:
        dataset_name: Full dataset path (e.g., 'zpool/vms/prod')
        create_parents: If True, create parent datasets with mountpoint=none

    The dataset will be created with default properties.
    Mountpoint should be set separately via dataset_set_mountpoint().
    """
    if create_parents:
        # Create any missing parent datasets with mountpoint=none
        # so they don't clutter the filesystem
        for parent in dataset_get_parents(dataset_name):
            if not dataset_exists(dbg, parent):
                log.debug(
                    "%s: creating parent dataset %s with mountpoint=none", dbg, parent
                )
                cmd = ["zfs", "create", "-o", "mountpoint=none", parent]
                run_zfs_command(dbg, cmd)

    cmd = ["zfs", "create", dataset_name]
    run_zfs_command(dbg, cmd)


def _lzc_recursive_destroy(dbg, root, lzc_module):
    """Python-side tree walk for recursive destroy (#246 phase 3).

    libzfs_core has no recursive-destroy primitive -- `zfs destroy
    -r` is implemented in libzfs.so as a tree walk over
    `zfs_iter_*`. Reproduce that walk in Python: enumerate every
    descendant once via `zfs list -r -t all -o name,type` (read
    path, doesn't retry on busy), then reverse the order and
    `lzc.destroy_with_retry` each node. Reverse order means
    snapshots -> leaf datasets/zvols -> ancestors, which is the
    dependency-correct destroy sequence matching `zfs destroy
    -r`'s semantics.

    Filesystem nodes are unmounted before destroy. The libzfs CLI
    wraps unmount inside its destroy code path, but lzc operates
    below the mount layer -- `lzc_destroy` on a still-mounted
    filesystem returns EBUSY. We replicate the libzfs unmount
    step explicitly. Volumes (zvols) and snapshots don't have
    mount state and skip this step.

    Tolerates ENOENT mid-walk (a node may have been destroyed
    transitively if the kernel coalesced operations). Any other
    non-zero errno raises -- same dependency semantics as `zfs
    destroy -r` (errors on clones referencing snapshots being
    destroyed)."""
    cmd = ["zfs", "list", "-r", "-H", "-t", "all", "-o", "name,type", root]
    out = run_zfs_command(dbg, cmd)
    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    nodes = []
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 2:
            nodes.append((parts[0], parts[1]))
    log.debug("%s: lzc recursive destroy of %s: %d nodes", dbg, root, len(nodes))
    for name, node_type in reversed(nodes):
        # Filesystems need unmount before lzc_destroy -- see
        # docstring above. Best-effort: if already unmounted
        # the call no-ops (or returns harmless error which we
        # swallow); if a real mount holds open files, surface
        # the failure.
        if node_type == "filesystem":
            try:
                if dataset_is_mounted(dbg, name):
                    log.debug("%s: unmounting %s before lzc destroy", dbg, name)
                    dataset_unmount(dbg, name)
            except Exception as exc:  # pylint: disable=broad-except
                # Surface clearly: an unmount failure here means
                # destroy will return EBUSY and the operator
                # needs to know which mount is stuck.
                raise xapi.InternalError(
                    "lzc recursive destroy of {} cannot proceed: "
                    "unmount of {} failed: {}".format(root, name, exc)
                )
        rc = lzc_module.destroy_with_retry(name)
        if rc in (0, lzc_module.ENOENT):
            continue
        raise xapi.InternalError(
            "lzc recursive destroy of {} failed at {}: "
            "errno {} ({})".format(root, name, rc, lzc_module.errno_to_str(rc))
        )


def dataset_destroy(dbg, dataset_name, recursive=True):
    """Destroy a ZFS dataset and optionally all its children.

    Args:
        dataset_name: Full dataset path (e.g., 'zpool/sr-data')
        recursive: If True, also destroy all child datasets/volumes

    libzfs_core migration (#246):

    - `recursive=False` (phase 1): single `lzc.destroy_with_retry`
      call. Structured EBUSY signal, no stderr substring match.
    - `recursive=True` (phase 3): Python-side tree walk via
      `_lzc_recursive_destroy` -- enumerate descendants once via
      `zfs list -r -t all` (read path), then per-node
      `lzc.destroy_with_retry` in reverse order. Same dependency
      semantics as `zfs destroy -r`.

    Falls back to CLI on hosts where `libzfs_core.so` isn't
    loadable. See `lzc.available()` and lzc-migration
    for the broader migration plan.
    """
    try:
        from . import lzc  # pylint: disable=import-outside-toplevel
    except (ImportError, ValueError):
        import lzc  # noqa: F401  pylint: disable=import-error,import-outside-toplevel
    if lzc.available():
        if recursive:
            log.debug("%s: dataset_destroy(%s, recursive) via lzc", dbg, dataset_name)
            _lzc_recursive_destroy(dbg, dataset_name, lzc)
            return
        log.debug("%s: dataset_destroy(%s) via lzc", dbg, dataset_name)
        rc = lzc.destroy_with_retry(dataset_name)
        if rc == 0:
            return
        raise xapi.InternalError(
            "lzc_destroy({}) failed: errno {} ({})".format(
                dataset_name, rc, lzc.errno_to_str(rc)
            )
        )
    # libzfs_core unavailable -- fall through to the CLI path.
    cmd = ["zfs", "destroy"]
    if recursive:
        cmd.append("-r")
    cmd.append(dataset_name)
    run_zfs_command_with_retry(dbg, cmd)


def dataset_set_mountpoint(dbg, dataset_name, mount_path):
    """Set the mountpoint property for a ZFS dataset.

    This is stored in ZFS metadata and persists across reboots.
    The zfs-mount service will auto-mount to this path on boot.
    """
    cmd = ["zfs", "set", "mountpoint={}".format(mount_path), dataset_name]
    run_zfs_command(dbg, cmd)


def dataset_mount(dbg, dataset_name):
    """Mount a ZFS dataset to its configured mountpoint."""
    cmd = ["zfs", "mount", dataset_name]
    run_zfs_command(dbg, cmd)


def dataset_unmount(dbg, dataset_name):
    """Unmount a ZFS dataset."""
    cmd = ["zfs", "unmount", dataset_name]
    run_zfs_command(dbg, cmd)


def dataset_is_mounted(dbg, dataset_name):
    """Check if a ZFS dataset is currently mounted.

    Returns True if mounted, False otherwise.

    `run_zfs_command()` returns bytes on Python 3 (subprocess.Popen output);
    decode before comparing or the bytes-vs-str comparison is
    always False -- same class of latent bug #213 caught in the
    busy-retry path. Surfaced during #246 phase 3 lab smoke when
    the recursive walk's pre-destroy unmount step was a no-op
    against a confirmed-mounted tree.
    """
    cmd = ["zfs", "get", "-H", "-o", "value", "mounted", dataset_name]
    try:
        result = run_zfs_command(dbg, cmd)
        if isinstance(result, bytes):
            result = result.decode("utf-8", errors="replace")
        return result.strip() == "yes"
    except xapi.InternalError:
        return False


def sr_mount_path(sr_uuid):
    """Get the standard SR mount path for a given SR UUID."""
    return "{}/{}".format(ZFS_LIVE_MOUNT_PREFIX, sr_uuid)


def dataset_path(pool_name, dataset_name=None):
    """Get the full dataset path.

    Args:
        pool_name: The ZFS pool name
        dataset_name: Optional dataset name within the pool

    Returns:
        Full dataset path (e.g., 'zpool/sr-data' or just 'zpool' if no dataset)
    """
    if dataset_name:
        return "{}/{}".format(pool_name, dataset_name)
    return pool_name


def dataset_get_used(dbg, dataset_name):
    """Get the used space of a dataset in bytes."""
    cmd = ["zfs", "get", "-Hp", "-o", "value", "used", dataset_name]
    return int(run_zfs_command(dbg, cmd))


def dataset_get_available(dbg, dataset_name):
    """Get the available space for a dataset in bytes.

    For datasets without quota, this is the pool's free space.
    For datasets with quota, this is min(quota - used, pool free).
    """
    cmd = ["zfs", "get", "-Hp", "-o", "value", "available", dataset_name]
    return int(run_zfs_command(dbg, cmd))


def dataset_get_quota(dbg, dataset_name):
    """Get the quota of a dataset in bytes, or 0 if no quota set."""
    cmd = ["zfs", "get", "-Hp", "-o", "value", "quota", dataset_name]
    result = run_zfs_command(dbg, cmd).strip()
    # ZFS returns '0' for no quota, or 'none' in some versions
    if result in ("0", "none", b"0", b"none"):
        return 0
    return int(result)


def dataset_get_refquota(dbg, dataset_name):
    """Get the refquota of a dataset in bytes, or 0 if no refquota set.

    refquota limits the dataset itself, not including snapshots/children.
    """
    cmd = ["zfs", "get", "-Hp", "-o", "value", "refquota", dataset_name]
    result = run_zfs_command(dbg, cmd).strip()
    if result in ("0", "none", b"0", b"none"):
        return 0
    return int(result)


# SR marker property - used to identify datasets that are SRs
SR_PROPERTY = "xcp:sr"


def dataset_set_sr_marker(dbg, dataset_name, sr_uuid):
    """Mark a dataset as an SR by setting a ZFS user property."""
    cmd = ["zfs", "set", "{}={}".format(SR_PROPERTY, sr_uuid), dataset_name]
    run_zfs_command(dbg, cmd)


def dataset_get_sr_marker(dbg, dataset_name):
    """Get the SR marker from a dataset, or None if not an SR."""
    cmd = ["zfs", "get", "-Hp", "-o", "value", SR_PROPERTY, dataset_name]
    try:
        result = run_zfs_command(dbg, cmd).strip()
        if isinstance(result, bytes):
            result = result.decode("utf-8", errors="replace")
        if result and result != "-":
            return result
    except xapi.InternalError:
        pass
    return None


def dataset_is_sr(dbg, dataset_name):
    """Check if a dataset is marked as an SR."""
    return dataset_get_sr_marker(dbg, dataset_name) is not None


def dataset_list_children(dbg, dataset_name):
    """List all child datasets (recursive) of a dataset."""
    cmd = ["zfs", "list", "-Hr", "-o", "name", "-t", "filesystem", dataset_name]
    try:
        output = run_zfs_command(dbg, cmd).strip()
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        # First line is the dataset itself, rest are children
        lines = output.splitlines()
        return lines[1:] if len(lines) > 1 else []
    except xapi.InternalError:
        return []


def dataset_list_zvols(dbg, dataset_name):
    """List all zvols directly under a dataset (non-recursive).

    Returns a list of zvol names (e.g., ['pool/sr/vdi-uuid1', ...]).
    """
    cmd = ["zfs", "list", "-Hd1", "-t", "volume", "-o", "name", dataset_name]
    try:
        output = run_zfs_command(dbg, cmd)
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return [line for line in output.strip().splitlines() if line]
    except xapi.InternalError:
        return []


def dataset_list_snapshots(dbg, dataset_name):
    """List snapshots of zvols directly under a dataset (depth-2).

    Returns names like 'pool/sr/<vdi-uuid>@<snap-uuid>'. Depth 2 reaches
    snapshots of the immediate zvol children but not snapshots of any
    deeper datasets (zfs-live keeps zvols flat under the SR dataset).
    """
    cmd = ["zfs", "list", "-Hd2", "-t", "snapshot", "-o", "name", dataset_name]
    try:
        output = run_zfs_command(dbg, cmd)
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return [line for line in output.strip().splitlines() if line]
    except xapi.InternalError:
        return []


def dataset_get_parents(dataset_name):
    """Get list of parent dataset paths (not including pool root).

    E.g., 'zpool/a/b/c' returns ['zpool/a', 'zpool/a/b']
    """
    parts = dataset_name.split("/")
    parents = []
    for i in range(2, len(parts)):  # Start at 2 to skip pool root
        parents.append("/".join(parts[:i]))
    return parents


def dataset_find_child_srs(
    dbg, pool_name, dataset_path
):  # pylint: disable=redefined-outer-name
    """Find any existing SR datasets that would be children of the given path.

    This is used to prevent creating an SR that would contain other SRs.
    Scans all datasets in the pool to find any marked as SR that start with
    the given path prefix.

    Args:
        pool_name: The ZFS pool name
        dataset_path: The full dataset path to check (e.g., 'zpool/vms')

    Returns:
        List of child dataset paths that are SRs
    """
    prefix = dataset_path + "/"
    child_srs = []

    # List all datasets in the pool
    cmd = ["zfs", "list", "-Hr", "-o", "name", "-t", "filesystem", pool_name]
    try:
        output = run_zfs_command(dbg, cmd).strip()
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")

        for ds in output.splitlines():
            # Check if this dataset would be a child of our target
            if ds.startswith(prefix):
                if dataset_is_sr(dbg, ds):
                    child_srs.append(ds)
    except xapi.InternalError:
        pass

    return child_srs


def destroy_pool(dbg, pool_name):
    cmd = "zpool destroy".split() + [pool_name]
    run_zfs_command_with_retry(dbg, cmd)


def pool_get_ashift(dbg, pool_name):
    """Get the pool's ashift value (minimum sector size exponent).

    ashift=9 -> 512 bytes, ashift=12 -> 4K, ashift=13 -> 8K.
    The minimum volblocksize is 1 << ashift.
    """
    cmd = ["zpool", "get", "-Hp", "-o", "value", "ashift", pool_name]
    return int(run_zfs_command(dbg, cmd))


def get_pool_total_bytes(dbg, sr_path):
    # size is returned in bytes
    cmd = "zpool get -Hp -o value size".split() + [sr_path]
    return int(run_zfs_command(dbg, cmd))


def get_pool_free_bytes(dbg, sr_path):
    # size is returned in bytes
    cmd = "zpool get -Hp -o value free".split() + [sr_path]
    return int(run_zfs_command(dbg, cmd))


###


def get_zvol_used_bytes(dbg, vol_name):
    # size is returned in bytes
    cmd = "zfs get -Hp -o value used".split() + [vol_name]
    return int(run_zfs_command(dbg, cmd))


def get_zvol_size_bytes(dbg, vol_name):
    # size is returned in bytes
    cmd = "zfs get -Hp -o value volsize".split() + [vol_name]
    return int(run_zfs_command(dbg, cmd))


def create_zvol(
    dbg,
    zvol_path,
    size_bytes,
    thin=True,
    volblocksize=8192,
    copies=None,
    compression=None,
    sync=None,
    primarycache=None,
    logbias=None,
):
    """Create a ZFS zvol with configurable properties.

    Args:
        size_bytes: Volume size in bytes -- passed straight to
            `zfs create -V`. Callers (volume.create) pass xapi's
            `virtual_size` which is already in bytes; the previous
            parameter name `size_mib` was misleading (#213 finding 4).
        thin: If True, create sparse zvol (-s flag).
              If False, create thick zvol with refreservation.
        volblocksize: Block size in bytes (4096-131072). IMMUTABLE after creation.
        copies: Number of data copies (1-3), or None to inherit from parent.
        compression: Compression algorithm, or None to inherit from parent.
        sync: Sync behavior, or None to inherit from parent.
        primarycache: ARC caching, or None to inherit from parent.
        logbias: ZIL bias, or None to inherit from parent.
    """
    cmd = ["zfs", "create"]

    # Sparse flag for thin provisioning
    if thin:
        cmd.append("-s")

    # volblocksize (must be set at creation, cannot change later)
    cmd.extend(["-o", "volblocksize={}".format(volblocksize)])

    # Optional per-zvol property overrides (None = inherit from parent dataset)
    if copies is not None:
        cmd.extend(["-o", "copies={}".format(copies)])
    if compression is not None:
        cmd.extend(["-o", "compression={}".format(compression)])
    if sync is not None:
        cmd.extend(["-o", "sync={}".format(sync)])
    if primarycache is not None:
        cmd.extend(["-o", "primarycache={}".format(primarycache)])
    if logbias is not None:
        cmd.extend(["-o", "logbias={}".format(logbias)])

    # Size and path
    cmd.extend(["-V", str(size_bytes)])
    cmd.append(zvol_path)

    # Phase 6 of #246: when the caller passes no extra property
    # overrides AND wants thin provisioning (no separate
    # refreservation set step), the zvol can be created via the
    # structured-signal `lzc_create` path. The current bindings
    # (phase 6) only support volsize + volblocksize as uint64
    # properties; extra string properties (compression / sync /
    # etc.) stay on the CLI path because they're PROP_TYPE_INDEX
    # in OpenZFS and the kernel rejects DATA_TYPE_STRING for
    # those -- a project-side property-name -> uint64-enum table
    # is needed before the lzc path can carry them. Tracked as
    # follow-up scope on #254. Thick provisioning needs a
    # separate `zfs set refreservation` after create -- also CLI
    # because `lzc_set_props` doesn't exist as an upstream
    # libzfs_core symbol (property mutation lives in libzfs.so).
    has_extra_props = any(
        p is not None for p in (copies, compression, sync, primarycache, logbias)
    )
    if thin and not has_extra_props:
        try:
            from . import lzc  # pylint: disable=import-outside-toplevel
        except (ImportError, ValueError):
            import lzc  # noqa: F401  pylint: disable=import-error,import-outside-toplevel
        if lzc.create_zvol_available():
            log.debug("%s: create_zvol(%s) via lzc", dbg, zvol_path)
            rc = lzc.create_zvol_with_retry(zvol_path, size_bytes, volblocksize)
            if rc == 0:
                return
            raise xapi.InternalError(
                "lzc_create({}) failed: errno {} ({})".format(
                    zvol_path, rc, lzc.errno_to_str(rc)
                )
            )

    run_zfs_command_with_retry(dbg, cmd)

    # For thick provisioning, set refreservation to guarantee space
    # Account for copies multiplier -- if copies was not explicitly passed,
    # the zvol inherits from the parent dataset, so query the effective value
    if not thin:
        if copies is not None:
            copies_val = int(copies)
        else:
            # Read the effective (possibly inherited) copies value from ZFS
            effective = vol_get_property(dbg, zvol_path, "copies")
            copies_val = int(effective)
        reservation = int(size_bytes) * copies_val
        run_zfs_command_with_retry(
            dbg, ["zfs", "set", "refreservation={}".format(reservation), zvol_path]
        )


def destroy_zvol(dbg, zvol_path):
    """Destroy a zvol. Phase-2 of the libzfs_core migration
    (#246) -- zvols and datasets are identical at the ZFS ioctl
    layer, so this routes to the same `lzc.destroy_one` path as
    `dataset_destroy(recursive=False)` from phase 1. Falls back
    to CLI when lzc isn't available."""
    try:
        from . import lzc  # pylint: disable=import-outside-toplevel
    except (ImportError, ValueError):
        import lzc  # noqa: F401  pylint: disable=import-error,import-outside-toplevel
    if lzc.available():
        log.debug("%s: destroy_zvol(%s) via lzc", dbg, zvol_path)
        rc = lzc.destroy_with_retry(zvol_path)
        if rc == 0:
            return
        raise xapi.InternalError(
            "lzc_destroy({}) failed: errno {} ({})".format(
                zvol_path, rc, lzc.errno_to_str(rc)
            )
        )
    cmd = "zfs destroy".split() + [zvol_path]
    run_zfs_command_with_retry(dbg, cmd)


def promote_clone(dbg, zvol_path):
    cmd = "zfs promote".split() + [zvol_path]
    run_zfs_command_with_retry(dbg, cmd)


def resize_zvol(dbg, vol_path, new_size):
    cmd = "zfs set".split() + ["volsize={}".format(new_size), vol_path]
    run_zfs_command_with_retry(dbg, cmd)


def take_snapshot(dbg, snap_name):
    """Take a snapshot of a zvol or dataset. Phase 4 of #246 --
    the first nvlist-requiring lzc primitive in this driver.
    See nvlist-and-fnvlist for why we use libnvpair's
    `fnvlist_*` family for the snap-names argument.

    When lzc + nvlist support is available, calls
    `lzc.snapshot_with_retry()` and consumes the kernel errno
    directly. Otherwise falls back to the CLI path."""
    try:
        from . import lzc  # pylint: disable=import-outside-toplevel
    except (ImportError, ValueError):
        import lzc  # noqa: F401  pylint: disable=import-error,import-outside-toplevel
    if lzc.nvlist_available():
        log.debug("%s: take_snapshot(%s) via lzc", dbg, snap_name)
        rc = lzc.snapshot_with_retry(snap_name)
        if rc == 0:
            return
        raise xapi.InternalError(
            "lzc_snapshot({}) failed: errno {} ({})".format(
                snap_name, rc, lzc.errno_to_str(rc)
            )
        )
    cmd = "zfs snapshot".split() + [snap_name]
    run_zfs_command_with_retry(dbg, cmd)


def clone_snapshot(dbg, snap_name, clone_name):
    """Clone a snapshot. Phase 5 of #246. Matches the existing
    CLI form `zfs clone <snap> <clone>` (no property overrides)
    so we pass NULL for the lzc props nvlist; a future PR can
    add `fnvlist_add_string`/`add_uint64` bindings if
    property-at-clone-time becomes a need.

    When lzc + extended-tier symbols + libnvpair are all
    available, calls `lzc.clone_with_retry()` and consumes the
    kernel errno directly. Otherwise falls back to CLI."""
    try:
        from . import lzc  # pylint: disable=import-outside-toplevel
    except (ImportError, ValueError):
        import lzc  # noqa: F401  pylint: disable=import-error,import-outside-toplevel
    if lzc.clone_available():
        log.debug("%s: clone_snapshot(%s -> %s) via lzc", dbg, snap_name, clone_name)
        rc = lzc.clone_with_retry(clone_name, snap_name)
        if rc == 0:
            return
        raise xapi.InternalError(
            "lzc_clone({} from {}) failed: errno {} ({})".format(
                clone_name, snap_name, rc, lzc.errno_to_str(rc)
            )
        )
    cmd = "zfs clone".split() + [snap_name, clone_name]
    run_zfs_command_with_retry(dbg, cmd)


def vol_send_receive(dbg, src_snap, dest_vol):
    """Stream `src_snap` to `dest_vol` via `zfs send -c | zfs receive`.

    Uses subprocess.Popen pipeline so the kernel pipes blocks directly
    without a temporary file. The `-c` flag ships the on-wire
    compressed form when the source dataset is compressed (sparseness
    preserved for free).

    Raises CalledProcessError on either side's non-zero exit. Caller
    is responsible for cleaning up `dest_vol` on failure.

    Used by Volume.copy's cross-pool path. The disk-staging variants
    `vol_send`/`vol_receive` (file-based) live below and are reserved
    for zfs-migrate-style operator workflows where an
    intermediate file is wanted for resumability."""
    send_cmd = ["zfs", "send", "-c", src_snap]
    recv_cmd = ["zfs", "receive", dest_vol]
    log.debug("%s: Running pipeline %s | %s", dbg, send_cmd, recv_cmd)
    send = subprocess.Popen(  # pylint: disable=consider-using-with
        send_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True
    )
    try:
        recv = subprocess.Popen(  # pylint: disable=consider-using-with
            recv_cmd, stdin=send.stdout, stderr=subprocess.PIPE, close_fds=True
        )
        # Close our copy of the pipe so SIGPIPE reaches send if recv
        # dies first; otherwise send blocks forever on a full pipe.
        send.stdout.close()
        _, recv_err = recv.communicate()
        _, send_err = send.communicate()
    except BaseException:
        # KeyboardInterrupt mid-transfer is the realistic case.
        send.kill()
        try:
            recv.kill()
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        raise
    if send.returncode != 0:
        raise subprocess.CalledProcessError(
            send.returncode, send_cmd, stderr=send_err.decode("utf-8", errors="replace")
        )
    if recv.returncode != 0:
        raise subprocess.CalledProcessError(
            recv.returncode, recv_cmd, stderr=recv_err.decode("utf-8", errors="replace")
        )


###
# ZFS property management


def dataset_set_properties(dbg, dataset_name, properties):
    """Set multiple ZFS properties on a dataset.

    Args:
        properties: dict of {property_name: value} pairs.
                    None values are skipped (inherit from parent).
    """
    for prop, value in properties.items():
        if value is not None:
            cmd = ["zfs", "set", "{}={}".format(prop, value), dataset_name]
            run_zfs_command(dbg, cmd)


def vol_get_property(dbg, vol_name, prop):
    """Get a single ZFS property value from a zvol or dataset."""
    cmd = ["zfs", "get", "-Hp", "-o", "value", prop, vol_name]
    result = run_zfs_command(dbg, cmd)
    if isinstance(result, bytes):
        result = result.decode("utf-8", errors="replace")
    return result.strip()


def vol_get_property_with_source(dbg, vol_name, prop):
    """Get a ZFS property value and its source (local, inherited, default).

    Returns:
        tuple: (value, source) where source is 'local', 'inherited', 'default', etc.
    """
    cmd = ["zfs", "get", "-H", "-o", "value,source", prop, vol_name]
    result = run_zfs_command(dbg, cmd)
    if isinstance(result, bytes):
        result = result.decode("utf-8", errors="replace")
    parts = result.strip().split("\t")
    value = parts[0]
    source = parts[1] if len(parts) > 1 else "unknown"
    # Normalize source (e.g., "inherited from zpool/foo" -> "inherited")
    if source.startswith("inherited"):
        source = "inherited"
    return (value, source)


def vol_set_property(dbg, vol_name, prop, value):
    """Set a single ZFS property on a zvol.

    Stays on the CLI path: `lzc_set_props` is not exported by
    upstream libzfs_core (verified against OpenZFS 2.1.x /
    2.2.x / master headers -- property mutation lives in
    `libzfs.so` via `zfs_prop_set()`, not libzfs_core). Phase
    7's original design assumed that symbol existed; #254
    re-scopes to the libzfs-bindings work needed to migrate
    this path properly."""
    cmd = ["zfs", "set", "{}={}".format(prop, value), vol_name]
    run_zfs_command_with_retry(dbg, cmd)


def vol_inherit_property(dbg, vol_name, prop):
    """Reset a ZFS property on a zvol to inherit from parent."""
    cmd = ["zfs", "inherit", prop, vol_name]
    run_zfs_command_with_retry(dbg, cmd)


def vol_get_refreservation(dbg, vol_name):
    """Get the refreservation of a zvol in bytes, or 0 if none set."""
    result = vol_get_property(dbg, vol_name, "refreservation")
    if result in ("0", "none", "-", ""):
        return 0
    return int(result)


def vol_sync_thick_refreservation(dbg, vol_name):
    """Sync refreservation with volsize * copies for thick-provisioned zvols.

    If the zvol has refreservation > 0 (thick provisioning), recalculates
    and sets refreservation = volsize * effective_copies to keep the space
    guarantee accurate after resize or copies change.

    No-op for thin-provisioned zvols (refreservation == 0).
    """
    refreservation = vol_get_refreservation(dbg, vol_name)
    if refreservation > 0:
        volsize = get_zvol_size_bytes(dbg, vol_name)
        copies = int(vol_get_property(dbg, vol_name, "copies"))
        new_reservation = volsize * copies
        if new_reservation != refreservation:
            log.debug(
                "%s: syncing refreservation on %s: %d -> %d " "(volsize=%d, copies=%d)",
                dbg,
                vol_name,
                refreservation,
                new_reservation,
                volsize,
                copies,
            )
            run_zfs_command_with_retry(
                dbg,
                ["zfs", "set", "refreservation={}".format(new_reservation), vol_name],
            )


def vol_set_thick_refreservation(dbg, vol_name):
    """Set refreservation on a zvol for thick provisioning.

    Sets refreservation = volsize * effective_copies unconditionally.
    Used when a thin zvol must become thick (e.g., cloning from a thick source).
    """
    volsize = get_zvol_size_bytes(dbg, vol_name)
    copies = int(vol_get_property(dbg, vol_name, "copies"))
    reservation = volsize * copies
    log.debug(
        "%s: setting thick refreservation on %s: %d " "(volsize=%d, copies=%d)",
        dbg,
        vol_name,
        reservation,
        volsize,
        copies,
    )
    run_zfs_command_with_retry(
        dbg, ["zfs", "set", "refreservation={}".format(reservation), vol_name]
    )


def vol_get_properties_batch(dbg, vol_name, properties):
    """Get multiple ZFS properties with sources in a single zfs get call.

    Much more efficient than calling vol_get_property_with_source() in a loop
    when listing many VDIs (1 subprocess call vs N).

    Args:
        properties: list of property names to query

    Returns:
        dict of {prop: (value, source)} pairs. Missing properties are omitted.
    """
    props_str = ",".join(properties)
    cmd = ["zfs", "get", "-Hp", "-o", "property,value,source", props_str, vol_name]
    result = run_zfs_command(dbg, cmd)
    if isinstance(result, bytes):
        result = result.decode("utf-8", errors="replace")

    props = {}
    for line in result.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            prop, value, source = parts[0], parts[1], parts[2]
            # Normalize source (e.g., "inherited from zpool/foo" -> "inherited")
            if source.startswith("inherited"):
                source = "inherited"
            props[prop] = (value, source)
    return props


def format_volblocksize(bytes_val):
    """Format volblocksize for display: 8192 -> '8K', 65536 -> '64K'."""
    val = int(bytes_val)
    if val >= 1024:
        return "{}K".format(val // 1024)
    return str(val)


def vol_get_zfs_properties_dict(dbg, vol_name):
    """Read the ZFS-property surface a zvol exposes to xapi
    (provisioning, volblocksize, mutable properties + sources) and
    return as a flat dict suitable for `VDI.stat` / `SR.ls` payloads.

    One batched `zfs get` call. Failures are swallowed and an empty
    dict returned -- callers degrade to library-level metadata if
    ZFS isn't reachable for some reason.

    Single source of truth for the property list: extending
    MUTABLE_ZFS_PROPERTIES extends what this returns. Pre-#217 this
    function lived as `_get_zfs_properties_for_ls` in sr.py and
    `_get_zfs_properties` in volume.py -- byte-identical except for
    log-prefix.
    """
    keys = {}
    try:
        batch_props = ["refreservation", "volblocksize"] + sorted(
            MUTABLE_ZFS_PROPERTIES
        )
        props = vol_get_properties_batch(dbg, vol_name, batch_props)

        # Provisioning type from refreservation
        if "refreservation" in props:
            refres_val = props["refreservation"][0]
            if refres_val in ("0", "none", "-", ""):
                keys["provisioning"] = "thin"
            else:
                try:
                    keys["provisioning"] = "thick" if int(refres_val) > 0 else "thin"
                except ValueError:
                    keys["provisioning"] = "thin"

        # volblocksize (immutable, displayed as '8K' / '64K')
        if "volblocksize" in props:
            keys["volblocksize"] = format_volblocksize(props["volblocksize"][0])

        # Mutable properties + their inherit/local source.
        for prop in MUTABLE_ZFS_PROPERTIES:
            if prop in props:
                value, source = props[prop]
                keys[prop] = value
                keys[prop + "_source"] = source

    except Exception as e:  # pylint: disable=broad-exception-caught
        log.debug(
            "%s: vol_get_zfs_properties_dict: failed for %s: %s", dbg, vol_name, e
        )
    return keys


###


def vol_send(dbg, zvol_path, output_file, base_snap=None):
    """Send a zvol or snapshot to a file. If base_snap is provided,
    sends an incremental stream from base_snap to zvol_path."""
    if base_snap:
        cmd = ["zfs", "send", "-i", base_snap, zvol_path]
    else:
        cmd = ["zfs", "send", zvol_path]
    with open(output_file, "wb") as f:
        log.debug("%s: Running cmd %s > %s", dbg, cmd, output_file)
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            cmd, stdout=f, stderr=subprocess.PIPE, close_fds=True
        )
        _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise xapi.InternalError(
                "zfs send failed: {}".format(stderr.decode("utf-8", errors="replace"))
            )


def vol_receive(dbg, pool_name, input_file):
    """Receive a ZFS stream from a file into a pool."""
    cmd = ["zfs", "receive", "-F", pool_name]
    with open(input_file, "rb") as f:
        log.debug("%s: Running cmd %s < %s", dbg, cmd, input_file)
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            cmd, stdin=f, stderr=subprocess.PIPE, close_fds=True
        )
        _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise xapi.InternalError(
                "zfs receive failed: {}".format(
                    stderr.decode("utf-8", errors="replace")
                )
            )


def vol_copy_raw(dbg, src_zvol_path, dest_file, block_size=1048576):
    """Copy a zvol's raw block device to a file.
    src_zvol_path should be pool/vol_id format."""
    src_dev = "/dev/zvol/{}".format(src_zvol_path)
    cmd = [
        "dd",
        "if={}".format(src_dev),
        "of={}".format(dest_file),
        "bs={}".format(block_size),
        "conv=sparse",
    ]
    log.debug("%s: Running cmd %s", dbg, cmd)
    proc = subprocess.Popen(  # pylint: disable=consider-using-with
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True
    )
    _, stderr = proc.communicate()
    if proc.returncode != 0:
        raise xapi.InternalError(
            "dd copy failed: {}".format(stderr.decode("utf-8", errors="replace"))
        )


###

# Shared between SR.ls and Volume operations -- recovers VDI
# metadata when the metabase vsize column is NULL after a crash
# during resize.


def recover_vdi_vsize(dbg, vdi, db, pool_name):
    """Recover VDI virtual size from ZFS after an interrupted resize.

    A crash during Volume.resize leaves vsize as None in the metabase
    (the column is NULLed before the ZFS resize, then updated after).
    This function queries the actual zvol size from ZFS and patches
    both the in-memory object and the database row so the VDI is
    usable again without operator intervention.
    """
    if vdi.volume.vsize is None:
        vol_name = format_zvol_name(pool_name, vdi.uuid)
        vdi.volume.vsize = get_zvol_size_bytes(dbg, vol_name)
        db.update_volume_vsize(vdi.volume.id, vdi.volume.vsize)


def _zvol_device_is_open(zvol_path):
    """Check if the zvol block device is open by any process.

    Uses fuser(1) to test whether any process holds a file descriptor
    on the block device.  Returns True if the device is in use.
    """
    dev_path = "/dev/zvol/" + zvol_path
    try:
        devnull = open(
            os.devnull, "w"
        )  # pylint: disable=unspecified-encoding,consider-using-with
        try:
            subprocess.check_call(
                ["fuser", "-s", dev_path], stdout=devnull, stderr=devnull
            )
            return True  # exit 0 = device in use
        finally:
            devnull.close()
    except subprocess.CalledProcessError:
        return False  # exit non-zero = not in use
    except OSError:
        return False  # fuser not available, assume not in use


def _zvol_has_active_datapath(zvol_path):
    """Check if a datapath daemon is active for this zvol.

    The raw-qdisk datapath stores per-VDI metadata in a pickle file
    keyed by SHA-256(device_path)[:16].  If the pickle exists and the
    recorded PID is still alive, the zvol is actively served.
    """
    dev_path = "/dev/zvol/" + zvol_path
    key = hashlib.sha256(dev_path.encode("utf-8")).hexdigest()[:16]
    meta_dir = "/var/run/nonpersistent/dp-raw-qdisk/" + key
    meta_file = os.path.join(meta_dir, "qemu-dp-raw.pickle")
    if not os.path.exists(meta_file):
        return False
    try:
        with open(meta_file, "rb") as f:
            data = pickle.load(f)
        pid = data.get("pid")
        if pid:
            os.kill(pid, 0)  # check if alive (signal 0)
            return True
    except (OSError, EOFError, pickle.UnpicklingError, KeyError):
        pass
    return False


def zvol_find_orphans(dbg, sr_dataset, known_vdi_uuids):
    """Find zvols under sr_dataset with no DB entry and not in-flight.

    Checks concrete signals (block device open, datapath metadata)
    to distinguish true orphans from in-flight operations such as
    running VMs, active migrations, or in-progress copies.

    Args:
        dbg: Debug context string.
        sr_dataset: Full ZFS dataset path (e.g. 'pool/sr-uuid').
        known_vdi_uuids: set of VDI UUID strings present in the database.

    Returns:
        List of (zvol_name, volsize, creation) tuples for orphan zvols.
        volsize and creation are string values from ``zfs get``.
    """
    orphans = []
    all_zvols = dataset_list_zvols(dbg, sr_dataset)

    for zvol_name in all_zvols:
        uuid = zvol_name.split("/")[-1]
        if uuid in known_vdi_uuids:
            continue

        # In-flight checks -- skip if anything indicates active use
        if _zvol_device_is_open(zvol_name):
            log.debug(
                "%s: zvol_find_orphans: %s has open device, skipping", dbg, zvol_name
            )
            continue
        if _zvol_has_active_datapath(zvol_name):
            log.debug(
                "%s: zvol_find_orphans: %s has active datapath, " "skipping",
                dbg,
                zvol_name,
            )
            continue

        # Confirmed orphan -- gather metadata for reporting
        try:
            props = vol_get_properties_batch(dbg, zvol_name, ["volsize", "creation"])
            volsize = props.get("volsize", ("?",))[0]
            creation = props.get("creation", ("?",))[0]
        except Exception:  # pylint: disable=broad-exception-caught
            volsize = "?"
            creation = "?"

        orphans.append((zvol_name, volsize, creation))

    return orphans


def zsnap_find_orphans(dbg, sr_dataset, known_vdi_uuids):
    """Find ZFS snapshots under sr_dataset whose snap_uuid is not in
    the metabase and whose parent zvol is not actively in use.

    Snapshots in zfs-live are named ``<sr_dataset>/<vdi-uuid>@<snap-uuid>``
    and tracked by ``snap_uuid`` (a row in the metabase with
    ``volume.snap = 1``).  An orphan is a snapshot whose ``snap_uuid``
    has no metabase row -- typically the result of a crash between
    ``zfs snapshot`` and the metabase commit, or a manual snapshot.

    Parent in-flight protection: if the parent zvol has an open block
    device or an active datapath, the snapshot is skipped.  A
    snapshot/clone in progress against the parent could be mid-flight,
    and destroying its just-created snap would corrupt the operation.

    Args:
        dbg: Debug context string.
        sr_dataset: Full ZFS dataset path (e.g. 'pool/sr-uuid').
        known_vdi_uuids: set of VDI UUID strings present in the database.
            Both base-zvol UUIDs and snap UUIDs share this namespace --
            every snapshot we created has a metabase row keyed by its
            ``snap_uuid``.

    Returns:
        List of (snap_name, used, creation) tuples for orphan snapshots.
        ``used`` and ``creation`` are string values from ``zfs get -p``.
    """
    orphans = []
    all_snaps = dataset_list_snapshots(dbg, sr_dataset)

    for snap_name in all_snaps:
        if "@" not in snap_name:
            # Defensive -- zfs list -t snapshot should never emit this.
            continue
        parent_zvol, snap_uuid = snap_name.split("@", 1)
        if snap_uuid in known_vdi_uuids:
            continue

        # Parent in-flight protection -- re-use zvol checks on the parent.
        if _zvol_device_is_open(parent_zvol):
            log.debug(
                "%s: zsnap_find_orphans: parent %s has open device, "
                "skipping snapshot %s",
                dbg,
                parent_zvol,
                snap_name,
            )
            continue
        if _zvol_has_active_datapath(parent_zvol):
            log.debug(
                "%s: zsnap_find_orphans: parent %s has active "
                "datapath, skipping snapshot %s",
                dbg,
                parent_zvol,
                snap_name,
            )
            continue

        # Confirmed orphan -- gather metadata for reporting
        try:
            props = vol_get_properties_batch(dbg, snap_name, ["used", "creation"])
            used = props.get("used", ("?",))[0]
            creation = props.get("creation", ("?",))[0]
        except Exception:  # pylint: disable=broad-exception-caught
            used = "?"
            creation = "?"

        orphans.append((snap_name, used, creation))

    return orphans


def zsnap_destroy_orphans(dbg, orphans, grace_period_seconds=0):
    """Destroy orphan snapshots (best-effort).

    Mirrors :func:`zvol_destroy_orphans`: applies a grace-period filter
    against the ZFS ``creation`` timestamp and logs failures without
    raising.

    A snapshot with dependent clones cannot be destroyed by
    ``zfs destroy``; that is the *desired* behaviour and is logged at
    warning level rather than treated as an error -- promoting/destroying
    the clone is an explicit operator action, not a cleanup task.
    """
    now = int(time.time())
    for snap_name, used, creation in orphans:
        if grace_period_seconds > 0:
            try:
                creation_ts = int(creation)
            except (ValueError, TypeError):
                log.warning(
                    "%s: orphan snapshot %s has unparseable "
                    "creation %r -- skipping for safety",
                    dbg,
                    snap_name,
                    creation,
                )
                continue
            age = now - creation_ts
            if age < grace_period_seconds:
                log.info(
                    "%s: orphan snapshot %s is %ds old (< grace %ds) "
                    "-- skipping cleanup",
                    dbg,
                    snap_name,
                    age,
                    grace_period_seconds,
                )
                continue
        try:
            log.warning(
                "%s: destroying orphan snapshot %s " "(used=%s, created=%s)",
                dbg,
                snap_name,
                used,
                creation,
            )
            destroy_zvol(dbg, snap_name)
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Most likely cause: snapshot has dependent clone(s).
            # Log and move on -- no automatic promote.
            log.warning(
                "%s: failed to destroy orphan snapshot %s "
                "(may have dependent clones): %s",
                dbg,
                snap_name,
                e,
            )


def zvol_destroy_orphans(dbg, orphans, grace_period_seconds=0):
    """Destroy orphan zvols (best-effort).

    Each *orphan* is a ``(zvol_name, volsize, creation)`` tuple as
    returned by :func:`zvol_find_orphans` -- ``creation`` is the Unix
    timestamp from ``zfs get -p creation``.

    Orphans whose ``creation`` timestamp is younger than
    ``grace_period_seconds`` are skipped to avoid destroying zvols that
    may belong to in-flight operations whose fuser/datapath signals
    have not yet stabilised (e.g. an interrupted SXM mid-allocation).
    A ``grace_period_seconds`` of 0 disables the check.

    Destruction failures are logged but do not raise -- the next
    ``SR.ls`` will retry.
    """
    now = int(time.time())
    for zvol_name, volsize, creation in orphans:
        if grace_period_seconds > 0:
            try:
                creation_ts = int(creation)
            except (ValueError, TypeError):
                log.warning(
                    "%s: orphan %s has unparseable creation %r -- "
                    "skipping for safety",
                    dbg,
                    zvol_name,
                    creation,
                )
                continue
            age = now - creation_ts
            if age < grace_period_seconds:
                log.info(
                    "%s: orphan %s is %ds old (< grace %ds) -- " "skipping cleanup",
                    dbg,
                    zvol_name,
                    age,
                    grace_period_seconds,
                )
                continue
        try:
            log.warning(
                "%s: destroying orphan zvol %s " "(size=%s, created=%s)",
                dbg,
                zvol_name,
                volsize,
                creation,
            )
            destroy_zvol(dbg, zvol_name)
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.error("%s: failed to destroy orphan %s: %s", dbg, zvol_name, e)
