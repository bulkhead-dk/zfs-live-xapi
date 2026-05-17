# Known Limitations

## Live storage migration (SXM) is not yet supported

`xe vm-migrate ... vdi:<vdi>=<dest-sr>` (live storage migration of
a running VM between hosts) is fully implemented in the driver but
blocked by an upstream limitation in xapi. The upstream
`Storage_migrate` module assumes tapdisk as the NBD backend and
fails before it ever reaches our mirror handlers. When the upstream
patch lands, SXM works automatically with no driver update needed.

**Workaround:** Use `xe vdi-copy` for offline VDI relocation between
zfs-live SRs (uses native ZFS send/receive). For live VM migration
without storage migration, use shared storage or migrate the VM
first, then copy the VDI.

## UEFI boot limitation

UEFI Linux VMs may hang at boot when a zfs-live boot disk is
attached at userdevice 0–3. Moving the boot disk to userdevice 4 or
higher clears the hang. This appears to be an upstream XCP-ng / QEMU
/ OVMF issue, not specific to zfs-live.

## `xe vdi-copy` uses a CLI wrapper

`xe vdi-copy` between zfs-live SRs routes through a CLI wrapper
that intercepts the command and calls our native ZFS send/receive
implementation. This is because upstream xapi doesn't recognise the
`VDI_COPY` capability yet. The wrapper is transparent — operators
use `xe vdi-copy` normally.

## VDI shrink not supported

ZFS zvols cannot be shrunk — only grown. `xe vdi-resize` with a
smaller size than the current size is rejected.

## Python 2 on host

XCP-ng 8.3 runs SMAPIv3 plugin scripts under Python 2. The
installer rewrites shebangs automatically. The native
xapi-storage-script daemon handles all dispatch.
