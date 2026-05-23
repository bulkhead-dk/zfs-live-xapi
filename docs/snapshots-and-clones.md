# Snapshots & Clones

## Snapshots

`xe vdi-snapshot` creates an instant ZFS snapshot. No data is
copied — the snapshot is a point-in-time reference that shares
blocks with the original VDI until either diverges.

```bash
SNAP=$(xe vdi-snapshot uuid=<vdi-uuid>)
echo "Snapshot: $SNAP"
```

Snapshots are read-only. They appear as VDIs in `xe vdi-list`
with `is-a-snapshot=true`.

## Clones

`xe vdi-clone` creates a writable copy using ZFS clone. Like
snapshots, clones share blocks with the parent and only allocate
space for divergent writes.

```bash
CLONE=$(xe vdi-clone uuid=<vdi-uuid>)
echo "Clone: $CLONE"
```

Clones are fully independent VDIs — they can be attached to VMs,
resized, snapshotted, or destroyed independently.

## How it works under the hood

| Operation | ZFS command | Time | Space |
|-----------|------------|------|-------|
| Snapshot | `zfs snapshot` | Instant | Zero (shared blocks) |
| Clone | `zfs snapshot` + `zfs clone` | Instant | Zero initially |
| Destroy snapshot | `zfs destroy` | Instant | Frees unique blocks |

Both operations are O(1) regardless of VDI size — a 1 TB snapshot
takes the same time as a 1 GB snapshot.

## Xen Orchestra integration

XO's snapshot and clone operations use the same `xe` API and work
identically. The ZFS layer is transparent to XO.

## Limitations

- Snapshots cannot be resized (ZFS constraint)
- Destroying a VDI that has dependent clones requires promoting
  the clones first (handled automatically by the driver)
