#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import importlib
import os
import os.path
import sys

import xapi.storage.api.v5.volume
from xapi.storage import log
from xapi.storage.common import call
from xapi.storage.libs import util
from xapi.storage.libs.libcow.callbacks import VolumeContext
from libcow.imageformat import ImageFormat

import zfs_operations
import zfs_features
from sr_capability_summary import format_capability_summary

# Valid values for ZFS properties configurable via device-config.
# Single source of truth in zfs_operations so volume.py's per-VDI mutator
# stays aligned (#217 finding 3).
VALID_COMPRESSION = zfs_operations.VALID_COMPRESSION
VALID_COPIES = zfs_operations.VALID_COPIES
VALID_SYNC = zfs_operations.VALID_SYNC
VALID_ATIME = zfs_operations.VALID_ATIME
VALID_CACHE = zfs_operations.VALID_CACHE
VALID_LOGBIAS = zfs_operations.VALID_LOGBIAS
VALID_PROVISIONING = zfs_operations.VALID_PROVISIONING
VALID_VOLBLOCKSIZE = zfs_operations.VALID_VOLBLOCKSIZE


def _validate_config_value(param, value, valid_set, configuration_errors):
    """Validate a device-config value against a set of valid options."""
    if value not in valid_set:
        configuration_errors.append(
            "{} must be one of: {}, got '{}'".format(param, ", ".join(sorted(valid_set)), value)
        )


def _parse_zfs_config(configuration, dbg=None, pool_name=None):
    """Parse and validate ZFS properties from SR device-config.

    Args:
        configuration: device-config dict from XAPI
        dbg: debug context (needed for ashift check)
        pool_name: ZFS pool name (needed for ashift check)

    Returns:
        tuple: (dataset_props, vdi_defaults, sr_settings, errors)
            dataset_props: dict of ZFS properties to set on the SR dataset
            vdi_defaults: dict of default settings for VDIs in this SR
            sr_settings: dict of SR-level metadata entries (e.g. orphan
                cleanup config) to merge into the top-level SR metadata
            errors: list of validation error messages
    """
    errors = []
    dataset_props = {}
    vdi_defaults = {}
    sr_settings = {}

    # SR dataset-level properties (inherited by zvols unless overridden)
    if "compression" in configuration:
        val = configuration["compression"].lower()
        _validate_config_value("compression", val, VALID_COMPRESSION, errors)
        dataset_props["compression"] = val

    if "copies" in configuration:
        val = configuration["copies"]
        _validate_config_value("copies", val, VALID_COPIES, errors)
        dataset_props["copies"] = val

    if "sync" in configuration:
        val = configuration["sync"].lower()
        _validate_config_value("sync", val, VALID_SYNC, errors)
        dataset_props["sync"] = val

    if "atime" in configuration:
        val = configuration["atime"].lower()
        _validate_config_value("atime", val, VALID_ATIME, errors)
        dataset_props["atime"] = val

    if "primarycache" in configuration:
        val = configuration["primarycache"].lower()
        _validate_config_value("primarycache", val, VALID_CACHE, errors)
        dataset_props["primarycache"] = val

    if "secondarycache" in configuration:
        val = configuration["secondarycache"].lower()
        _validate_config_value("secondarycache", val, VALID_CACHE, errors)
        dataset_props["secondarycache"] = val

    if "logbias" in configuration:
        val = configuration["logbias"].lower()
        _validate_config_value("logbias", val, VALID_LOGBIAS, errors)
        dataset_props["logbias"] = val

    # VDI-level defaults (stored in SR metadata, used during Volume.create)
    effective_volblocksize = 8192  # default
    if "volblocksize" in configuration:
        val = configuration["volblocksize"].upper()
        if val not in VALID_VOLBLOCKSIZE:
            errors.append(
                "volblocksize must be one of: {}, got '{}'".format(
                    ", ".join(
                        sorted(
                            VALID_VOLBLOCKSIZE.keys(),
                            key=lambda x: VALID_VOLBLOCKSIZE[x],
                        )
                    ),
                    configuration["volblocksize"],
                )
            )
        else:
            vdi_defaults["volblocksize"] = val
            effective_volblocksize = VALID_VOLBLOCKSIZE[val]

    if "provisioning" in configuration:
        val = configuration["provisioning"].lower()
        _validate_config_value("provisioning", val, VALID_PROVISIONING, errors)
        vdi_defaults["provisioning"] = val

    # Orphan-detection settings (top-level SR metadata, read by SR.ls)
    if "auto_cleanup_orphans" in configuration:
        val = configuration["auto_cleanup_orphans"].lower()
        if val not in ("true", "false"):
            errors.append(
                "auto_cleanup_orphans must be 'true' or 'false', got '{}'".format(
                    configuration["auto_cleanup_orphans"]
                )
            )
        else:
            sr_settings["auto_cleanup_orphans"] = val

    if "orphan_grace_period_seconds" in configuration:
        raw = configuration["orphan_grace_period_seconds"]
        try:
            grace = int(raw)
            if grace < 0:
                raise ValueError
            sr_settings["orphan_grace_period_seconds"] = grace
        except (TypeError, ValueError):
            errors.append(
                "orphan_grace_period_seconds must be a non-negative "
                "integer, got '{}'".format(raw)
            )

    # Validate volblocksize against pool's minimum sector size (ashift)
    if dbg is not None and pool_name is not None:
        try:
            ashift = zfs_operations.pool_get_ashift(dbg, pool_name)
            min_sector = 1 << ashift
            if effective_volblocksize < min_sector:
                min_sector_fmt = zfs_operations.format_volblocksize(min_sector)
                errors.append(
                    "volblocksize ({vbs}) is smaller than pool's minimum "
                    "sector size ({ms}). Use volblocksize >= {ms} for this "
                    "pool".format(
                        vbs=zfs_operations.format_volblocksize(effective_volblocksize),
                        ms=min_sector_fmt,
                    )
                )
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.debug("%s: _parse_zfs_config: failed to check pool ashift: %s", dbg, e)

    return dataset_props, vdi_defaults, sr_settings, errors


# `_get_zfs_properties_for_ls` was a near-byte-identical duplicate
# of `_get_zfs_properties` in volume.py -- both have moved into
# `zfs_operations.vol_get_zfs_properties_dict` (#217 finding 2).


@util.decorate_all_routines(util.log_exceptions_in_function)
class Implementation(xapi.storage.api.v5.volume.SR_skeleton):
    "SR driver to provide volumes from zvol's"

    def create(self, dbg, sr_uuid, configuration, name, description):
        log.debug("{}: SR.create: config={}, sr_uuid={}".format(dbg, configuration, sr_uuid))

        # Require path parameter - specifies where SR dataset will be created
        # Examples: "zpool", "zpool/storage", "zpool/vms/production"
        if "path" not in configuration:
            log.error("path parameter is required")
            raise util.create_storage_error(
                "SR_BACKEND_FAILURE_109",
                [
                    "Invalid configuration",
                    "path parameter is required (e.g., path=zpool or path=zpool/storage)",
                ],
            )

        # Parse path - first component is pool, rest is parent path
        path = configuration["path"].strip("/")
        path_parts = path.split("/")
        pool_name = path_parts[0]
        parent_path = "/".join(path_parts[1:]) if len(path_parts) > 1 else ""

        # Verify pool exists and is imported
        if not zfs_operations.pool_is_imported(dbg, pool_name):
            # Try to find if it exists but isn't imported
            if zfs_operations.pool_exists(dbg, pool_name):
                raise util.create_storage_error(
                    "SR_BACKEND_FAILURE_107",
                    [
                        "ZFS pool not imported",
                        "pool '{0}' exists but is not imported - run 'zpool import {0}'".format(
                            pool_name
                        ),
                    ],
                )
            raise util.create_storage_error(
                "SR_BACKEND_FAILURE_107",
                [
                    "ZFS pool not found",
                    "pool '{}' does not exist - create it first".format(pool_name),
                ],
            )

        adopt = configuration.get("adopt", "").lower() == "true"

        if adopt:
            # Adopt mode: use the supplied path as-is. When path is
            # just a pool name (no parent), the zvols live directly
            # under the pool root. When path has components after the
            # pool, those form the dataset name.
            if parent_path:
                dataset_name = parent_path
                sr_dataset = zfs_operations.dataset_path(pool_name, dataset_name)
            else:
                dataset_name = ""
                sr_dataset = pool_name
            if not zfs_operations.dataset_exists(dbg, sr_dataset):
                raise util.create_storage_error(
                    "SR_BACKEND_FAILURE_109",
                    [
                        "Dataset not found for adopt",
                        "dataset '{}' does not exist -- adopt requires "
                        "an existing dataset".format(sr_dataset),
                    ],
                )
            log.debug(
                "%s: SR.create: adopt mode -- adopting existing " "dataset %s",
                dbg,
                sr_dataset,
            )
        else:
            # Normal mode: create a new dataset named by the SR UUID.
            if parent_path:
                dataset_name = "{}/{}".format(parent_path, sr_uuid)
            else:
                dataset_name = sr_uuid
            sr_dataset = zfs_operations.dataset_path(pool_name, dataset_name)

            if zfs_operations.dataset_exists(dbg, sr_dataset):
                raise util.create_storage_error(
                    "SR_BACKEND_FAILURE_109",
                    [
                        "Dataset already exists",
                        "dataset '{}' already exists in pool '{}'".format(dataset_name, pool_name),
                    ],
                )

            # Check for SR conflicts
            for parent in zfs_operations.dataset_get_parents(sr_dataset):
                if zfs_operations.dataset_exists(dbg, parent) and zfs_operations.dataset_is_sr(
                    dbg, parent
                ):
                    raise util.create_storage_error(
                        "SR_BACKEND_FAILURE_109",
                        [
                            "Cannot create SR inside another SR",
                            "parent dataset '{}' is already an SR".format(parent),
                        ],
                    )
            child_srs = zfs_operations.dataset_find_child_srs(dbg, pool_name, sr_dataset)
            if child_srs:
                raise util.create_storage_error(
                    "SR_BACKEND_FAILURE_109",
                    [
                        "Cannot create SR that contains another SR",
                        "child dataset(s) already SR: {}".format(", ".join(child_srs)),
                    ],
                )

        # Parse and validate ZFS properties from device-config
        dataset_props, vdi_defaults, sr_settings, config_errors = _parse_zfs_config(
            configuration, dbg=dbg, pool_name=pool_name
        )
        if config_errors:
            raise util.create_storage_error(
                "SR_BACKEND_FAILURE_109",
                ["Invalid ZFS configuration", "; ".join(config_errors)],
            )

        if not adopt:
            log.debug("{}: SR.create: creating dataset {}".format(dbg, sr_dataset))
            zfs_operations.dataset_create(dbg, sr_dataset)

        # Apply ZFS properties to the SR dataset
        # These are inherited by child zvols unless explicitly overridden
        if dataset_props:
            log.debug(
                "{}: SR.create: setting ZFS properties on {}: {}".format(
                    dbg, sr_dataset, dataset_props
                )
            )
            zfs_operations.dataset_set_properties(dbg, sr_dataset, dataset_props)

        # Mark this dataset as an SR (for conflict detection)
        zfs_operations.dataset_set_sr_marker(dbg, sr_dataset, sr_uuid)

        # Set ZFS mountpoint to standard SR location
        # This is stored in ZFS metadata and persists across reboots
        mountpoint = zfs_operations.sr_mount_path(sr_uuid)
        zfs_operations.dataset_set_mountpoint(dbg, sr_dataset, mountpoint)

        # Ensure dataset is mounted
        if not zfs_operations.dataset_is_mounted(dbg, sr_dataset):
            zfs_operations.dataset_mount(dbg, sr_dataset)

        importlib.import_module("zfs_live").Callbacks().create_database(mountpoint)

        # Datapath selection: 'raw-qdisk' (default) uses qemu-dp, 'tapdisk' uses blktap
        datapath = configuration.get("datapath", "raw-qdisk")
        if datapath not in ("tapdisk", "raw-qdisk"):
            raise util.create_storage_error(
                "SR_BACKEND_FAILURE_109",
                [
                    "Invalid configuration",
                    "datapath must be 'tapdisk' or 'raw-qdisk', got '{}'".format(datapath),
                ],
            )

        # Store pool and dataset in configuration for future attach operations
        configuration["zpool"] = pool_name
        configuration["dataset"] = dataset_name

        meta = {
            # mandatory elements we need everywhere
            "name": name,
            "description": description,
            "uuid": sr_uuid,
            # pool and dataset for this SR
            "zpool": pool_name,
            "dataset": dataset_name,
            # datapath selection for VDI access
            "datapath": datapath,
        }

        # Store VDI defaults in SR metadata (volblocksize, provisioning)
        if vdi_defaults:
            meta["vdi_defaults"] = vdi_defaults
            log.debug("{}: SR.create: VDI defaults: {}".format(dbg, vdi_defaults))

        # Merge SR-level settings (orphan cleanup config etc.) into meta
        for k, v in sr_settings.items():
            meta[k] = v
        if sr_settings:
            log.debug("{}: SR.create: SR settings: {}".format(dbg, sr_settings))

        util.update_sr_metadata(dbg, "file://" + mountpoint, meta)

        if adopt:
            zvols = zfs_operations.dataset_list_zvols(dbg, sr_dataset)
            adopted = 0
            failed = []
            zfs_live_mod = importlib.import_module("zfs_live")
            callbacks = zfs_live_mod.Callbacks()
            for zvol_name in zvols:
                vdi_uuid = zvol_name.split("/")[-1]
                try:
                    vdi_size = zfs_operations.get_zvol_size_bytes(dbg, zvol_name)
                    image_type = ImageFormat.IMAGE_RAW_QDISK
                    with VolumeContext(callbacks, mountpoint, "w") as opq:
                        with callbacks.db_context(opq) as db:
                            volume = db.insert_new_volume(vdi_size, image_type)
                            db.insert_vdi(vdi_uuid, "", vdi_uuid, volume.id, False)
                            volume.vsize = vdi_size
                            db.update_volume_vsize(volume.id, volume.vsize)
                    adopted += 1
                    log.debug(
                        "%s: SR.create adopt: registered VDI %s " "(%d bytes)",
                        dbg,
                        vdi_uuid,
                        vdi_size,
                    )
                except Exception as e:  # pylint: disable=broad-exception-caught
                    failed.append(zvol_name)
                    log.warning(
                        "%s: SR.create adopt: failed to register " "zvol %s: %s",
                        dbg,
                        zvol_name,
                        e,
                    )
            log.debug(
                "%s: SR.create adopt: registered %d of %d zvols",
                dbg,
                adopted,
                len(zvols),
            )
            if failed:
                raise util.create_storage_error(
                    "SR_BACKEND_FAILURE_109",
                    [
                        "Adopt incomplete -- {} of {} zvols failed to "
                        "register: {}".format(len(failed), len(zvols), ", ".join(failed))
                    ],
                )

        log.debug("{}: SR.create: sr={}".format(dbg, mountpoint))
        return configuration

    def destroy(self, dbg, sr):
        log.debug("{}: SR.destroy: sr={}".format(dbg, sr))
        meta = util.get_sr_metadata(dbg, "file://" + sr)
        pool_name = meta["zpool"]
        dataset_name = meta.get("dataset", "")

        if not dataset_name:
            raise util.create_storage_error(
                "SR_BACKEND_FAILURE_109",
                [
                    "Cannot destroy a pool-root SR",
                    "This SR was adopted at the pool root ({}). "
                    "Use 'xe sr-forget' to remove the XAPI metadata "
                    "without destroying the pool.".format(pool_name),
                ],
            )

        sr_dataset = zfs_operations.dataset_path(pool_name, dataset_name)

        log.debug("{}: SR.destroy: destroying dataset {}".format(dbg, sr_dataset))
        zfs_operations.dataset_destroy(dbg, sr_dataset, recursive=True)

        # Clean up mount point directory if empty
        if os.path.isdir(sr) and not os.listdir(sr):
            log.debug("{}: SR.destroy: removing mount point {}".format(dbg, sr))
            os.rmdir(sr)

    def attach(self, dbg, configuration):
        log.debug("{}: SR.attach: config={}".format(dbg, configuration))

        pool_name = configuration["zpool"]
        dataset_name = configuration["dataset"]
        sr_dataset = zfs_operations.dataset_path(pool_name, dataset_name)

        # Pool should already be imported by user
        if not zfs_operations.pool_is_imported(dbg, pool_name):
            raise util.create_storage_error(
                "SR_BACKEND_FAILURE_107",
                [
                    "ZFS pool not imported",
                    "pool '{}' is not imported - import it first".format(pool_name),
                ],
            )

        if not zfs_operations.dataset_exists(dbg, sr_dataset):
            raise util.create_storage_error(
                "SR_BACKEND_FAILURE_107",
                ["Dataset not found", "dataset '{}' does not exist".format(sr_dataset)],
            )

        # Get mountpoint from ZFS metadata
        mountpoint = zfs_operations.get_dataset_mountpoint(dbg, sr_dataset)

        # Ensure dataset is mounted
        if not zfs_operations.dataset_is_mounted(dbg, sr_dataset):
            log.debug("{}: SR.attach: mounting dataset {}".format(dbg, sr_dataset))
            zfs_operations.dataset_mount(dbg, sr_dataset)

        # Log detected properties for all VDIs (aids debugging legacy VDIs
        # created before property exposure -- all detection is ZFS-first)
        try:
            zvols = zfs_operations.dataset_list_zvols(dbg, sr_dataset)
            if zvols:
                log.debug("%s: SR.attach: detecting properties for %d VDI(s)", dbg, len(zvols))
                for zvol_name in zvols:
                    props = zfs_operations.vol_get_zfs_properties_dict(dbg, zvol_name)
                    vdi_id = zvol_name.split("/")[-1]
                    log.debug(
                        "%s: SR.attach: VDI %s: provisioning=%s, "
                        "volblocksize=%s, compression=%s (%s), "
                        "copies=%s (%s)",
                        dbg,
                        vdi_id,
                        props.get("provisioning", "?"),
                        props.get("volblocksize", "?"),
                        props.get("compression", "?"),
                        props.get("compression_source", "?"),
                        props.get("copies", "?"),
                        props.get("copies_source", "?"),
                    )
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.debug("%s: SR.attach: VDI property scan skipped: %s", dbg, e)

        # Probe ZFS features once per attach. The result is written
        # to a JSON sidecar under the SR mountpoint so later RPC
        # handlers (Plugin.Query, Volume.*, datapath) -- each
        # spawned by xapi-storage-script as a fresh interpreter --
        # can read it via `zfs_features.get_cached(sr_uuid)`
        # without re-probing per request.
        try:
            # Canonical SR UUID source: the `xcp:sr=<sr-uuid>` ZFS
            # user property SR.create stamped onto the dataset.
            # `configuration` is the device-config dict and does
            # not carry the SR UUID.
            sr_uuid = zfs_operations.dataset_get_sr_marker(dbg, sr_dataset)
            if sr_uuid:
                features = zfs_features.probe(dbg, sr_uuid, pool_name)
                zfs_features.set_cached(sr_uuid, features)
                zfs_features.log_summary(dbg, features)
            else:
                log.debug(
                    "%s: SR.attach: zfs-features probe skipped -- " "no xcp:sr marker on %s",
                    dbg,
                    sr_dataset,
                )
        except Exception as exc:  # pylint: disable=broad-except
            # Probe failure must never block attach -- capability
            # advertising falls back to the conservative "no
            # optional features" default per #230's failure model.
            log.debug("%s: SR.attach: zfs-features probe skipped: %s", dbg, exc)

        log.debug("{}: SR.attach: returning mountpoint {}".format(dbg, mountpoint))
        return mountpoint

    def detach(self, dbg, sr):
        log.debug("{}: SR.detach: sr={}".format(dbg, sr))
        meta = util.get_sr_metadata(dbg, "file://" + sr)
        pool_name = meta["zpool"]
        sr_dataset = zfs_operations.dataset_path(pool_name, meta["dataset"])

        # Drop the zfs-features sidecar before unmount so a re-attach
        # observes a fresh probe (operator may have run `zpool upgrade`
        # or updated OpenZFS between detach and re-attach).
        try:
            sr_uuid = zfs_operations.dataset_get_sr_marker(dbg, sr_dataset)
            if sr_uuid:
                zfs_features.clear_cached(sr_uuid)
        except Exception as exc:  # pylint: disable=broad-except
            log.debug("%s: SR.detach: zfs-features cleanup skipped: %s", dbg, exc)

        # Unmount the dataset - pool remains imported (user manages pool lifecycle)
        if zfs_operations.dataset_is_mounted(dbg, sr_dataset):
            log.debug("{}: SR.detach: unmounting dataset {}".format(dbg, sr_dataset))
            zfs_operations.dataset_unmount(dbg, sr_dataset)

    def _get_sr_dataset_path(self, meta):
        """Get the ZFS dataset path for zvol operations."""
        return zfs_operations.dataset_path(meta["zpool"], meta["dataset"])

    def ls(self, dbg, sr):
        results = []
        meta = util.get_sr_metadata(dbg, "file://" + sr)
        sr_dataset = self._get_sr_dataset_path(meta)
        cb = importlib.import_module("zfs_live").Callbacks()
        with VolumeContext(cb, sr, "r") as opq:
            with cb.db_context(opq) as db:
                vdis = db.get_all_vdis()
                all_custom_keys = db.get_all_vdi_custom_keys()
                for vdi in vdis:
                    zfs_operations.recover_vdi_vsize(dbg, vdi, db, sr_dataset)

            # Detect orphan zvols and snapshots (on disk but not in DB).
            # Both share auto_cleanup_orphans / orphan_grace_period_seconds.
            known_uuids = {vdi.uuid for vdi in vdis}
            cleanup_enabled = meta.get("auto_cleanup_orphans") == "true"
            grace = int(meta.get("orphan_grace_period_seconds", 3600))

            try:
                orphans = zfs_operations.zvol_find_orphans(dbg, sr_dataset, known_uuids)
                if orphans:
                    for zvol_name, volsize, creation in orphans:
                        log.warning(
                            "%s: SR.ls: orphan zvol detected: %s " "(size=%s, created=%s)",
                            dbg,
                            zvol_name,
                            volsize,
                            creation,
                        )
                    if cleanup_enabled:
                        zfs_operations.zvol_destroy_orphans(
                            dbg, orphans, grace_period_seconds=grace
                        )
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.warning("%s: SR.ls: zvol orphan scan failed: %s", dbg, e)

            try:
                snap_orphans = zfs_operations.zsnap_find_orphans(dbg, sr_dataset, known_uuids)
                if snap_orphans:
                    for snap_name, used, creation in snap_orphans:
                        log.warning(
                            "%s: SR.ls: orphan snapshot detected: %s " "(used=%s, created=%s)",
                            dbg,
                            snap_name,
                            used,
                            creation,
                        )
                    if cleanup_enabled:
                        zfs_operations.zsnap_destroy_orphans(
                            dbg, snap_orphans, grace_period_seconds=grace
                        )
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.warning("%s: SR.ls: snapshot orphan scan failed: %s", dbg, e)

            for vdi in vdis:
                image_format = ImageFormat.get_format(vdi.image_type)
                is_snapshot = bool(vdi.volume.snap)
                if is_snapshot:
                    vol_name = zfs_operations.find_snapshot_by_uuid(dbg, sr_dataset, vdi.uuid)
                    if vol_name is None:
                        log.error("ZFS snapshot %s missing from backing store", vdi.uuid)
                        continue
                else:
                    vol_name = zfs_operations.format_zvol_name(sr_dataset, vdi.uuid)
                    # Check if zvol exists (handles orphaned VDI metadata)
                    if not zfs_operations.vol_exists(vol_name):
                        log.error("volume %s not found on disk for VDI %s", vol_name, vdi.uuid)
                        continue
                psize = zfs_operations.get_zvol_used_bytes(dbg, vol_name)

                vdi_uri = cb.getVolumeUriPrefix(opq) + vdi.uuid
                custom_keys = {}
                if vdi.uuid in all_custom_keys:
                    custom_keys = all_custom_keys[vdi.uuid]

                # Query ZFS properties from the zvol
                zfs_keys = zfs_operations.vol_get_zfs_properties_dict(dbg, vol_name)
                merged_keys = {}
                merged_keys.update(zfs_keys)
                merged_keys.update(custom_keys)

                results.append(
                    {
                        "uuid": vdi.uuid,
                        "key": vdi.uuid,
                        "name": vdi.name,
                        "description": vdi.description,
                        "read_write": not is_snapshot,
                        "virtual_size": vdi.volume.vsize,
                        "physical_utilisation": psize,
                        "uri": [image_format.uri_prefix + vdi_uri],
                        "keys": merged_keys,
                        "sharable": bool(vdi.sharable),
                    }
                )

        return results

    def stat(self, dbg, sr):
        if not os.path.isdir(sr):
            raise xapi.storage.api.v5.volume.Sr_not_attached(sr)
        meta = util.get_sr_metadata(dbg, "file://" + sr)
        sr_dataset = self._get_sr_dataset_path(meta)

        # Report dataset space (respects quotas if set)
        used_space = zfs_operations.dataset_get_used(dbg, sr_dataset)
        free_space = zfs_operations.dataset_get_available(dbg, sr_dataset)

        # Check if quota is set - if so, total = quota, else use pool size
        quota = zfs_operations.dataset_get_quota(dbg, sr_dataset)
        if quota > 0:
            total_space = quota
        else:
            # No quota: total = used + available
            total_space = used_space + free_space

        # Surface pool-level ZFS capability state in `health[1]`.
        # SMAPIv3's SR.stat schema locks the field set, but health[1]
        # is a free-form string operators see via `xe sr-param-list`,
        # so it's the right place to convey actual pool-level
        # truth -- the answer Plugin.Query's `binary_*` fields can't
        # honestly give (Plugin.Query has no SR context). Reads the
        # sidecar #230's SR.attach wrote; falls back to an empty
        # string if the sidecar is missing (truthful silence beats
        # a guess).
        health_detail = format_capability_summary(meta.get("uuid", ""))

        return {
            "sr": sr,
            "name": meta["name"],
            "description": meta["description"],
            "total_space": total_space,
            "free_space": free_space,
            "uuid": meta["uuid"],
            # Note: datasources as dicts causes TypeError in SMAPIv3
            # Using empty list until proper format is determined
            "datasources": [],
            "clustered": False,
            "health": ["Healthy", health_detail],
        }

    def probe(self, dbg, configuration):
        """Inspect the proposed device-config and report what we can see.

        Probe is non-destructive: it never creates or mutates ZFS state.
        It returns a list of `probe_result` entries shaped per the
        SMAPIv3 v5 contract:

            { 'configuration': {...}, 'complete': bool,
              'sr': {...} | absent, 'extra_info': {...} }

        Behaviour:
          - Missing `path`: one incomplete entry that hints the caller
            to supply `path=<pool>[/<parent>]`.
          - `path` references a non-existent / unimported pool: one
            incomplete entry whose `extra_info` carries the diagnostic.
          - `path` resolves cleanly: one create-ready entry
            (`complete=True`, no `sr`) plus one entry per existing
            `zfs-live` SR found at or under the location, each
            populated with the SR's stat (name, uuid, free/total
            space). Existing-SR entries are `complete=True` with
            `path` rewritten to the parent so the caller can re-issue
            `SR.attach` against them.
        """
        log.debug("{}: SR.probe: config={}".format(dbg, configuration))
        results = []

        if "path" not in configuration:
            results.append(
                {
                    "configuration": dict(configuration),
                    "complete": False,
                    "extra_info": {
                        "hint": "supply 'path' as <pool>[/<parent-dataset>]",
                    },
                }
            )
            return results

        path = configuration["path"].strip().strip("/")
        if not path:
            results.append(
                {
                    "configuration": dict(configuration),
                    "complete": False,
                    "extra_info": {"error": "'path' is empty"},
                }
            )
            return results

        path_parts = path.split("/")
        pool_name = path_parts[0]
        parent_path = "/".join(path_parts[1:]) if len(path_parts) > 1 else ""

        if not zfs_operations.pool_is_imported(dbg, pool_name):
            extra = {}
            if zfs_operations.pool_exists(dbg, pool_name):
                extra["error"] = "pool '{}' exists but is not imported".format(pool_name)
            else:
                extra["error"] = "pool '{}' does not exist".format(pool_name)
            results.append(
                {
                    "configuration": dict(configuration),
                    "complete": False,
                    "extra_info": extra,
                }
            )
            return results

        # Probed location: pool[/parent]. We look for child SRs under it,
        # treating the location itself the same as any other ancestor.
        if parent_path:
            location = zfs_operations.dataset_path(pool_name, parent_path)
        else:
            location = pool_name

        # Discover existing SRs at-or-under `location`. The location
        # itself being an SR is *also* surfaced (caller probed an
        # exact SR path -- they get the attachable SR payload alongside
        # the create-readiness refusal emitted further down).
        existing_srs = []
        if zfs_operations.dataset_exists(dbg, location):
            if zfs_operations.dataset_is_sr(dbg, location):
                existing_srs.append(location)
            existing_srs.extend(zfs_operations.dataset_find_child_srs(dbg, pool_name, location))

        for sr_dataset in existing_srs:
            sr_uuid = zfs_operations.dataset_get_sr_marker(dbg, sr_dataset)
            entry_config = dict(configuration)
            # Rewrite `path` to the SR's parent so a follow-up
            # SR.attach with this configuration would target the SR.
            sr_parent = "/".join(sr_dataset.split("/")[:-1])
            entry_config["path"] = sr_parent
            entry_config["zpool"] = pool_name
            entry_config["dataset"] = "/".join(sr_dataset.split("/")[1:])

            sr_info = self._probe_sr_info(dbg, sr_dataset, sr_uuid)
            entry = {
                "configuration": entry_config,
                "complete": True,
                "extra_info": {
                    "dataset": sr_dataset,
                },
            }
            if sr_info is not None:
                entry["sr"] = sr_info
            results.append(entry)

        # Create-readiness entry: the location itself is suitable for a
        # new SR if (a) the parent path is reachable and (b) the parent
        # is not already an SR (no nested SRs allowed).
        create_ready = True
        create_extra = {}
        if parent_path and not zfs_operations.dataset_exists(dbg, location):
            create_ready = False
            create_extra["error"] = "parent dataset '{}' does not exist".format(location)
        else:
            for parent in zfs_operations.dataset_get_parents(location):
                if zfs_operations.dataset_exists(dbg, parent) and zfs_operations.dataset_is_sr(
                    dbg, parent
                ):
                    create_ready = False
                    create_extra["error"] = "ancestor dataset '{}' is already an SR".format(parent)
                    break
            if create_ready and zfs_operations.dataset_is_sr(dbg, location):
                create_ready = False
                create_extra["error"] = "dataset '{}' is already an SR".format(location)

        results.append(
            {
                "configuration": dict(configuration),
                "complete": create_ready,
                "extra_info": create_extra,
            }
        )

        return results

    def _probe_sr_info(self, dbg, sr_dataset, sr_uuid):
        """Best-effort SR stat for a discovered SR dataset.

        Reads name/description from `meta.json` if the SR is currently
        mounted; otherwise falls back to ZFS-derived defaults. Returns
        None only if even the ZFS-derived numbers fail (in that case
        the caller emits the entry without an `sr` field, which the
        SMAPIv3 contract permits).
        """
        try:
            free_space = zfs_operations.dataset_get_available(dbg, sr_dataset)
            used_space = zfs_operations.dataset_get_used(dbg, sr_dataset)
            quota = zfs_operations.dataset_get_quota(dbg, sr_dataset)
            total_space = quota if quota > 0 else used_space + free_space
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.debug("{}: SR.probe: stat failed for {}: {}".format(dbg, sr_dataset, e))
            return None

        name = ""
        description = ""
        try:
            mountpoint = zfs_operations.get_dataset_mountpoint(dbg, sr_dataset)
            if mountpoint and os.path.isdir(mountpoint):
                meta = util.get_sr_metadata(dbg, "file://" + mountpoint)
                name = meta.get("name", "") or ""
                description = meta.get("description", "") or ""
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.debug("{}: SR.probe: meta read failed for {}: {}".format(dbg, sr_dataset, e))

        return {
            "sr": sr_dataset,
            "name": name,
            "uuid": sr_uuid or "",
            "description": description,
            "free_space": int(free_space),
            "total_space": int(total_space),
            "datasources": [],
            "clustered": False,
            "health": ["Healthy", ""],
        }

    def set_name(self, dbg, sr, new_name):
        util.update_sr_metadata(dbg, "file://" + sr, {"name": new_name})

    def set_description(self, dbg, sr, new_description):
        util.update_sr_metadata(dbg, "file://" + sr, {"description": new_description})


if __name__ == "__main__":
    log.log_call_argv()
    cmd = xapi.storage.api.v5.volume.SR_commandline(Implementation())

    call("zfs-live.sr", ["modprobe", "zfs"])

    base = os.path.basename(sys.argv[0])
    if base == "SR.create":
        cmd.create()
    elif base == "SR.attach":
        cmd.attach()
    elif base == "SR.destroy":
        cmd.destroy()
    elif base == "SR.detach":
        cmd.detach()
    elif base == "SR.ls":
        cmd.ls()
    elif base == "SR.stat":
        cmd.stat()
    elif base == "SR.set_name":
        cmd.set_name()
    elif base == "SR.set_description":
        cmd.set_description()
    elif base == "SR.probe":
        cmd.probe()
    else:
        raise xapi.storage.api.v5.volume.Unimplemented(base)
