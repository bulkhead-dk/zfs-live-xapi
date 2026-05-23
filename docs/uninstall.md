# Uninstall

## Remove the driver

```bash
cd zfs-live
bash installer.sh remove
```

This removes:
- Volume and datapath plugin scripts
- `xe` CLI wrapper (restored to the original `xe` binary)

## What happens to existing SRs

**Your data is safe.** The ZFS datasets and zvols are NOT touched
by the uninstaller. The `xe sr-list` entry will show a broken SR
(no plugin to handle it), but the underlying ZFS data remains.

To re-activate an SR after reinstalling the driver, plug the PBD:

```bash
xe pbd-plug uuid=$(xe pbd-list sr-uuid=<sr-uuid> --minimal)
```

## Complete cleanup (destroys data)

If you also want to remove the ZFS datasets:

```bash
# Destroy the SR (removes VDI metadata)
xe sr-destroy uuid=<sr-uuid>

# Destroy the ZFS pool (DESTROYS ALL DATA)
zpool destroy <pool-name>
```
