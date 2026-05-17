# Quickstart

From bare XCP-ng host to a VM running on ZFS in 5 minutes.

## Prerequisites

- XCP-ng 8.3 host with root SSH access
- ZFS installed (`yum install zfs && modprobe zfs`)
- At least one disk available for a ZFS pool

## 1. Create a ZFS pool

```bash
# Single disk (dev/testing)
zpool create -f tank /dev/sdb

# Mirror (production)
zpool create -f tank mirror /dev/sdb /dev/sdc
```

## 2. Install the driver

```bash
git clone https://git.bulkhead.dk/storage/zfs-live.git
cd zfs-live
bash installer.sh
```

## 3. Create an SR

```bash
xe sr-create type=zfs-live name-label="ZFS Storage" \
  device-config:path=tank
```

## 4. Create a VDI and add it to a VM

```bash
VDI=$(xe vdi-create sr-uuid=$(xe sr-list type=zfs-live --minimal) \
  name-label="my-disk" type=user virtual-size=50GiB)

# Create the VBD (disk attachment record). The VM must be halted
# or the VBD will attach on next boot.
xe vbd-create vm-uuid=<your-vm-uuid> vdi-uuid=$VDI device=xvdb
```

## 5. Verify

```bash
xe sr-list type=zfs-live
zpool list
zfs list -t volume
```

Your VM can now use the ZFS-backed disk. ZFS compression is
inherited from the pool by default.

## Next steps

- [Configuration](configuration.md) — set compression, copies, sync per-SR or per-VDI
- [Snapshots & Clones](snapshots-and-clones.md) — instant snapshots via `xe vdi-snapshot`
- [Backup Integration](backup-integration.md) — Xen Orchestra backup compatibility
