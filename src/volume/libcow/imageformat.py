#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extended ImageFormat class with raw-qdisk support.

This module extends the upstream xapi.storage.libs.libcow.imageformat
to add support for the raw-qdisk datapath (IMAGE_RAW_QDISK format).

Usage:
    # Instead of:
    # from xapi.storage.libs.libcow.imageformat import ImageFormat

    # Use:
    from libcow.imageformat import ImageFormat
"""

from xapi.storage.libs.libcow.imageformat import ImageFormat as _BaseImageFormat

# New format constant for raw-qdisk datapath
# This uses qemu-dp with raw block device access instead of tapdisk
IMAGE_RAW_QDISK = 3


class _RawQdiskFormat:
    """Format descriptor for raw-qdisk datapath.

    This format uses qemu-dp to provide direct access to raw block devices
    (like ZFS zvols) with support for live storage migration and CBT.
    """

    def __init__(self):
        self.uri_prefix = "raw+qdisk://"
        self.image_type = IMAGE_RAW_QDISK

    def __repr__(self):
        return "RawQdiskFormat(uri_prefix={})".format(self.uri_prefix)


class ImageFormat(_BaseImageFormat):
    """Extended ImageFormat with raw-qdisk support.

    Adds IMAGE_RAW_QDISK format type that maps to the raw+qdisk://
    URI scheme for the raw-qdisk datapath plugin.
    """

    # Re-export base class constants for convenience
    IMAGE_RAW = _BaseImageFormat.IMAGE_RAW
    IMAGE_VHD = _BaseImageFormat.IMAGE_VHD
    # IMAGE_QCOW2 may not exist in older XCP-ng versions
    IMAGE_QCOW2 = getattr(_BaseImageFormat, "IMAGE_QCOW2", 2)

    # New format constant
    IMAGE_RAW_QDISK = IMAGE_RAW_QDISK

    # Format registry extension
    _extended_formats = {IMAGE_RAW_QDISK: _RawQdiskFormat()}

    @classmethod
    def get_format(cls, image_type):
        """Get format descriptor for the given image type.

        Checks extended formats first, then falls back to base class.

        Args:
            image_type: Format type constant (IMAGE_RAW, IMAGE_RAW_QDISK, etc.)

        Returns:
            Format descriptor with uri_prefix and other properties

        Raises:
            ValueError: If image_type is not recognized
        """
        # Check our extended formats first
        if image_type in cls._extended_formats:
            return cls._extended_formats[image_type]

        # Fall back to base class
        return _BaseImageFormat.get_format(image_type)

    @classmethod
    def get_format_by_uri(cls, uri):
        """Get format descriptor by URI prefix.

        Args:
            uri: A storage URI (e.g., "raw+qdisk://zfs-live//dev/zvol/...")

        Returns:
            Format descriptor matching the URI prefix

        Raises:
            ValueError: If no format matches the URI
        """
        # Check extended formats
        for fmt in cls._extended_formats.values():
            if uri.startswith(fmt.uri_prefix):
                return fmt

        # Fall back to base class behavior
        # Note: Base class may not have this method, so we implement it here
        if hasattr(_BaseImageFormat, "get_format_by_uri"):
            return _BaseImageFormat.get_format_by_uri(uri)

        # Manual lookup for base formats
        for image_type in [cls.IMAGE_RAW, cls.IMAGE_VHD, cls.IMAGE_QCOW2]:
            try:
                fmt = _BaseImageFormat.get_format(image_type)
                if hasattr(fmt, "uri_prefix") and uri.startswith(fmt.uri_prefix):
                    return fmt
            except (ValueError, KeyError):
                continue

        raise ValueError("No format found for URI: {}".format(uri))

    @classmethod
    def is_raw_qdisk(cls, image_type):
        """Check if the image type is raw-qdisk.

        Args:
            image_type: Format type constant

        Returns:
            True if this is the raw-qdisk format
        """
        return image_type == IMAGE_RAW_QDISK
