# Backup Integration

## Xen Orchestra (XO)

XO backup and replication workflows are fully compatible:

- **Full backup** — works out of the box
- **Delta backup** — works via Changed Block Tracking (CBT)
- **Continuous Replication** — works (snapshot + delta)
- **Disaster Recovery** — works (full + incremental)

No XO configuration changes needed. XO talks to XAPI, which talks
to the driver — the ZFS layer is transparent.

## Changed Block Tracking (CBT)

The driver tracks changed blocks using QEMU dirty bitmaps. When XO
requests an incremental backup, only the blocks that changed since
the last snapshot are transferred.

### Enable CBT on a VDI

```bash
xe vdi-enable-cbt uuid=<vdi-uuid>
```

### Check CBT status

```bash
xe vdi-param-get uuid=<vdi-uuid> param-name=cbt-enabled
```

### How it works

1. `Datapath.activate` creates a QEMU dirty bitmap for the VDI
2. Writes to the VDI are tracked in the bitmap
3. `Volume.snapshot` captures the bitmap state at snapshot time
4. `Volume.list_changed_blocks` returns the dirty extents between
   two snapshots (used by XO for delta computation)
5. Bitmap state persists across qemu-dp restarts and SR reattach

CBT metadata is stored under the SR mount at:
```
/var/run/sr-mount/<sr-uuid>/.zfs-live/cbt/<vdi-key>.pickle
```

## Native ZFS send/receive (cross-host copy)

For offline VDI relocation between two zfs-live SRs:

```bash
xe vdi-copy uuid=<vdi-uuid> sr-uuid=<dest-sr-uuid>
```

When both source and destination are zfs-live SRs, this uses
native `zfs send | zfs receive` instead of XAPI's `sparse_dd`
fallback — typically 4-5x faster.
