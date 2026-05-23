#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# pylint: disable=too-many-lines

import importlib
import os
import sys
import uuid

import xapi.storage.api.v5.volume
from xapi.storage import log
from xapi.storage.libs import util
from xapi.storage.libs.libcow.callbacks import VolumeContext
from xapi.storage.libs.libcow.lock import PollLock
from xapi.storage.libs.libcow.volume_implementation import (
    Implementation as DefaultImplementation,
)
from libcow.imageformat import ImageFormat

import zfs_operations

# On installed hosts, datapath helpers (qemudisk_raw, cbt_consumer)
# live under PLUGIN_ROOT/datapath/raw+qdisk/. The lazy imports use
# `from datapath import qemudisk_raw` so we need that directory
# importable as a `datapath` package. The installer creates a
# `datapath/` symlink inside the volume plugin dir pointing at the
# sibling datapath/raw+qdisk/ directory, with an __init__.py.
# This sys.path entry makes the symlinked package visible.
_VOLUME_DIR = os.path.dirname(os.path.abspath(__file__))
if _VOLUME_DIR not in sys.path:
    sys.path.insert(0, _VOLUME_DIR)

# ZFS properties that can be changed after VDI creation via Volume.set()
# xapi-storage-script prefixes sm-config keys with this before calling
# Volume.set/unset (see vdi_add_to_sm_config_impl in main.ml)
_SM_CONFIG_PREFIX = "_sm_config_"

# All validation constants live in zfs_operations so sr.py (device-config
# validator at SR.create) and this module (per-VDI Volume.set / unset
# validator + Volume.create's volblocksize translator) cannot drift
# apart. Pre-#217 there were parallel literal definitions in each
# module and the secondarycache asymmetry from #215 was a direct
# consequence.
VALID_VOLBLOCKSIZE = zfs_operations.VALID_VOLBLOCKSIZE
MUTABLE_ZFS_PROPERTIES = zfs_operations.MUTABLE_ZFS_PROPERTIES
IMMUTABLE_ZFS_PROPERTIES = zfs_operations.IMMUTABLE_ZFS_PROPERTIES
VALID_MUTABLE_VALUES = zfs_operations.VALID_MUTABLE_VALUES


@util.decorate_all_routines(util.log_exceptions_in_function)
class Implementation(DefaultImplementation):
    "Volume driver to provide raw volumes from zvol's"

    def create(self, dbg, sr, name, description, size, sharable):
        meta = util.get_sr_metadata(dbg, "file://" + sr)
        sr_dataset = zfs_operations.dataset_path(meta["zpool"], meta["dataset"])

        # Read VDI defaults from SR metadata
        vdi_defaults = meta.get("vdi_defaults", {})
        volblocksize_str = vdi_defaults.get("volblocksize", "8K").upper()
        volblocksize = VALID_VOLBLOCKSIZE.get(volblocksize_str, 8192)
        provisioning = vdi_defaults.get("provisioning", "thin")
        thin = provisioning == "thin"

        log.debug(
            "%s: VDI.create: provisioning=%s, volblocksize=%s (%d bytes)",
            dbg,
            provisioning,
            volblocksize_str,
            volblocksize,
        )

        # Pre-flight check: verify space for thick provisioning
        if not thin:
            try:
                effective_copies = int(zfs_operations.vol_get_property(dbg, sr_dataset, "copies"))
            except Exception:  # pylint: disable=broad-exception-caught
                effective_copies = 1
            required = size * effective_copies
            available = zfs_operations.dataset_get_available(dbg, sr_dataset)
            if required > available:
                raise util.create_storage_error(
                    "SR_BACKEND_FAILURE_109",
                    [
                        "Insufficient space for thick provisioning",
                        "Required: {} bytes ({} x {} copies). "
                        "Available: {} bytes".format(required, size, effective_copies, available),
                    ],
                )

        with VolumeContext(self.callbacks, sr, "w") as opq:
            # Select datapath based on SR configuration
            # Use raw-qdisk (default) for qemu-dp, or tapdisk for blktap
            datapath = meta.get("datapath", "raw-qdisk")
            if datapath == "tapdisk":
                image_type = ImageFormat.IMAGE_RAW
            else:
                image_type = ImageFormat.IMAGE_RAW_QDISK
            image_format = ImageFormat.get_format(image_type)
            vdi_uuid = str(uuid.uuid4())

            with PollLock(opq, "gl", self.callbacks, 0.5):
                with self.callbacks.db_context(opq) as db:
                    volume = db.insert_new_volume(size, image_type)
                    db.insert_vdi(name, description, vdi_uuid, volume.id, sharable)
                    # Name zvol with VDI UUID for consistency with URI
                    path = zfs_operations.format_zvol_name(sr_dataset, vdi_uuid)
                    zfs_operations.create_zvol(
                        dbg, path, size, thin=thin, volblocksize=volblocksize
                    )

                    vol_name = zfs_operations.format_zvol_name(sr_dataset, vdi_uuid)
                    volume.vsize = zfs_operations.get_zvol_size_bytes(dbg, vol_name)
                    if volume.vsize != size:
                        log.debug(
                            "%s: VDI.create adjusted requested size %s to %s",
                            dbg,
                            size,
                            volume.vsize,
                        )
                    db.update_volume_vsize(volume.id, volume.vsize)

            vdi_uri = self.callbacks.getVolumeUriPrefix(opq) + vdi_uuid

        return {
            "key": vdi_uuid,
            "uuid": vdi_uuid,
            "name": name,
            "description": description,
            "read_write": True,
            "virtual_size": volume.vsize,
            "physical_utilisation": zfs_operations.get_zvol_used_bytes(dbg, vol_name),
            "uri": [image_format.uri_prefix + vdi_uri],
            "sharable": False,
            "keys": {},
        }

    def import_existing(self, dbg, sr, key, name, description, size, sharable):
        """Register an *already-created* zvol in the SR's metabase.

        The xe-wrapper's cross-host fast path (#88) lands a zvol on
        the destination via `zfs receive` -- that creates the on-disk
        dataset but doesn't add a row to the SR's metabase, so xapi
        can't see it via `xe vdi-list` until the row exists. This
        method bridges that gap: given a zvol that already exists at
        `<sr_dataset>/<key>`, insert the matching metabase row so
        `xe sr-scan` + `xe vdi-list` resolve it as a normal VDI.

        It is the symmetric counterpart of `create()` minus the
        `create_zvol` call. We deliberately do NOT take a `key`
        argument expressed as a free-form string -- the caller's key
        becomes the new VDI's uuid, identical to the zvol name."""
        meta = util.get_sr_metadata(dbg, "file://" + sr)
        sr_dataset = zfs_operations.dataset_path(meta["zpool"], meta["dataset"])

        vol_name = zfs_operations.format_zvol_name(sr_dataset, key)
        if not zfs_operations.vol_exists(vol_name):
            raise util.create_storage_error(
                "SR_BACKEND_FAILURE_109",
                ["Cannot import_existing -- zvol does not exist", vol_name],
            )

        with VolumeContext(self.callbacks, sr, "w") as opq:
            datapath = meta.get("datapath", "raw-qdisk")
            if datapath == "tapdisk":
                image_type = ImageFormat.IMAGE_RAW
            else:
                image_type = ImageFormat.IMAGE_RAW_QDISK
            image_format = ImageFormat.get_format(image_type)

            with PollLock(opq, "gl", self.callbacks, 0.5):
                with self.callbacks.db_context(opq) as db:
                    volume = db.insert_new_volume(size, image_type)
                    db.insert_vdi(name, description, key, volume.id, sharable)
                    actual_vsize = zfs_operations.get_zvol_size_bytes(dbg, vol_name)
                    db.update_volume_vsize(volume.id, actual_vsize)

            vdi_uri = self.callbacks.getVolumeUriPrefix(opq) + key

        return {
            "key": key,
            "uuid": key,
            "name": name,
            "description": description,
            "read_write": True,
            "virtual_size": actual_vsize,
            "physical_utilisation": zfs_operations.get_zvol_used_bytes(dbg, vol_name),
            "uri": [image_format.uri_prefix + vdi_uri],
            "sharable": False,
            "keys": {},
        }

    def destroy(self, dbg, sr, key):
        meta = util.get_sr_metadata(dbg, "file://" + sr)
        sr_dataset = zfs_operations.dataset_path(meta["zpool"], meta["dataset"])

        cb = self.callbacks
        with VolumeContext(cb, sr, "w") as opq:
            with PollLock(opq, "gl", cb, 0.5):
                with cb.db_context(opq) as db:
                    vdi = db.get_vdi_by_id(key)
                    zfs_operations.recover_vdi_vsize(dbg, vdi, db, sr_dataset)
                    is_snapshot = vdi.volume.snap
                    if is_snapshot:
                        snap_name = zfs_operations.find_snapshot_by_uuid(dbg, sr_dataset, vdi.uuid)
                        zfs_operations.log_pool_tree(
                            dbg, "before destroy {}".format(snap_name), sr_dataset
                        )
                        if snap_name is None:
                            raise util.create_storage_error(
                                "SR_BACKEND_FAILURE_110",
                                [
                                    "Snapshot not found",
                                    "ZFS-Live snapshot object %s/*@%s missing from backing store"
                                    % (sr_dataset, vdi.uuid),
                                ],
                            )

                        # Snapshot VDIs created by Volume.snapshot are
                        # backed by a `<parent>@<snap_uuid>` ZFS snapshot
                        # AND a `<sr>/<snap_uuid>` clone zvol (#71). The
                        # clone has the snapshot as its origin, so it
                        # shows up in zsnap_get_dependencies -- filter
                        # the snapshot's own materialised clone out of
                        # the dependency check so we only refuse the
                        # destroy when an *external* clone (e.g. one
                        # created via Volume.clone(snapshot_vdi)) still
                        # references the snapshot. Without this filter
                        # the dependency check would always trip on our
                        # own clone and destruction would never succeed.
                        clone_path = zfs_operations.format_zvol_name(sr_dataset, vdi.uuid)
                        external_deps = [
                            dep
                            for dep in zfs_operations.find_snapshot_clones(dbg, snap_name)
                            if dep != clone_path
                        ]
                        if external_deps:
                            raise util.create_storage_error(
                                "SR_BACKEND_FAILURE_111",
                                [
                                    "Snapshot has dependents",
                                    "zfs snapshot %s cannot be removed: external dependent clones exist: %s"
                                    % (snap_name, ", ".join(external_deps)),
                                ],
                            )

                        # Safe to destroy: drop our materialised clone
                        # first if present (idempotent for snapshots
                        # created by older driver revisions that didn't
                        # create one), then the underlying ZFS snapshot.
                        if zfs_operations.vol_exists(clone_path):
                            zfs_operations.destroy_zvol(dbg, clone_path)
                        zfs_operations.destroy_zvol(dbg, snap_name)
                    else:
                        # Zvol is named with VDI UUID
                        vol_name = zfs_operations.format_zvol_name(sr_dataset, vdi.uuid)
                        zfs_operations.log_pool_tree(
                            dbg, "before destroy {}".format(vol_name), sr_dataset
                        )
                        # for each snapshot select a clone
                        vol_dependencies = []
                        for vol_snap in zfs_operations.list_zvol_snapshots(dbg, vol_name):
                            snap_dependencies = tuple(
                                zfs_operations.find_snapshot_clones(dbg, vol_snap)
                            )
                            if snap_dependencies:
                                vol_dependencies.append(snap_dependencies[0])
                            else:
                                # no clone: create one!
                                clone_volume = db.insert_child_volume(
                                    vdi.volume.id, vdi.volume.vsize
                                )
                                clone_uuid = str(uuid.uuid4())
                                db.insert_vdi("clone", "", clone_uuid, clone_volume.id, False)
                                clone_path = zfs_operations.format_zvol_name(sr_dataset, clone_uuid)
                                zfs_operations.clone_snapshot(dbg, vol_snap, clone_path)
                                vol_dependencies.append(clone_path)

                        if vol_dependencies:
                            for dep in vol_dependencies:
                                zfs_operations.promote_clone(dbg, dep)
                            zfs_operations.log_pool_tree(dbg, "after promotions", sr_dataset)
                        zfs_operations.destroy_zvol(dbg, vol_name)

                    db.delete_vdi(key)

                    cb.volumeDestroy(opq, str(vdi.volume.id))
                    db.delete_volume(vdi.volume.id)

        # CBT cleanup. The persistent CBT helpers from #30 store
        # `<sr-mount>/.zfs-live/cbt/<key>.pickle`; on VDI destroy we
        # tidy any state for this key. The remove is idempotent --
        # if no CBT export ever ran for this VDI (the common case
        # today, since the save/load lifecycle wire-up is a
        # follow-up), the helper no-ops. Doing this outside the
        # VolumeContext is fine: the file is driver-owned, no
        # libcow lock applies.
        try:
            from datapath import (  # pylint: disable=import-outside-toplevel
                qemudisk_raw,
            )

            sr_uri = "file://" + sr
            qemudisk_raw.remove_cbt_metadata(dbg, sr_uri, key)
            # Also tidy any pre-#104 legacy `<hash>.pickle` left by
            # the old datapath lifecycle key shape -- a VDI deleted
            # before its post-upgrade first activate would otherwise
            # leak the legacy file. Construct the device path the
            # same way the datapath side does, hash it, and call
            # remove on the legacy key. Idempotent -- no-op when no
            # legacy file exists.
            try:
                import hashlib  # pylint: disable=import-outside-toplevel

                device_path = "/dev/zvol/{}/{}".format(sr_dataset, key)
                legacy_key = hashlib.sha256(device_path.encode("utf-8")).hexdigest()[:16]
                qemudisk_raw.remove_cbt_metadata(dbg, sr_uri, legacy_key)
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.warning("{}: legacy CBT cleanup failed " "(continuing): {}".format(dbg, e))
        except ImportError:
            # The datapath module isn't on the path during certain
            # standalone invocations of volume.py (e.g., unit tests
            # that import this module without the SMAPIv3 plugin
            # layout in place). Silently skip -- destroy itself is
            # already complete by the time we get here.
            pass

    def resize(self, dbg, sr, key, new_size):
        meta = util.get_sr_metadata(dbg, "file://" + sr)
        sr_dataset = zfs_operations.dataset_path(meta["zpool"], meta["dataset"])

        cb = self.callbacks
        with VolumeContext(cb, sr, "r") as opq:
            with cb.db_context(opq) as db:
                vdi = db.get_vdi_by_id(key)
                zfs_operations.recover_vdi_vsize(dbg, vdi, db, sr_dataset)
                if new_size < vdi.volume.vsize:
                    log.error(
                        "Resize rejected: shrinking not supported from {} to {}".format(
                            vdi.volume.vsize, new_size
                        )
                    )
                    raise util.create_storage_error(
                        "SR_BACKEND_FAILURE_79",
                        ["VDI Invalid size", "shrinking not allowed"],
                    )
                db.update_volume_vsize(vdi.volume.id, None)
            with cb.db_context(opq) as db:
                vol_name = zfs_operations.format_zvol_name(sr_dataset, vdi.uuid)
                zfs_operations.resize_zvol(dbg, vol_name, new_size)
                vdi.volume.vsize = zfs_operations.get_zvol_size_bytes(dbg, vol_name)
                if vdi.volume.vsize != new_size:
                    log.debug(
                        "%s: VDI.resize adjusted requested size %s to %s",
                        dbg,
                        new_size,
                        vdi.volume.vsize,
                    )
                db.update_volume_vsize(vdi.volume.id, vdi.volume.vsize)
                # Keep refreservation in sync for thick-provisioned VDIs
                zfs_operations.vol_sync_thick_refreservation(dbg, vol_name)

    def stat(self, dbg, sr, key):
        meta = util.get_sr_metadata(dbg, "file://" + sr)
        sr_dataset = zfs_operations.dataset_path(meta["zpool"], meta["dataset"])

        cb = self.callbacks
        with VolumeContext(cb, sr, "r") as opq:
            with cb.db_context(opq) as db:
                vdi = db.get_vdi_by_id(key)
                zfs_operations.recover_vdi_vsize(dbg, vdi, db, sr_dataset)
                image_format = ImageFormat.get_format(vdi.image_type)
                is_snapshot = vdi.volume.snap
                if is_snapshot:
                    vol_name = zfs_operations.find_snapshot_by_uuid(dbg, sr_dataset, vdi.uuid)
                    if vol_name is None:
                        raise util.create_storage_error(
                            "SR_BACKEND_FAILURE_110",
                            [
                                "Snapshot not found",
                                "ZFS snapshot %s missing from backing store" % (vdi.uuid,),
                            ],
                        )
                else:
                    vol_name = zfs_operations.format_zvol_name(sr_dataset, vdi.uuid)
                custom_keys = db.get_vdi_custom_keys(vdi.uuid)

            vdi_uri = cb.getVolumeUriPrefix(opq) + vdi.uuid

        # Query actual ZFS properties from the zvol and report them
        zfs_keys = zfs_operations.vol_get_zfs_properties_dict(dbg, vol_name)
        # Merge: custom keys (user-set overrides) take precedence
        merged_keys = {}
        merged_keys.update(zfs_keys)
        merged_keys.update(custom_keys)

        return {
            "uuid": vdi.uuid,
            "key": vdi.uuid,
            "name": vdi.name,
            "description": vdi.description,
            "read_write": not is_snapshot,
            "virtual_size": vdi.volume.vsize,
            "physical_utilisation": zfs_operations.get_zvol_used_bytes(dbg, vol_name),
            "uri": [image_format.uri_prefix + vdi_uri],
            "keys": merged_keys,
            "sharable": False,
        }

    def snapshot(self, dbg, sr, key):
        meta = util.get_sr_metadata(dbg, "file://" + sr)
        sr_dataset = zfs_operations.dataset_path(meta["zpool"], meta["dataset"])

        snap_uuid = str(uuid.uuid4())
        cb = self.callbacks
        with VolumeContext(cb, sr, "w") as opq:
            with PollLock(opq, "gl", cb, 0.5):
                with cb.db_context(opq) as db:
                    vdi = db.get_vdi_by_id(key)
                    zfs_operations.recover_vdi_vsize(dbg, vdi, db, sr_dataset)
                    image_format = ImageFormat.get_format(vdi.image_type)

                    # Find the base zvol to snapshot
                    # If this is already a snapshot, we need to find the parent VDI
                    if vdi.volume.snap == 0:
                        parent_vdi_uuid = vdi.uuid
                        vol_id = vdi.volume.id
                    else:
                        # This is a snapshot, find the parent VDI
                        parent_vdi = db.get_vdi_for_volume(vdi.volume.parent_id)
                        parent_vdi_uuid = parent_vdi.uuid if parent_vdi else vdi.uuid
                        vol_id = vdi.volume.parent_id

                    snap_volume = db.insert_child_volume(vol_id, vdi.volume.vsize, is_snapshot=True)
                    # Snapshot naming: <parent_vdi_uuid>@<snap_uuid>
                    snap_name = zfs_operations.format_snap_name(
                        sr_dataset, parent_vdi_uuid, snap_uuid
                    )
                    # Clone path: <sr_dataset>/<snap_uuid>. Required so
                    # XAPI's HTTP /export_raw_vdi/ endpoint and our own
                    # Datapath.attach can resolve the snapshot VDI to a
                    # block device at /dev/zvol/<sr>/<snap_uuid>. Without
                    # the clone the ZFS snapshot exists as <parent>@<snap>
                    # but no /dev/zvol entry -- XO backup runs then fail
                    # with "device path does not exist" when XAPI calls
                    # Datapath.attach on the snapshot VDI ref. (#71)
                    clone_path = zfs_operations.format_zvol_name(sr_dataset, snap_uuid)

                    zfs_operations.take_snapshot(dbg, snap_name)
                    zfs_operations.clone_snapshot(dbg, snap_name, clone_path)
                    # Mark the clone read-only -- XAPI's snapshot semantics
                    # treat the snapshot VDI as immutable, and ZFS clones
                    # default to writable. Setting readonly=on prevents
                    # accidental writes through the snapshot's block
                    # device while still allowing reads via Datapath.attach.
                    zfs_operations.vol_set_property(dbg, clone_path, "readonly", "on")

                    db.insert_vdi(
                        vdi.name,
                        vdi.description,
                        snap_uuid,
                        snap_volume.id,
                        vdi.sharable,
                    )

            # Best-effort CBT capture (#102 -- partial #96): if a
            # driver-tracked qemu-dp has a `cbt-active` tracking
            # bitmap on the parent VDI, freeze it under the
            # snapshot's name and persist for incremental backup
            # consumers. Wrapped broadly -- `Volume.snapshot` must
            # not fail because of CBT plumbing.
            try:
                _cbt_capture_at_snapshot(dbg, sr, sr_dataset, parent_vdi_uuid, snap_uuid)
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.warning("{}: CBT snapshot capture failed " "(continuing): {}".format(dbg, e))

            snap_uri = cb.getVolumeUriPrefix(opq) + snap_uuid

        return {
            "uuid": snap_uuid,
            "key": snap_uuid,
            "name": vdi.name,
            "description": vdi.description,
            "read_write": False,
            "virtual_size": vdi.volume.vsize,
            "physical_utilisation": zfs_operations.get_zvol_used_bytes(dbg, snap_name),
            "uri": [image_format.uri_prefix + snap_uri],
            "keys": {},
            "sharable": False,
        }

    def clone(self, dbg, sr, key):
        meta = util.get_sr_metadata(dbg, "file://" + sr)
        sr_dataset = zfs_operations.dataset_path(meta["zpool"], meta["dataset"])

        clone_uuid = str(uuid.uuid4())
        cb = self.callbacks
        with VolumeContext(cb, sr, "w") as opq:
            with PollLock(opq, "gl", cb, 0.5):
                with cb.db_context(opq) as db:
                    vdi = db.get_vdi_by_id(key)
                    zfs_operations.recover_vdi_vsize(dbg, vdi, db, sr_dataset)
                    image_format = ImageFormat.get_format(vdi.image_type)

                    if vdi.volume.snap:
                        # Cloning an existing snapshot
                        snap_name = zfs_operations.find_snapshot_by_uuid(dbg, sr_dataset, vdi.uuid)
                        snap_parent_id = vdi.volume.id
                    else:
                        # Fast clone: create an implicit snapshot first,
                        # then clone from it. zvol_snap_path takes
                        # (sr_dataset, parent_uuid, snap_uuid) -- both
                        # have to be UUIDs to match how the parent zvol
                        # was named at create-time (sr.py creates zvols
                        # as `<sr-dataset>/<vdi-uuid>`). The libcow
                        # Volume's integer `id` is not part of the ZFS
                        # name and would target a non-existent zvol.
                        vol_id = vdi.volume.id
                        auto_snap_uuid = str(uuid.uuid4())
                        auto_snap = db.insert_child_volume(
                            vol_id, vdi.volume.vsize, is_snapshot=True
                        )
                        snap_name = zfs_operations.format_snap_name(
                            sr_dataset, vdi.uuid, auto_snap_uuid
                        )
                        zfs_operations.take_snapshot(dbg, snap_name)
                        snap_parent_id = auto_snap.id

                    cloned_volume = db.insert_child_volume(snap_parent_id, vdi.volume.vsize)

                    # Name cloned zvol with VDI UUID
                    clone_path = zfs_operations.format_zvol_name(sr_dataset, clone_uuid)
                    zfs_operations.clone_snapshot(dbg, snap_name, clone_path)

                    # Preserve thick provisioning: zfs clone creates thin
                    # zvols by default (refreservation=0); if the source
                    # was thick, set refreservation on the clone
                    source_zvol = snap_name.split("@")[0]
                    if zfs_operations.vol_get_refreservation(dbg, source_zvol) > 0:
                        zfs_operations.vol_set_thick_refreservation(dbg, clone_path)

                    db.insert_vdi(
                        vdi.name,
                        vdi.description,
                        clone_uuid,
                        cloned_volume.id,
                        vdi.sharable,
                    )

            clone_uri = cb.getVolumeUriPrefix(opq) + clone_uuid

        return {
            "uuid": clone_uuid,
            "key": clone_uuid,
            "name": vdi.name,
            "description": vdi.description,
            "read_write": True,
            "virtual_size": vdi.volume.vsize,
            "physical_utilisation": zfs_operations.get_zvol_used_bytes(dbg, snap_name),
            "uri": [image_format.uri_prefix + clone_uri],
            "keys": {},
            "sharable": False,
        }

    def similar_content(self, dbg, sr, key):
        # Stub: XAPI falls back to a full copy when no related VDIs are
        # reported, which is correct pending native zfs send/receive (#2).
        log.debug(
            "%s: VDI.similar_content: returning empty list (sr=%s, key=%s)",
            dbg,
            sr,
            key,
        )
        return []

    def list_changed_blocks(self, dbg, sr, key, key2, offset, length):
        """Surface persisted CBT bitmap state to incremental-backup
        consumers per the SMAPIv3 contract (#113).

        Returns the blocks that have changed between volumes `key`
        and `key2` in the extent `[offset, offset+length)` as a
        base64-encoded bit-bitmap. The bitmap covers the requested
        extent extended to the nearest granularity boundaries --
        same rule the spec mandates.

        ## Which bitmap state corresponds to "between key and key2"

        Under the producer contracts established in PR #99 / #103:

          - `cbt-active` on a head VDI tracks writes **since the
            most recent snapshot** (cleared and re-enabled on each
            `Volume.snapshot`).
          - `cbt-snap-<snap_uuid>` on a snapshot VDI is the
            **frozen bitmap as of that snapshot's moment** --
            writes that led *up to* that snapshot, since the
            previous snapshot point.

        So the bitmap that represents "writes between A and B"
        (where B is the later one) is owned by **B alone**:

          - `(snap_old, snap_new)`: `snap_new`'s
            `cbt-snap-<snap_new_uuid>` captures writes from
            `snap_old` to `snap_new`.
          - `(snap, head)`: `head`'s `cbt-active` captures writes
            since `snap`.

        Unioning A's state in (the original draft of this method)
        over-reports: A's `cbt-snap-<a>` represents writes BEFORE
        A's snapshot moment, irrelevant to the A->B delta. So we
        return **only key2's persisted state**, ignoring key.

        ## Caller convention

        Pass `(older, newer)` -- same convention every other
        SMAPIv3 caller follows for incremental-backup operations.
        Reversing the order returns the bitmap from key's
        perspective, which represents the previous interval, not
        the requested one. We don't try to detect / re-order
        because libcow's snapshot graph isn't always reachable
        from this method (the call doesn't guarantee an attached
        SR), and over-reporting is far worse than asking the
        caller to follow a documented convention.

        ## No-state shape

        If key2 has no persisted CBT state (CBT not enabled, or
        no qemu-dp ever serviced this VDI), the bitmap is all
        zeros at the default granularity. The operator gets
        "no changes" rather than an error -- same wire shape as a
        CBT-enabled VDI that simply hasn't seen any writes.
        """
        log.debug(
            "{}: Volume.list_changed_blocks: sr={} key={} key2={} "
            "offset={} length={}".format(dbg, sr, key, key2, offset, length)
        )

        # Lazy import: cbt_consumer pulls in qemudisk_raw which
        # is host-only on the production install.
        try:
            from datapath import (  # pylint: disable=import-outside-toplevel
                cbt_consumer,
            )
        except ImportError as e:
            log.debug(
                "{}: list_changed_blocks: cbt_consumer not "
                "available ({}); returning empty bitmap".format(dbg, e)
            )
            return _empty_changed_blocks_result(offset, length)

        sr_uri = "file://" + sr

        # Only key2's state -- see the docstring's "Which bitmap
        # state corresponds to..." section for why unioning key's
        # state too over-reports.
        try:
            extents_b = cbt_consumer.extract_dirty_extents_for(dbg, sr_uri, key2)
        except Exception as e:  # noqa: BLE001  pylint: disable=broad-exception-caught
            log.warning(
                "{}: list_changed_blocks: extent extraction for "
                "key2={} failed ({}), returning empty bitmap".format(dbg, key2, e)
            )
            return _empty_changed_blocks_result(offset, length)

        # Granularity: max across key2's bitmaps (or default).
        granularity = _resolve_changed_blocks_granularity(dbg, sr_uri, key2)

        # Union the bitmaps WITHIN key2's state -- a head VDI may
        # carry both `cbt-active` and a freshly-captured
        # `cbt-snap-*` between two snapshot points; both are
        # writes-since-the-previous-snapshot under the producer
        # contract, so they union cleanly.
        all_extents = []
        for bitmap_extents in extents_b.values():
            all_extents.extend(bitmap_extents)

        bitmap_b64 = _pack_extents_to_bitmap(all_extents, offset, length, granularity)

        return {
            "granularity": granularity,
            "bitmap": bitmap_b64,
        }

    def copy(self, dbg, sr, key, dest_sr):
        """Copy a VDI to another zfs-live SR using native ZFS replication.

        Two paths, picked from the pool topology:

          - **Same-pool** (source and destination SRs share a `zpool`):
            transient `zfs snapshot` then `zfs clone`. The clone is
            instantaneous and CoW-shared with the source until either
            side is modified. The shared snapshot stays in place; the
            existing `Volume.destroy` handles dependent-clone promotion
            when either VDI is later destroyed (mirrors `clone()`'s
            semantics -- see destroy() at the dependency-walk block).
          - **Cross-pool**: `zfs send -c | zfs receive` over a kernel
            pipe (no temp file). `-c` ships compressed records when
            the source is compressed, preserving sparseness. After the
            stream completes the transient source snapshot AND the
            auto-created destination snapshot are destroyed so neither
            VDI carries dead snapshots forward.

        Bypasses the upstream `xapi-storage-script` OCaml dispatch gap
        on `VDI.similar_content` (see docs/known-limitations.md /
        #78) -- XAPI calls Volume.copy directly without going through
        the storage-mux similarity-detection path.

        If the destination is not a zfs-live SR, raises Unimplemented
        so XAPI falls back to its generic copy code path.
        """
        src_meta = util.get_sr_metadata(dbg, "file://" + sr)
        src_sr_dataset = zfs_operations.dataset_path(src_meta["zpool"], src_meta["dataset"])

        try:
            dest_meta = util.get_sr_metadata(dbg, "file://" + dest_sr)
        except Exception as e:
            log.debug(
                "%s: Volume.copy: dest SR %s metabase unreadable (%s); "
                "fall back to generic copy",
                dbg,
                dest_sr,
                e,
            )
            raise xapi.storage.api.v5.volume.Unimplemented("Volume.copy")
        if not dest_meta or "zpool" not in dest_meta or "dataset" not in dest_meta:
            log.debug(
                "%s: Volume.copy: dest SR %s is not zfs-live; " "fall back to generic copy",
                dbg,
                dest_sr,
            )
            raise xapi.storage.api.v5.volume.Unimplemented("Volume.copy")

        dest_sr_dataset = zfs_operations.dataset_path(dest_meta["zpool"], dest_meta["dataset"])

        same_pool = src_meta["zpool"] == dest_meta["zpool"]
        dest_uuid = str(uuid.uuid4())
        snap_token = str(uuid.uuid4())
        cb = self.callbacks

        src_vol = zfs_operations.format_zvol_name(src_sr_dataset, key)
        dest_vol = zfs_operations.format_zvol_name(dest_sr_dataset, dest_uuid)
        snap_name = zfs_operations.format_snap_name(src_sr_dataset, key, snap_token)

        log.debug(
            "%s: Volume.copy: src=%s dest=%s same_pool=%s",
            dbg,
            src_vol,
            dest_vol,
            same_pool,
        )

        # Phase 1 -- read source VDI under the source SR's write lock,
        # then snapshot it. Holding the write lock here means a
        # concurrent destroy/resize on the source can't race the
        # snapshot creation.
        with VolumeContext(cb, sr, "w") as src_opq:
            with PollLock(src_opq, "gl", cb, 0.5):
                with cb.db_context(src_opq) as db:
                    vdi = db.get_vdi_by_id(key)
                    zfs_operations.recover_vdi_vsize(dbg, vdi, db, src_sr_dataset)
                    image_type = vdi.image_type
                    vsize = vdi.volume.vsize
                    vdi_name = vdi.name
                    vdi_description = vdi.description
                    vdi_sharable = vdi.sharable
                zfs_operations.take_snapshot(dbg, snap_name)

        # Capture src thickness *before* phase 2 so a same-pool clone
        # gets the matching refreservation set explicitly (zfs clone
        # creates thin zvols regardless of source).
        src_thick = zfs_operations.vol_get_refreservation(dbg, src_vol) > 0

        # Phase 2 -- replicate. On any failure, tear down the partial
        # destination zvol and the source snapshot so we don't leak
        # state into the SR.
        try:
            if same_pool:
                zfs_operations.clone_snapshot(dbg, snap_name, dest_vol)
            else:
                zfs_operations.vol_send_receive(dbg, snap_name, dest_vol)
            if src_thick:
                zfs_operations.vol_set_thick_refreservation(dbg, dest_vol)
        except Exception:  # pylint: disable=broad-exception-caught
            try:
                if zfs_operations.vol_exists(dest_vol):
                    zfs_operations.destroy_zvol(dbg, dest_vol)
            except Exception as cleanup_err:  # pylint: disable=broad-exception-caught
                log.error(
                    "%s: Volume.copy: failed to clean up %s: %s",
                    dbg,
                    dest_vol,
                    cleanup_err,
                )
            try:
                zfs_operations.destroy_zvol(dbg, snap_name)
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            raise

        # Phase 3 -- post-replication cleanup. The contract for cross-pool
        # copy is "no transient snapshots remain after success", so both
        # cleanups must succeed. If either destroy fails (e.g. ZFS error,
        # or -- though unrealistic between phase 2 and here -- a dependent
        # clone has appeared), tear down the destination zvol so the SR
        # is left as we found it and re-raise. Best-effort log-and-swallow
        # here would let us return success while leaking a snapshot that
        # the docs and tests promise is gone.
        if not same_pool:
            recv_snap = "{}@{}".format(dest_vol, snap_token)
            try:
                zfs_operations.destroy_zvol(dbg, snap_name)
                zfs_operations.destroy_zvol(dbg, recv_snap)
            except Exception:  # pylint: disable=broad-exception-caught
                # Roll back the destination so phase 4 doesn't register
                # a VDI whose backing state we couldn't fully clean up.
                # Nested cleanup failures are logged but don't mask the
                # original error -- operator may still need to inspect.
                try:
                    if zfs_operations.vol_exists(dest_vol):
                        zfs_operations.destroy_zvol(dbg, dest_vol)
                except Exception as cleanup_err:  # pylint: disable=broad-exception-caught
                    log.error(
                        "%s: Volume.copy: failed to clean up %s after "
                        "transient-snapshot teardown error: %s",
                        dbg,
                        dest_vol,
                        cleanup_err,
                    )
                # Best-effort: try the snapshots once more in case the
                # first failure was the receive snap and the source snap
                # is still around.
                for s in (snap_name, recv_snap):
                    try:
                        if zfs_operations.vol_exists(s.split("@")[0]):
                            zfs_operations.destroy_zvol(dbg, s)
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass
                raise
        # Same-pool: keep the snapshot -- it's the CoW backing for both
        # VDIs. Volume.destroy already promotes dependent clones when
        # either side is later destroyed, so no upkeep is needed.

        # Phase 4 -- register the destination VDI in the destination
        # SR's metabase. Separate context from phase 1 so we don't
        # hold two SR write locks at once (deadlock vector).
        with VolumeContext(cb, dest_sr, "w") as dest_opq:
            with PollLock(dest_opq, "gl", cb, 0.5):
                with cb.db_context(dest_opq) as db:
                    volume = db.insert_new_volume(vsize, image_type)
                    db.insert_vdi(vdi_name, vdi_description, dest_uuid, volume.id, vdi_sharable)
                    db.update_volume_vsize(volume.id, vsize)
            dest_uri = cb.getVolumeUriPrefix(dest_opq) + dest_uuid

        image_format = ImageFormat.get_format(image_type)
        return {
            "key": dest_uuid,
            "uuid": dest_uuid,
            "name": vdi_name,
            "description": vdi_description,
            "read_write": True,
            "virtual_size": vsize,
            "physical_utilisation": zfs_operations.get_zvol_used_bytes(dbg, dest_vol),
            "uri": [image_format.uri_prefix + dest_uri],
            "sharable": False,
            "keys": {},
        }

    def set(self, dbg, sr, key, k, v):
        # xapi-storage-script prefixes sm-config keys with _sm_config_
        if k.startswith(_SM_CONFIG_PREFIX):
            k = k[len(_SM_CONFIG_PREFIX) :]

        meta = util.get_sr_metadata(dbg, "file://" + sr)
        sr_dataset = zfs_operations.dataset_path(meta["zpool"], meta["dataset"])

        # Check if this is a mutable ZFS property
        if k in MUTABLE_ZFS_PROPERTIES:
            # Validate value before applying to ZFS
            if k in VALID_MUTABLE_VALUES:
                check_val = v.lower() if k != "copies" else v
                valid = VALID_MUTABLE_VALUES[k]
                if check_val not in valid:
                    raise util.create_storage_error(
                        "SR_BACKEND_FAILURE_109",
                        [
                            "Invalid property value",
                            "{} must be one of: {}, got '{}'".format(
                                k, ", ".join(sorted(valid)), v
                            ),
                        ],
                    )
            vol_name = zfs_operations.format_zvol_name(sr_dataset, key)
            log.debug("%s: VDI.set: applying ZFS property %s=%s on %s", dbg, k, v, vol_name)
            zfs_operations.vol_set_property(dbg, vol_name, k, v)
            # When copies changes on a thick VDI, refreservation must be
            # recalculated to keep the space guarantee accurate
            if k == "copies":
                zfs_operations.vol_sync_thick_refreservation(dbg, vol_name)
        elif k in IMMUTABLE_ZFS_PROPERTIES:
            vol_name = zfs_operations.format_zvol_name(sr_dataset, key)
            if k == "volblocksize":
                current = zfs_operations.format_volblocksize(
                    zfs_operations.vol_get_property(dbg, vol_name, "volblocksize")
                )
            elif k == "provisioning":
                refres = zfs_operations.vol_get_refreservation(dbg, vol_name)
                current = "thick" if refres > 0 else "thin"
            else:
                current = "unknown"
            raise util.create_storage_error(
                "SR_BACKEND_FAILURE_109",
                [
                    "Immutable property",
                    "{} cannot be changed after VDI creation. "
                    "Current value: {}".format(k, current),
                ],
            )

        # Store in custom keys (for all properties, including non-ZFS)
        cb = self.callbacks
        with VolumeContext(cb, sr, "w") as opq:
            with PollLock(opq, "gl", cb, 0.5):
                with cb.db_context(opq) as db:
                    db.set_vdi_custom_key(key, k, v)

    def unset(self, dbg, sr, key, k):
        # xapi-storage-script prefixes sm-config keys with _sm_config_
        if k.startswith(_SM_CONFIG_PREFIX):
            k = k[len(_SM_CONFIG_PREFIX) :]

        meta = util.get_sr_metadata(dbg, "file://" + sr)
        sr_dataset = zfs_operations.dataset_path(meta["zpool"], meta["dataset"])

        # If this is a mutable ZFS property, revert to inherited value
        if k in MUTABLE_ZFS_PROPERTIES:
            vol_name = zfs_operations.format_zvol_name(sr_dataset, key)
            log.debug("%s: VDI.unset: inheriting ZFS property %s on %s", dbg, k, vol_name)
            zfs_operations.vol_inherit_property(dbg, vol_name, k)
            # When copies reverts to inherited value on a thick VDI,
            # refreservation must be recalculated
            if k == "copies":
                zfs_operations.vol_sync_thick_refreservation(dbg, vol_name)
        elif k in IMMUTABLE_ZFS_PROPERTIES:
            vol_name = zfs_operations.format_zvol_name(sr_dataset, key)
            if k == "volblocksize":
                current = zfs_operations.format_volblocksize(
                    zfs_operations.vol_get_property(dbg, vol_name, "volblocksize")
                )
            elif k == "provisioning":
                refres = zfs_operations.vol_get_refreservation(dbg, vol_name)
                current = "thick" if refres > 0 else "thin"
            else:
                current = "unknown"
            raise util.create_storage_error(
                "SR_BACKEND_FAILURE_109",
                [
                    "Immutable property",
                    "{} cannot be changed after VDI creation. "
                    "Current value: {}".format(k, current),
                ],
            )

        # Remove from custom keys
        cb = self.callbacks
        with VolumeContext(cb, sr, "w") as opq:
            with PollLock(opq, "gl", cb, 0.5):
                with cb.db_context(opq) as db:
                    db.delete_vdi_custom_key(key, k)


# -- CBT capture at Volume.snapshot (#102) ---------------------------
#
# Counterpart to the Datapath.activate/deactivate hooks (#100). When
# a snapshot is taken, the live `cbt-active` tracking bitmap stops
# growing -- but the consumer's next incremental backup needs *that*
# frozen state as its reference. We freeze the live bitmap (rename
# it to `cbt-snap-<snap_uuid>` per the convention in qemudisk_raw.py's
# CBT section header), open a fresh `cbt-active` for forward
# tracking, export the frozen one, and persist under the snapshot
# VDI's key so it survives qemu-dp restart / reboot.
#
# Bounded to **driver-tracked qemu-dp instances** -- same scope rule
# as #100. xenopsd-spawned qemu-dp QMP discovery is a separate
# follow-up under #96.

# Convention: snapshot-era frozen bitmaps are named `cbt-snap-<uuid>`,
# matching the section header in qemudisk_raw.py.
_CBT_SNAPSHOT_BITMAP_PREFIX = "cbt-snap-"
_CBT_ACTIVE_BITMAP = "cbt-active"


def _cbt_capture_at_snapshot(dbg, sr, sr_dataset, parent_vdi_uuid, snap_uuid):
    """Freeze the parent VDI's tracking bitmap and persist it under
    the snapshot's key. No-op if no driver-tracked qemu-dp is
    registered for the parent or no `cbt-active` bitmap is live."""
    # Lazy import: the datapath module only imports cleanly on a
    # host install (it pulls in xapi.storage.api.v5.datapath). The
    # volume tests stub xapi but not the datapath subset; gating
    # the import here keeps the snapshot path importable on any
    # environment that runs this code.
    try:
        from datapath import (  # pylint: disable=import-outside-toplevel
            qemudisk_raw,
        )
    except ImportError as e:
        log.debug(
            "{}: CBT snapshot capture: qemudisk_raw not " "available ({}); skipping".format(dbg, e)
        )
        return

    # Datapath-side key shape: sha256 of the parent's device path.
    # See `_device_key` in src/datapath/datapath.py -- same hash
    # construction the lifecycle hooks use, so we look up the same
    # qemu-dp instance.
    import hashlib  # pylint: disable=import-outside-toplevel

    device_path = "/dev/zvol/{}/{}".format(sr_dataset, parent_vdi_uuid)
    device_key = hashlib.sha256(device_path.encode("utf-8")).hexdigest()[:16]

    qemu_disk = qemudisk_raw.load_metadata(dbg, device_key)
    if qemu_disk is None:
        # Fall through to xenopsd's qemu-dm (#106). The volume
        # plugin doesn't directly own the discovery helper that
        # the datapath plugin defines; import lazily and reach
        # through. Wrapped in try/except so a missing datapath
        # module on a stripped-down install just no-ops.
        try:
            from datapath import (  # pylint: disable=import-outside-toplevel
                datapath as _dp,
            )

            qemu_disk = _dp._find_xenopsd_qemu_for_device(dbg, device_path)
        except ImportError:
            qemu_disk = None
    if qemu_disk is None:
        log.debug(
            "{}: CBT snapshot capture: no qemu instance "
            "carries {} -- no-op".format(dbg, parent_vdi_uuid)
        )
        return

    bitmaps = qemu_disk.cbt_list_bitmaps(dbg)
    active = next((bm for bm in bitmaps if bm.get("name") == _CBT_ACTIVE_BITMAP), None)
    if active is None:
        log.debug(
            "{}: CBT snapshot capture: no '{}' bitmap on {} -- "
            "no-op".format(dbg, _CBT_ACTIVE_BITMAP, parent_vdi_uuid)
        )
        return

    frozen_name = _CBT_SNAPSHOT_BITMAP_PREFIX + snap_uuid
    granularity = active.get("granularity", 65536)

    # Capture sequence. Note that `cbt_snapshot_bitmap` (the
    # existing helper) is NOT what we want here: its docstring
    # contract is "disable current_name + add new_name", i.e. the
    # OLD bitmap stays frozen under `cbt-active` and the NEW
    # active opens under `cbt-snap-<snap>`. That's the opposite of
    # the lifecycle we need (forward tracking would stop being
    # under `cbt-active` after the first snapshot, and the
    # `cbt-active`-prefix lifecycle filter from #100/#101 would
    # immediately stop covering new writes).
    #
    # The right sequence is:
    #   1. disable cbt-active            (stop counting writes)
    #   2. add cbt-snap-<snap>           (placeholder of same shape)
    #   3. merge cbt-active -> cbt-snap   (copy bits-as-of-disable)
    #   4. clear cbt-active              (zero its bits)
    #   5. enable cbt-active             (resume forward tracking)
    #
    # After this dance:
    #   * cbt-snap-<snap> holds the dirty bits as of the snapshot
    #     moment -- that's what we export and persist
    #   * cbt-active is empty and tracking forward writes again,
    #     under the same name so the lifecycle hooks still match it
    qemu_disk.cbt_bitmap_disable(dbg, _CBT_ACTIVE_BITMAP)
    qemu_disk.cbt_bitmap_add(dbg, frozen_name, granularity=granularity)
    qemu_disk.cbt_bitmap_merge(dbg, frozen_name, [_CBT_ACTIVE_BITMAP])
    qemu_disk.cbt_bitmap_clear(dbg, _CBT_ACTIVE_BITMAP)
    qemu_disk.cbt_bitmap_enable(dbg, _CBT_ACTIVE_BITMAP)

    # Export the frozen bitmap as a persistable payload.
    payload = qemu_disk.export_bitmap(dbg, frozen_name)

    # Persist under the snapshot VDI's key (matches the
    # `Volume.destroy -> remove_cbt_metadata` precedent that uses
    # the libcow VDI uuid). Multi-bitmap dict shape mirrors the
    # datapath lifecycle's wire format from #100, so a future
    # consumer reads both file shapes uniformly.
    qemudisk_raw.save_cbt_metadata(dbg, "file://" + sr, snap_uuid, {frozen_name: payload})

    log.debug(
        "{}: CBT snapshot capture: persisted '{}' for snap "
        "{}".format(dbg, frozen_name, snap_uuid)
    )


# --- list_changed_blocks helpers (#113) -------------------------------
#
# `Volume.list_changed_blocks` returns a base64-encoded bit-bitmap
# whose bit `i` is set iff block `i` (of size `granularity`) within
# the requested extent has changed between the two VDIs. The spec
# (xapi-storage-api volume.py:1724) says:
#
#   "If this extent is not aligned to the granularity of the
#    returned bitmap, then the bitmap will cover the area extended
#    to the nearest block boundaries."
#
# So we round `offset` down and `(offset+length)` up to granularity
# multiples, build a bit-bitmap covering the rounded span, and
# emit it as base64.

_DEFAULT_CBT_GRANULARITY = 65536


def _empty_changed_blocks_result(offset, length, granularity=_DEFAULT_CBT_GRANULARITY):
    """No-state shape: return a correctly-sized all-zeros bitmap.
    Operator sees "no changes" rather than an error -- same wire
    shape as a CBT-enabled VDI that hasn't seen any writes."""
    import base64  # pylint: disable=import-outside-toplevel

    aligned_start = (offset // granularity) * granularity
    aligned_end = -(-(offset + length) // granularity) * granularity
    nblocks = (aligned_end - aligned_start) // granularity
    nbytes = (nblocks + 7) // 8
    return {
        "granularity": granularity,
        "bitmap": base64.b64encode(b"\x00" * nbytes).decode("ascii"),
    }


def _resolve_changed_blocks_granularity(dbg, sr_uri, vdi_key):
    """Pick the granularity for the returned bitmap as the max
    across all persisted bitmaps for `vdi_key`. Falls back to the
    default if no payload exists.

    Maximum is correct (not optimal): a block dirty at finer
    granularity is necessarily dirty at coarser, so a coarser
    output is always a safe over-approximation. The spec's
    rounded-extent wording covers this case.

    Only `vdi_key` (key2) is consulted -- the source semantics
    documented on `list_changed_blocks` use only key2's state, so
    using key's granularity here would be misleading metadata.
    """
    try:
        from datapath import (  # pylint: disable=import-outside-toplevel
            qemudisk_raw,
        )
    except ImportError:
        return _DEFAULT_CBT_GRANULARITY

    granularities = []
    multi = qemudisk_raw.load_cbt_metadata(dbg, sr_uri, vdi_key)
    if isinstance(multi, dict):
        for bitmap_payload in multi.values():
            if not isinstance(bitmap_payload, dict):
                continue
            g = bitmap_payload.get("granularity")
            if isinstance(g, int) and g > 0:
                granularities.append(g)

    return max(granularities) if granularities else _DEFAULT_CBT_GRANULARITY


def _pack_extents_to_bitmap(extents, offset, length, granularity):
    """Pack a list of `(extent_offset, extent_length)` byte-ranges
    into a base64-encoded bit-bitmap where bit `i` is set iff
    block `i` (of size `granularity`) in the rounded span
    `[aligned_start, aligned_end)` has any byte-overlap with any
    extent.

    Bit ordering: MSB-first within each byte (matches the
    convention every other CBT-bitmap producer in xapi-land
    uses; verified against `xe vdi-list-changed-blocks` output
    on a VHD-based SR).
    """
    import base64  # pylint: disable=import-outside-toplevel

    aligned_start = (offset // granularity) * granularity
    aligned_end = -(-(offset + length) // granularity) * granularity
    nblocks = (aligned_end - aligned_start) // granularity
    bits = bytearray((nblocks + 7) // 8)

    for ext_off, ext_len in extents:
        ext_end = ext_off + ext_len
        # Clip to the rounded span before mapping into block
        # indices -- extents outside the requested range don't
        # contribute bits.
        clip_start = max(ext_off, aligned_start)
        clip_end = min(ext_end, aligned_end)
        if clip_end <= clip_start:
            continue
        # First / last block indices the extent touches (any
        # byte-overlap counts; even a one-byte sub-block write
        # marks the whole block dirty per QEMU's bitmap shape).
        first_block = (clip_start - aligned_start) // granularity
        last_block = (clip_end - 1 - aligned_start) // granularity
        for b in range(first_block, last_block + 1):
            bits[b // 8] |= 0x80 >> (b % 8)

    return base64.b64encode(bytes(bits)).decode("ascii")


def call_volume_command():
    """Parse the arguments and call the required command"""
    log.log_call_argv()
    fsp = importlib.import_module("zfs_live")
    cmd = xapi.storage.api.v5.volume.Volume_commandline(Implementation(fsp.Callbacks()))
    base = os.path.basename(sys.argv[0])
    if base == "Volume.create":
        cmd.create()
    elif base == "Volume.destroy":
        cmd.destroy()
    elif base == "Volume.resize":
        cmd.resize()
    elif base == "Volume.snapshot":
        cmd.snapshot()
    elif base == "Volume.clone":
        cmd.clone()
    elif base == "Volume.stat":
        cmd.stat()
    elif base == "Volume.set":
        cmd.set()
    elif base == "Volume.unset":
        cmd.unset()
    elif base == "Volume.set_name":
        cmd.set_name()
    elif base == "Volume.set_description":
        cmd.set_description()
    elif base == "Volume.similar_content":
        cmd.similar_content()
    elif base == "Volume.list_changed_blocks":
        cmd.list_changed_blocks()
    elif base == "Volume.copy":
        cmd.copy()
    elif base == "Volume.import_existing":
        # Custom method (not in upstream Volume_commandline). Read the
        # JSON args from stdin like every other plugin script and
        # invoke the implementation directly. The xe-wrapper's
        # cross-host fast path is the only caller -- see #88 + the
        # `import_existing()` docstring above.
        import json as _json  # pylint: disable=import-outside-toplevel

        request = _json.loads(sys.stdin.readline())
        impl = Implementation(fsp.Callbacks())
        result = impl.import_existing(
            request["dbg"],
            request["sr"],
            request["key"],
            request.get("name", ""),
            request.get("description", ""),
            int(request.get("size", 0)),
            bool(request.get("sharable", False)),
        )
        print(_json.dumps({"Status": "Success", "Value": result}))
    else:
        raise xapi.storage.api.v5.volume.Unimplemented(base)


if __name__ == "__main__":
    call_volume_command()
