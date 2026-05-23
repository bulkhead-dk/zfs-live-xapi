# -*- coding: utf-8 -*-
import xapi.storage.libs.libcow.callbacks
from xapi.storage.libs import util

import zfs_operations


class Callbacks(xapi.storage.libs.libcow.callbacks.Callbacks):
    "ZFS-live callbacks"

    def getVolumeUriPrefix(self, opq):  # pylint: disable=invalid-name
        """Return URI prefix for raw-qdisk datapath.

        The URI format is: zfs-live//dev/zvol/<sr_dataset>/
        When VDI UUID is appended, the full device path is formed.
        """
        # Get SR metadata to find the ZFS dataset path
        meta = util.get_sr_metadata("zfs-live.getVolumeUriPrefix", "file://" + opq)
        sr_dataset = zfs_operations.dataset_path(meta["zpool"], meta["dataset"])
        return "zfs-live//dev/zvol/{}/".format(sr_dataset)

    def volumeGetPath(self, opq, name):  # pylint: disable=invalid-name
        """Return the block device path for a volume."""
        meta = util.get_sr_metadata("zfs-live.volumeGetPath", "file://" + opq)
        sr_dataset = zfs_operations.dataset_path(meta["zpool"], meta["dataset"])
        return "/dev/zvol/{}/{}".format(sr_dataset, name)
